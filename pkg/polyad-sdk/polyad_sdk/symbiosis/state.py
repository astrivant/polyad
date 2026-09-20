"""
Project authorized events into bounded neighborhood state and stable delta paths.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from polyad_sdk.symbiosis.models import Environment, freeze
from polyad_types.events.codec import CURSOR_PATTERN
from polyad_types.events.models import ConnectionEvent, GraphEvent

if TYPE_CHECKING:
    from typing import Any

    from polyad_sdk.symbiosis.models import Settings
    from polyad_types.api.discovery import ServiceEndpoint
    from polyad_types.events.envelope import Event


class State:
    """
    Retain only this node's observed neighborhood and public policy data.
    """

    def __init__(self, identity: ServiceEndpoint, settings: Settings) -> None:
        """
        Initialize one bounded graph-node observation cache.

        Args:
            identity (ServiceEndpoint): Exact application boundary and node.
            settings (Settings): Freshness and inventory budgets.
        """
        self.identity, self.settings = identity, settings
        self.topology: Mapping[str, Any] | None = None
        self.allowed: dict[str, tuple[str, str, str, str]] = {}
        self.observations: dict[str, tuple[float, Mapping[str, Any]]] = {}
        self.connections: dict[str, tuple[float, Mapping[str, Any]]] = {}
        self.reason: str | None = "initial snapshot required"
        self.refreshed = 0.0

    def load(self, snapshot: dict[str, Any], now: float, *, reset: bool = False) -> None:
        """
        Validate a selected-node snapshot against the configured incarnation and inventory budget.

        Args:
            snapshot (dict[str, Any]): Response from the pinned events client's topology endpoint.
            now (float): Current application time.
            reset (bool): Discard observations and receipts after a replay gap.

        Returns:
            None: Replace topology only after complete validation.
        """
        identity = self.identity
        graph = snapshot["graph"]
        if any(
            graph[key] != value
            for key, value in (
                ("name", identity.graph),
                ("uid", identity.graphUid),
                ("namespace", identity.namespace),
                ("kind", identity.kind),
            )
        ) or (graph.get("cluster") and identity.cluster and graph["cluster"] != identity.cluster):
            raise ValueError("topology identity differs from the configured graph incarnation")
        if not isinstance(snapshot["cursor"], str) or not re.fullmatch(CURSOR_PATTERN, snapshot["cursor"]):
            raise ValueError("topology requires a valid replay cursor")
        if not isinstance(snapshot["revision"], str) or not snapshot["revision"]:
            raise ValueError("topology requires a structural revision")
        timestamp = snapshot["observedAt"]
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
            raise ValueError("topology requires a finite observation timestamp")
        for key in ("valid", "terminating", "templateOnly"):
            if type(snapshot[key]) is not bool:
                raise ValueError("topology lifecycle fields must be booleans")
        if snapshot["node"]["name"] != identity.node:
            raise ValueError("topology selected a different logical node")
        nodes = [snapshot["node"]]
        for direction in ("incoming", "outgoing", "dependencies", "dependents"):
            nodes.extend(item["node"] for item in snapshot[direction])
        allowed = {identity.graphUid: (identity.kind, identity.namespace, identity.graph, identity.cluster)}
        for node in nodes:
            if type(node["desired"]) is not bool:
                raise ValueError("node desired state must be boolean")
            for execution in node["executions"]:
                if type(execution["terminating"]) is not bool:
                    raise ValueError("execution termination state must be boolean")
                if "replicas" in execution and (type(execution["replicas"]) is not int or execution["replicas"] < 0):
                    raise ValueError("execution replicas must be a nonnegative integer")
                allowed[execution["uid"]] = (
                    execution["kind"],
                    execution.get("namespace", identity.namespace),
                    execution["name"],
                    execution.get("cluster", identity.cluster),
                )
        if len(allowed) > self.settings.max_observations:
            raise ValueError("neighborhood exceeds max_observations; narrow the boundary or raise its budget")
        if len(json.dumps(snapshot, allow_nan=False).encode()) > 4 * 1024 * 1024:
            raise ValueError("topology snapshot exceeds 4 MiB")
        self.topology = freeze(snapshot)
        self.allowed = allowed
        self.observations = {} if reset else {uid: value for uid, value in self.observations.items() if uid in allowed}
        self.connections = {} if reset else self.connections
        self.refreshed, self.reason = now, None

    def accept(self, event: Event, now: float) -> None:
        """
        Retain typed observations only for known incarnations or this endpoint's receipts.

        Args:
            event (Event): Validated transport event from the configured stream.
            now (float): Application receipt time, bounding cached metrics lifetime.

        Returns:
            None: Unrelated authorized events do not enlarge this view.
        """
        typed = event.typed()
        if isinstance(typed, GraphEvent):
            data = typed.data
            expected = self.allowed.get(data.uid)
            if expected is None or (data.kind, data.namespace, data.name) != expected[:3] or (expected[3] and data.cluster != expected[3]):
                return
            previous = self.observations.get(data.uid)
            if previous is not None and data.generation < previous[1]["generation"]:
                return
            self.observations[data.uid] = now, freeze(event.data)
            if data.uid == self.identity.graphUid and data.type == "deleting":
                self.reason = "containing graph is deleting"
        elif isinstance(typed, ConnectionEvent):
            connection = typed.data
            home = self.identity
            if (connection.graph.kind, connection.graph.namespace, connection.graph.name, connection.graph.uid) != (
                home.kind,
                home.namespace,
                home.graph,
                home.graphUid,
            ):
                return
            if home.cluster and connection.graph.cluster != home.cluster:
                return
            receipt = connection.connection
            if receipt.peers:
                peer = receipt.peers.get(connection.participant or "")
                if (
                    peer is None
                    or (peer.namespace, peer.kind, peer.graph, peer.graphUid, peer.node)
                    != (home.namespace, home.kind, home.graph, home.graphUid, home.node)
                    or (home.cluster and peer.cluster != home.cluster)
                ):
                    return
            elif home.node not in (receipt.target.source, receipt.target.target):
                return
            expiry = datetime.fromisoformat(receipt.expiresAt.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError("connection expiry requires a timezone")
            self.connections = {uid: item for uid, item in self.connections.items() if item[0] > now}
            if (
                expiry.timestamp() <= now
                or receipt.revokeRequested
                or receipt.status.get("phase") in {"Expired", "Revoked", "Rejected", "Failed"}
            ):
                self.connections.pop(receipt.uid, None)
                return
            if receipt.uid not in self.connections and len(self.connections) >= self.settings.max_connections:
                raise ValueError("connection inventory exceeds max_connections")
            self.connections[receipt.uid] = expiry.timestamp(), freeze(event.data["connection"])

    def view(self, now: float) -> Environment:
        """
        Apply freshness and expiry at read time without claiming empty topology on failures.

        Args:
            now (float): Current application time.

        Returns:
            Environment: Independent immutable view, with unavailable context explicitly identified.
        """

        # Freshness is evaluated when the application asks for a view, so silence
        # can make old evidence unavailable even without a new stream event.
        topology = self.topology
        reason = self.reason
        if topology is None:
            reason = reason or "initial snapshot required"
        elif not 0 <= now - topology["observedAt"] <= self.settings.max_age_seconds:
            reason = reason or "topology observation is stale"
        elif not topology["valid"] or topology["templateOnly"] or topology["terminating"] or not topology["node"]["desired"]:
            reason = reason or "topology does not admit new assignments"

        # Expire observations and temporary connections independently. Keep their
        # absence explicit instead of representing a failed stream as an empty graph.
        observations = {
            uid: item for uid, (received, item) in self.observations.items() if 0 <= now - received <= self.settings.max_age_seconds
        }
        connections = {uid: item for uid, (expiry, item) in self.connections.items() if expiry > now}
        return Environment(topology, freeze(observations), freeze(connections), reason is None, reason)


def normalized(value: Any) -> Any:
    """
    Key known record collections by stable identities instead of positional offsets.

    Args:
        value (Any): Immutable or ordinary JSON observation.

    Returns:
        Any: Comparison projection retaining actual values and stable record keys.
    """

    # Heartbeat timestamps should not trigger adaptations. Stable identity keys
    # also prevent harmless ordering changes from looking like topology churn.
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"observedAt", "metricsObservedAt"}:
                continue
            if (
                key == "executions"
                and isinstance(item, (list, tuple))
                and all(isinstance(entry, Mapping) and "uid" in entry for entry in item)
            ):
                result[key] = {entry["uid"]: normalized(entry) for entry in item}
            elif (
                key == "requires"
                and isinstance(item, (list, tuple))
                and all(isinstance(entry, Mapping) and "node" in entry for entry in item)
            ):
                result[key] = {entry["node"]: normalized(entry) for entry in item}
            elif (
                key == "ports" and isinstance(item, (list, tuple)) and all(isinstance(entry, Mapping) and "port" in entry for entry in item)
            ):
                result[key] = tuple(sorted((entry["port"], entry.get("protocol", "TCP")) for entry in item))
            else:
                result[key] = normalized(item)
        return result
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, Mapping) for item in value):
            for key in ("uid", "target", "name", "route"):
                identities = [item.get(key) for item in value]
                if all(isinstance(item, str) for item in identities) and len(set(identities)) == len(value):
                    return {item[key]: normalized(item) for item in value}
        return tuple(normalized(item) for item in value)
    return value


def projection(view: Environment) -> dict[str, Any]:
    """
    Compare useful structure, resources, decisions and consent without heartbeat churn.

    Args:
        view (Environment): Application state at one observation point.

    Returns:
        dict[str, Any]: Stable comparison fields; missing metrics remain missing.
    """

    # Compare the application's decision inputs, not every transport field. This
    # is the projection used to decide whether a strategy callback needs to run.
    result: dict[str, Any] = {"available": view.available, "reason": view.reason}
    if view.topology is not None:
        topology = view.topology
        result["topology"] = {key: normalized(topology[key]) for key in ("valid", "templateOnly", "terminating", "node")}
        for direction in ("incoming", "outgoing", "dependencies", "dependents"):
            result["topology"][direction] = {item["node"]["name"]: normalized(item) for item in topology[direction]}
    result["observations"] = {
        uid: normalized({key: data[key] for key in ("type", "generation", "status", "resources")})
        for uid, data in view.observations.items()
    }
    if view.resources is not None:
        result["resources"] = normalized(view.resources)
    if view.decision is not None:
        result["decision"] = normalized({key: value for key, value in view.decision.items() if key not in {"observedAt"}})
    result["connections"] = normalized(view.connections)
    return result
