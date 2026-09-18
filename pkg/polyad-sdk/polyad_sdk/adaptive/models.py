"""
Expose immutable application views and changes without importing operator code.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Literal

    from polyad_types.events import Event


def freeze(value: Any) -> Any:
    """
    Copy JSON into recursively read-only mappings and tuples.

    Args:
        value (Any): JSON-compatible observation.

    Returns:
        Any: Independent immutable view.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class Settings:
    """
    Bound application observation freshness, memory and transport behavior.

    Attributes:
        refresh_seconds (float): Interval between topology reads while receiving heartbeats or observations.
        max_age_seconds (float): Maximum age of topology and cached resource observations.
        max_observations (int): Maximum resource identities retained around this node.
        max_connections (int): Maximum unexpired consent receipts retained for this node.
        transport (Literal['sse', 'websocket']): Event transport enabled by the operator.
        rebalance (bool): Use configured operator endpoint discovery and managed reconnection.
    """

    refresh_seconds: float = 10
    max_age_seconds: float = 30
    max_observations: int = 512
    max_connections: int = 256
    transport: Literal["sse", "websocket"] = "sse"
    rebalance: bool = False

    def __post_init__(self) -> None:
        """
        Reject unbounded inventories and invalid observation intervals.

        Returns:
            None: Invalid settings fail before opening a stream.
        """
        for value in (self.refresh_seconds, self.max_age_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0.1 <= value <= 300:
                raise ValueError("observation intervals must be finite and between 0.1 and 300 seconds")
        if self.refresh_seconds > self.max_age_seconds:
            raise ValueError("refresh interval cannot exceed observation lifetime")
        for value in (self.max_observations, self.max_connections):
            if type(value) is not int or not 1 <= value <= 4096:
                raise ValueError("observation inventories must be integers from 1 through 4096")
        if self.transport not in {"sse", "websocket"} or type(self.rebalance) is not bool:
            raise ValueError("use sse or websocket and a boolean rebalance switch")


@dataclass(frozen=True)
class Delta:
    """
    Describe one change with identity-based paths and explicit value presence.

    Attributes:
        path (tuple[str, ...]): Field path, using neighbor names, execution UIDs and route targets instead of list offsets.
        kind (Literal['added', 'removed', 'changed']): Whether the field appeared, disappeared or changed.
        before (Any): Previous immutable value; kind distinguishes absence from JSON null.
        after (Any): Current immutable value; kind distinguishes absence from JSON null.
    """

    path: tuple[str, ...]
    kind: Literal["added", "removed", "changed"]
    before: Any
    after: Any

    @property
    def difference(self) -> float | None:
        """
        Return a numeric change only when both observations contain finite numbers.

        Returns:
            float | None: After minus before; missing values, booleans and nonnumeric values have no numeric difference.
        """
        if self.kind != "changed" or any(type(value) not in (int, float) for value in (self.before, self.after)):
            return None
        try:
            result = float(self.after - self.before)
        except OverflowError:
            return None
        return result if math.isfinite(result) else None


@dataclass(frozen=True)
class Environment:
    """
    Expose the latest authorized neighborhood and independently fresh observations.

    Attributes:
        topology (Mapping[str, Any] | None): Selected node snapshot; absent before the initial read.
        observations (Mapping[str, Mapping[str, Any]]): Fresh graph/resource observations indexed by exact UID.
        connections (Mapping[str, Mapping[str, Any]]): Unexpired receipts observed by this SDK instance, indexed by UID.
        available (bool): Whether current topology permits considering new routing assignments.
        reason (str | None): Why topology is unavailable or stale.
    """

    topology: Mapping[str, Any] | None
    observations: Mapping[str, Mapping[str, Any]]
    connections: Mapping[str, Mapping[str, Any]]
    available: bool
    reason: str | None

    @property
    def resources(self) -> Mapping[str, Any] | None:
        """
        Read resource-count metrics for the containing graph when observed.

        Returns:
            Mapping[str, Any] | None: Authorized metrics, or None before observation or after expiry.
        """
        observation = self.observations.get(self.topology["graph"]["uid"]) if self.topology else None
        return observation["resources"] if observation else None

    @property
    def decision(self) -> Mapping[str, Any] | None:
        """
        Read the containing graph's Soul searching measurements and decision.

        Returns:
            Mapping[str, Any] | None: Public throughput status, retaining phase and current/proposed distinctions.
        """
        observation = self.observations.get(self.topology["graph"]["uid"]) if self.topology else None
        return observation["status"].get("throughput") if observation else None

    @property
    def candidates(self) -> tuple[Mapping[str, Any], ...]:
        """
        Select outgoing peers with live executions for application readiness checks.

        Returns:
            tuple[Mapping[str, Any], ...]: Potential destinations; application compatibility, readiness and admission still apply.
        """
        if not self.available or self.topology is None:
            return ()
        return tuple(
            neighbor
            for neighbor in self.topology["outgoing"]
            if neighbor["node"]["desired"]
            and any(not item["terminating"] and item.get("replicas", 1) > 0 for item in neighbor["node"]["executions"])
        )


@dataclass(frozen=True)
class Change:
    """
    Deliver deltas together with the before/after context needed to act on them.

    Attributes:
        before (Environment): Previous published application view.
        after (Environment): Current application view.
        deltas (tuple[Delta, ...]): Meaningful changes, excluding stream cursors and heartbeat revisions.
        baseline (bool): Initial snapshot or explicit replay reset; no change from an invented empty graph.
        event (Event | None): Triggering observation, or None for refresh/reset/availability updates.
    """

    before: Environment
    after: Environment
    deltas: tuple[Delta, ...]
    baseline: bool
    event: Event | None = None

    def matching(self, prefix: str) -> tuple[Delta, ...]:
        """
        Select changes under a dot-separated field prefix.

        Args:
            prefix (str): Prefix such as topology.outgoing, resources, decision or connections.

        Returns:
            tuple[Delta, ...]: Deltas below the prefix, including additions/removals of its containing object.
        """
        parts = tuple(prefix.split("."))
        return tuple(
            delta
            for delta in self.deltas
            if delta.path[: len(parts)] == parts or (delta.kind in {"added", "removed"} and parts[: len(delta.path)] == delta.path)
        )


def differences(before: Mapping[str, Any], after: Mapping[str, Any], path: tuple[str, ...] = ()) -> tuple[Delta, ...]:
    """
    Compare keyed observations without treating missing numbers as zero.

    Args:
        before (Mapping[str, Any]): Earlier normalized view.
        after (Mapping[str, Any]): Later normalized view.
        path (tuple[str, ...]): Current recursive field prefix.

    Returns:
        tuple[Delta, ...]: Deterministically ordered, immutable changes.
    """
    result = []
    for key in sorted(before.keys() | after.keys()):
        current = (*path, key)
        if key not in before:
            result.append(Delta(current, "added", None, freeze(after[key])))
        elif key not in after:
            result.append(Delta(current, "removed", freeze(before[key]), None))
        elif isinstance(before[key], Mapping) and isinstance(after[key], Mapping):
            result.extend(differences(before[key], after[key], current))
        elif type(before[key]) is not type(after[key]) or before[key] != after[key]:
            result.append(Delta(current, "changed", freeze(before[key]), freeze(after[key])))
    return tuple(result)
