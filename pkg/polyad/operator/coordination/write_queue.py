"""
Identify incompatible Kubernetes writes before they leave a local adapter queue.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from attrs import define, field, frozen

# Preserve existing import paths while keeping each exception defined centrally.
from polyad.exceptions.kubernetes import WriteConflict as WriteConflict

if TYPE_CHECKING:
    from typing import Any

__all__ = (
    "PendingWrites",
    "WriteConflict",
    "WriteIntent",
    "write_intent",
)


@frozen
class WriteIntent:
    """
    Describe one concrete write without copying its potentially sensitive payload.

    Attributes:
        target (tuple[str, str, str]): Kind, namespace and concrete resource name within this adapter's cluster.
        method (str): HTTP mutation method.
        fingerprint (str): Private equality digest of the exact request, including subresource and query.
        uid (str | None): Expected target incarnation, when supplied by the caller.
        revision (str | None): Opaque expected resource version, when supplied by the caller.
    """

    target: tuple[str, str, str]
    method: str
    fingerprint: str = field(repr=False)
    uid: str | None = None
    revision: str | None = None


def write_intent(
    method: str,
    kind: str,
    namespace: str,
    name: str,
    body: Any,
    *,
    status: bool = False,
    query: list[tuple[str, str]] | None = None,
) -> WriteIntent | None:
    """
    Identify named mutations while leaving reads, reviews and dry runs outside conflict checks.

    Args:
        method (str): Kubernetes HTTP method.
        kind (str): Resource kind.
        namespace (str): Target namespace.
        name (str): URL resource name, empty for creates.
        body (Any): Already encoded, immutable-for-dispatch request snapshot.
        status (bool): Whether this request targets the status subresource.
        query (list[tuple[str, str]] | None): Exact query parameters, including any dry-run request.

    Returns:
        WriteIntent | None: Comparable named write, or None when there is no persistent named effect.
    """
    if method not in {"POST", "PUT", "PATCH", "DELETE"} or kind in {"TokenReview", "SubjectAccessReview"}:
        return None

    # Server validation has no persistent effect and must not invalidate a real queued write.
    if any(key == "dryRun" and value == "All" for key, value in query or []):
        return None
    document = body if isinstance(body, dict) else {}
    metadata = document.get("metadata") or {}
    target_name = name or (metadata.get("name") if method == "POST" else None)
    if not target_name:
        return None
    conditions = (document.get("preconditions") or {}) if method == "DELETE" else metadata
    encoded = json.dumps([method, status, query or [], body], sort_keys=True, separators=(",", ":"))
    return WriteIntent(
        (kind, namespace, target_name),
        method,
        hashlib.sha256(encoded.encode()).hexdigest(),
        conditions.get("uid"),
        conditions.get("resourceVersion"),
    )


@define
class PendingWrites:
    """
    Keep pending conflicts local to one cluster adapter and require explicit replanning.

    Attributes:
        entries (dict[int, WriteIntent]): Concrete writes that have not reached transport.
        targets (dict[tuple[str, str, str], set[int]]): Pending tokens indexed by resource to avoid scanning unrelated writes.
        conflicted (set[int]): Invalidated tokens retained until their callers leave the queue.
    """

    entries: dict[int, WriteIntent] = field(factory=dict)
    targets: dict[tuple[str, str, str], set[int]] = field(factory=dict)
    conflicted: set[int] = field(factory=set)

    def add(self, token: int, intent: WriteIntent) -> None:
        """
        Invalidate both sides of incompatible pending writes to the same object.

        Args:
            token (int): Adapter-local backlog token.
            intent (WriteIntent): Snapshot identity and equality information.

        Returns:
            None: Existing transports are absent from this pending-only registry.
        """

        # Neither conflicting caller is privileged: both must reconcile from a fresh observation.
        tokens = self.targets.setdefault(intent.target, set())
        for other in tokens:
            if other not in self.conflicted and self.entries[other].fingerprint != intent.fingerprint:
                self.conflicted.update((other, token))
        self.entries[token] = intent
        tokens.add(token)

    def check(self, token: int) -> None:
        """
        Require fresh reconciliation for an invalidated request.

        Args:
            token (int): Adapter-local backlog token.

        Returns:
            None: Raises WriteConflict when incompatible pending intent was observed.
        """
        if token in self.conflicted:
            raise WriteConflict("overlapping_pending_writes")

    def remove(self, token: int) -> None:
        """
        Forget a dispatched, rejected or cancelled request.

        Args:
            token (int): Adapter-local backlog token.

        Returns:
            None: Queue bookkeeping never persists beyond the waiting request.
        """
        intent = self.entries.pop(token, None)
        if intent is not None:
            tokens = self.targets[intent.target]
            tokens.remove(token)
            if not tokens:
                del self.targets[intent.target]
        self.conflicted.discard(token)
