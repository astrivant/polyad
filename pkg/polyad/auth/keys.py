"""
Load projected credential references and rotate tokens without changing lane identities.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from polyad.auth.policy import LISTENERS
from polyad_types.auth import Authentication, KeyDirection
from polyad_types.codec import from_dict

if TYPE_CHECKING:
    from polyad_types.auth import APIKey


class Keyring:
    """
    Read one atomic Kubernetes projected volume containing policy and secret files.
    """

    def __init__(self, path: Path) -> None:
        """
        Validate the complete registry before serving any request.

        Args:
            path (Path): Policy JSON alongside group/name token files.
        """
        self.path = path
        self.lock = Lock()
        self.revision: Path | None = None
        self.entries: list[tuple[str, APIKey, str]] = []
        self.read()

    @classmethod
    def from_environment(cls) -> Keyring | None:
        """
        Load optional operator credentials from their projected volume.

        Returns:
            Keyring | None: Configured registry or None for existing endpoint credentials.
        """
        filename = os.environ.get("POLYAD_AUTH_CONFIG_FILE")
        return cls(Path(filename)) if filename else None

    def read(self) -> list[tuple[str, APIKey, str]]:
        """
        Resolve an atomic projected-volume revision and reject duplicate bearer identities.

        Returns:
            list[tuple[str, APIKey, str]]: Group, policy and token entries; never logged or persisted.
        """
        with self.lock:
            # Resolving the policy symlink pins all reads to one Kubernetes projection revision.
            root = self.path.resolve(strict=True).parent
            if root == self.revision:
                return list(self.entries)
            policy = from_dict(json.loads((root / self.path.name).read_text()), Authentication)
            entries = []
            seen = set()
            for group in ("services", "operators"):
                for key in getattr(policy, group):
                    token = (root / group / key.name).read_text().strip()
                    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
                        raise ValueError("API key Secret must contain a nonempty printable bearer token")
                    digest = hashlib.sha256(token.encode()).digest()
                    if digest in seen:
                        raise ValueError("API key tokens must identify exactly one credential lane")
                    seen.add(digest)
                    entries.append((group, key, token))
            if root != self.path.parent.resolve():
                self.revision, self.entries = root, entries
            return entries

    def inbound(self, endpoint: str) -> bool:
        """
        Determine whether this registry replaces an endpoint's single credential.

        Args:
            endpoint (str): Composition, events, metrics or observations API.

        Returns:
            bool: Whether any key authorizes incoming traffic to this API.
        """
        scopes = LISTENERS.get(endpoint, {endpoint})
        return any(key.direction != KeyDirection.OUTBOUND and scopes.intersection(key.endpoints) for _, key, _ in self.read())
