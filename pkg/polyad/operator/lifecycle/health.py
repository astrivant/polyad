"""
Expose process replacement and credential changes through thread-safe health state.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from threading import Event

from polyad.auth.keys import Keyring
from polyad.auth.policy import public_demo

__all__ = (
    "Lifecycle",
    "credential_token",
    "credentials_changed",
    "lifecycle",
    "watch_credentials",
)


class Lifecycle:
    """
    Signal drain or replacement to health probes without mutating the Deployment.
    """

    def __init__(self) -> None:
        """
        Initialize process-wide signal flags and credential fingerprints.
        """
        self.replacement = Event()
        self.draining = Event()
        self.credentials: dict[Path, bytes] = {}


lifecycle = Lifecycle()


def credential_token(endpoint: str, *, setting: str = "TOKEN") -> str:
    """
    Read a projected Secret token and retain only its fingerprint for health checks.

    Args:
        endpoint (str): API, EVENTS, METRICS or CACHE configuration prefix.
        setting (str): Credential setting suffix, TOKEN or URL.

    Returns:
        str: Required token, retaining environment compatibility for local use.
    """
    if setting == "TOKEN":
        if public_demo():
            return ""
        keys = Keyring.from_environment()
        scope = {"API": "composition", "EVENTS": "events", "METRICS": "metrics", "OBSERVER": "observations"}.get(endpoint)
        if keys and scope and (keys.inbound(scope) or (endpoint == "EVENTS" and (keys.inbound("topology") or keys.inbound("discovery")))):
            return ""
    filename = os.environ.get(f"POLYAD_{endpoint}_{setting}_FILE")
    if not filename:
        return os.environ.get(f"POLYAD_{endpoint}_{setting}", "")
    path = Path(filename)
    token = path.read_bytes()

    # Retain a change detector, not another long-lived copy of the Secret's bytes.
    lifecycle.credentials[path] = hashlib.sha256(token).digest()
    return token.decode()


def credentials_changed() -> bool:
    """
    Detect replacement or removal of any mounted credential without exposing token data.

    Returns:
        bool: Whether the running replica's loaded credentials are stale.
    """
    for path, fingerprint in lifecycle.credentials.items():
        try:
            if hashlib.sha256(path.read_bytes()).digest() != fingerprint:
                return True
        except OSError:
            return True
    return False


async def watch_credentials() -> None:
    """
    Mark stale replicas unhealthy so Kubernetes restarts them with current Secrets.

    Returns:
        None: No return value.
    """
    while True:
        if await asyncio.to_thread(credentials_changed):
            lifecycle.replacement.set()
        await asyncio.sleep(5)
