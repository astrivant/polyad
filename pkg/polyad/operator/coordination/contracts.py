"""
Retain decision read sets and revalidate their semantic state at write dispatch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from polyad.compiler.registry import RECONCILED_KINDS
from polyad.operator.coordination.write_queue import WriteConflict
from polyad_types.resources import GROUP, VERSION

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad.operator.coordination.queue import Key
    from polyad.operator.coordination.write_queue import WriteIntent

__all__ = (
    "Observation",
    "ReadContract",
    "active_contract",
    "capture_decision",
    "expires_before",
    "resource_digest",
    "without_capture",
)


logger = logging.getLogger(__name__)
active_contract: ContextVar[ReadContract | None] = ContextVar("polyad_write_contract", default=None)


def resource_digest(document: Any) -> str:
    """
    Include semantic metadata while excluding fields which change on every write.

    Args:
        document (Any): Complete observed document or None.

    Returns:
        str: Stable digest; no resource payload is retained in the contract.
    """
    if document is not None:
        metadata = {key: value for key, value in document.get("metadata", {}).items() if key not in {"resourceVersion", "managedFields"}}
        document = {
            "content": {key: value for key, value in document.items() if key not in {"kind", "apiVersion", "metadata"}},
            "identity": metadata,
        }
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class Observation:
    """
    Store a resource or collection read and its expected semantic digests.

    Attributes:
        api (API): Adapter identifying the observed cluster.
        key (Key): Kind, namespace and name; empty name denotes a collection.
        query (tuple[tuple[str, str], ...]): Original collection selectors.
        expected (dict[str, str]): Per-name digests, with an empty key for a named read.
    """

    api: API = field(repr=False)
    key: Key
    query: tuple[tuple[str, str], ...]
    expected: dict[str, str] = field(repr=False)

    def snapshot(self, result: Any) -> dict[str, str]:
        """
        Normalize list ordering while retaining additions, removals and explicit absence.

        Args:
            result (Any): Fresh Kubernetes GET response.

        Returns:
            dict[str, str]: Comparable digests of all members or the named object.
        """
        if self.key[2]:
            return {"": resource_digest(result)}
        if not isinstance(result, dict) or result.get("metadata", {}).get("continue"):
            raise WriteConflict("dependency_collection_incomplete")
        return {item["metadata"]["name"]: resource_digest(item) for item in result.get("items", [])}


@dataclass
class ReadContract:
    """
    Keep one reconciliation's observed dependencies across local and remote adapters.

    Attributes:
        reads (dict[tuple[Any, ...], Observation]): First observations, advanced only by acknowledged own writes.
        affected (dict[API, set[Key]]): Reconcilable resources and owners to refresh on drift.
        invalid (str | None): A consumed or uncertain contract cannot silently rebase.
        notified (bool): Whether this failed decision already requested follow-up work.
        deadline (float | None): Earliest wall-clock expiry of a time-dependent decision input.
    """

    reads: dict[tuple[Any, ...], Observation] = field(default_factory=dict, repr=False)
    affected: dict[API, set[Key]] = field(default_factory=dict, repr=False)
    invalid: str | None = None
    notified: bool = False
    deadline: float | None = None

    def clone(self) -> ReadContract:
        """
        Freeze enqueue-time expectations while retaining cluster adapter identities.

        Returns:
            ReadContract: Independent hashes and recovery keys without copying credentials.
        """
        return ReadContract(
            reads={key: Observation(value.api, value.key, value.query, dict(value.expected)) for key, value in self.reads.items()},
            affected={api: set(keys) for api, keys in self.affected.items()},
            invalid=self.invalid,
            deadline=self.deadline,
        )

    def signature(self) -> str:
        """
        Identify equivalent decision inputs and recovery scopes for pending-write coalescing.

        Returns:
            str: Private equality digest, distinct for different source graphs or clusters.
        """
        reads = sorted((id(item.api), item.key, item.query, sorted(item.expected.items())) for item in self.reads.values())
        affected = sorted((id(api), sorted(keys)) for api, keys in self.affected.items())
        return hashlib.sha256(json.dumps([reads, affected, self.invalid, self.deadline], sort_keys=True).encode()).hexdigest()

    def expired(self) -> bool:
        """
        Detect expiry of a TTL grant or measured input without requiring a resource event.

        Returns:
            bool: Whether this decision's earliest known deadline has elapsed.
        """
        return self.deadline is not None and time.time() >= self.deadline

    def owners(self, api: API, key: Key, result: Any) -> None:
        """
        Remember graph owners even when their native child subsequently disappears.

        Args:
            api (API): Cluster adapter used for this observation.
            key (Key): Resource or collection identity.
            result (Any): Document, list response or explicit absence.

        Returns:
            None: Follow-up hints contain keys only, never write payloads.
        """
        targets = self.affected.setdefault(api, set())
        if key[2] and key[0] in RECONCILED_KINDS:
            targets.add(key)
        documents = (result or {}).get("items", []) if not key[2] else [result] if result else []
        for document in documents:
            meta = document.get("metadata", {})
            namespace = meta.get("namespace", key[1])
            if key[0] in RECONCILED_KINDS and meta.get("name"):
                targets.add((key[0], namespace, meta["name"]))
            for owner in meta.get("ownerReferences", []):
                if owner.get("controller") and owner.get("apiVersion") == f"{GROUP}/{VERSION}" and owner.get("kind") in RECONCILED_KINDS:
                    targets.add((owner["kind"], namespace, owner["name"]))

    def record(self, api: API, key: Key, query: list[tuple[str, str]] | None, result: Any) -> None:
        """
        Capture the first read rather than silently adopting a later conflicting observation.

        Args:
            api (API): Cluster adapter used by the decision.
            key (Key): Observed resource or collection.
            query (list[tuple[str, str]] | None): Original GET selectors.
            result (Any): Returned document or explicit absence.

        Returns:
            None: Stores private digests and bounded resource identities only.
        """
        selectors = tuple(query or ())
        identity = (api, *key, selectors)
        self.owners(api, key, result)
        if identity not in self.reads:
            observation = Observation(api, key, selectors, {})
            observation.expected = observation.snapshot(result)
            self.reads[identity] = observation
            if sum(len(item.expected) for item in self.reads.values()) > 16384:
                self.invalid = "dependency_contract_too_large"
                raise WriteConflict(self.invalid)

    async def validate(self) -> None:
        """
        Refresh the captured read set at dispatch without modifying its original expectations.

        Returns:
            None: Drift or unavailable observations stop the write before transport.
        """
        if self.invalid:
            raise WriteConflict(self.invalid)
        if self.expired():
            raise WriteConflict("dependency_deadline_elapsed")
        with without_capture():
            for observation in self.reads.values():
                try:
                    current = await observation.api.request("GET", *observation.key, query=list(observation.query))
                    self.owners(observation.api, observation.key, current)
                    matches = observation.snapshot(current) == observation.expected
                except Exception as error:
                    self.invalid = "dependency_observation_unavailable"
                    raise WriteConflict(self.invalid) from error
                if not matches:
                    self.invalid = "dependency_state_changed"
                    raise WriteConflict(self.invalid)

    def advance(self, api: API, intent: WriteIntent, result: Any) -> None:
        """
        Advance only the effects of an acknowledged write made by this decision.

        Args:
            api (API): Adapter which acknowledged the mutation.
            intent (WriteIntent): Dispatched target identity.
            result (Any): Acknowledged resource, or a deletion receipt.

        Returns:
            None: Unknown deletion progress forces a fresh pass before further effects.
        """
        if not isinstance(result, dict) or not result.get("metadata", {}).get("uid") or intent.method == "DELETE":
            self.invalid = "dependency_write_requires_refresh"
            return
        kind, namespace, name = intent.target
        for observation in self.reads.values():
            if observation.api is not api or observation.key[:2] != (kind, namespace):
                continue
            if observation.key[2]:
                if observation.key[2] == name:
                    observation.expected = observation.snapshot(result)
                continue
            included = True
            for selector, expression in observation.query:
                if selector != "labelSelector" or expression.count("=") != 1 or "," in expression:
                    self.invalid = "dependency_selector_requires_refresh"
                    return
                label, value = expression.split("=", 1)
                included &= result.get("metadata", {}).get("labels", {}).get(label) == value
            if included:
                observation.expected[name] = resource_digest(result)
            else:
                observation.expected.pop(name, None)
        self.record(api, intent.target, None, result)

    async def refresh(self) -> None:
        """
        Publish coalesced owner hints through existing cluster queues without awaiting reconciliation.

        Returns:
            None: A publication failure leaves the originating delivery unacknowledged for retry.
        """
        if self.notified:
            return
        self.notified = True
        with without_capture():
            for api, keys in self.affected.items():
                callback = getattr(api, "on_write_drift", None)
                if callback is not None:
                    for key in sorted(keys):
                        try:
                            await callback(key)
                        except Exception:
                            logger.warning("Could not publish a targeted refresh; the failed reconciliation remains retryable.")


@contextmanager
def without_capture() -> Iterator[None]:
    """
    Keep validation and ownership reads from replacing a decision's expectations.

    Yields:
        None: Observation capture is disabled in this task until exit.
    """
    token = active_contract.set(None)
    try:
        yield
    finally:
        active_contract.reset(token)


@contextmanager
def capture_decision(api: API, key: Key) -> Iterator[ReadContract]:
    """
    Scope dependencies to one async reconciliation, sharing nested graph decisions.

    Args:
        api (API): Adapter executing the originating reconciliation.
        key (Key): Resource whose desired state must be retried after drift.

    Yields:
        ReadContract: Captured dependencies, isolated from unrelated async tasks.
    """
    contract = active_contract.get() or ReadContract()
    contract.owners(api, key, None)
    token = active_contract.set(contract)
    try:
        yield contract
    finally:
        active_contract.reset(token)


def expires_before(deadline: datetime) -> None:
    """
    Limit a captured decision to the validity of a TTL connection or throughput sample.

    Args:
        deadline (datetime): Timezone-aware expiry of a consumed decision input.

    Returns:
        None: Queueing and cached validation cannot extend an input's validity.
    """
    if deadline.tzinfo is None:
        raise ValueError("decision deadlines must include a timezone")
    contract = active_contract.get()
    if contract is not None:
        value = deadline.timestamp()
        contract.deadline = min(contract.deadline, value) if contract.deadline is not None else value
