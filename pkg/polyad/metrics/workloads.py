"""
Select finite workload signals from fresh inventory without contacting the cluster.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

__all__ = (
    "GROUP_SIGNALS",
    "current_observation",
    "observation_time",
    "workload_metric",
)


GROUP_SIGNALS = ("replicas", "desiredReplicas", "readyReplicas", "totalReplicas", "instanceCount")


def observation_time(previous: str | None) -> str:
    """
    Refresh observation heartbeats at most once per ten seconds to avoid watch feedback loops.

    Args:
        previous (str | None): Persisted observation heartbeat.

    Returns:
        str: Previous heartbeat or a new UTC timestamp.
    """
    now = datetime.now(UTC)
    try:
        if previous and 0 <= (now - datetime.fromisoformat(previous)).total_seconds() < 10:
            return previous
    except (ValueError, TypeError):
        pass
    return now.isoformat()


def current_observation(timestamp: str | None) -> bool:
    """
    Reject expired or invalid controller observations independently of inventory freshness.

    Args:
        timestamp (str | None): UTC controller observation time.

    Returns:
        bool: Whether the controller observed the signal within thirty seconds.
    """
    try:
        return timestamp is not None and 0 <= (datetime.now(UTC) - datetime.fromisoformat(timestamp)).total_seconds() < 30
    except (ValueError, TypeError):
        return False


def workload_metric(snapshot: dict[str, Any], kind: str, name: str, metric: str, node: str | None = None) -> dict[str, Any]:
    """
    Return one scalar suitable for KEDA, failing closed for stale or incomplete observations.

    Args:
        snapshot (dict[str, Any]): Cached namespace observation.
        kind (str): Instance boundary or reusable definition kind.
        name (str): Resource name in the operator namespace.
        metric (str): Explicit numeric scheduler signal.
        node (str | None): Optional logical node within a boundary.

    Returns:
        dict[str, Any]: Numeric value and exact observation scope.

    Raises:
        KeyError: The resource, node or metric is absent.
        ValueError: A required observation is stale or incomplete.
    """
    inventory = snapshot["inventory"]

    # Unknown demand must not look like zero demand to an autoscaler considering scale-down.
    if not inventory["fresh"]:
        raise ValueError("namespace inventory is stale")
    records = inventory["objects"]
    target = next((obj for obj in records if obj["kind"] == kind and obj["name"] == name), None)
    if target is None or target["terminating"]:
        raise KeyError("workload is absent or deleting")
    values = []
    if kind == "ReplicaGroup" and node is None and metric in GROUP_SIGNALS:
        scale = target.get("scaling") or {}
        source = scale.get("source")
        if source:
            definition = next((obj for obj in records if obj["kind"] == "ReplicaGroup" and obj["uid"] == source["uid"]), None)
            if (
                definition is None
                or definition["generation"] != scale.get("sourceGeneration")
                or (definition.get("scaling") or {}).get("remoteScaleRevision", "") != (scale.get("sourceRemoteScaleRevision") or "")
            ):
                raise ValueError("replica source observation is stale")
        if not scale.get("current") or not current_observation(scale.get("observedAt")):
            raise ValueError("replication observation is stale")
        values = [scale[metric]]
    elif node is None and metric in target.get("boundarySignals", {}):
        if (
            not target["statusCurrent"]
            or not target["hierarchyComplete"]
            or not (target.get("rollup") or {}).get("observationsComplete")
            or not current_observation(target.get("metricsObservedAt"))
        ):
            raise ValueError("boundary observation is stale or incomplete")
        values = [target["boundarySignals"][metric]]
    else:
        if target["role"] == "definition":
            for obj in records:
                if {"kind": kind, "name": name} in obj.get("uses", []):
                    if not obj["statusCurrent"] or not current_observation(obj.get("workloadsObservedAt")):
                        raise ValueError("a use of this definition has no fresh observation")
                    if not any(entry["definitionUid"] == target["uid"] for entry in (obj.get("workloads") or {}).values()):
                        if obj["kind"] != "ReplicaGroup" or (obj.get("scaling") or {}).get("desiredReplicas") != 0:
                            raise ValueError("definition incarnation has not been observed by every use")
            matches = [
                (obj, entry)
                for obj in records
                for entry in (obj.get("workloads") or {}).values()
                if entry["definitionUid"] == target["uid"] and entry["kind"] == kind
            ]
            if not matches:
                raise KeyError("no observed uses of this definition")
        else:
            entries = target.get("workloads") or {}
            if node is not None:
                if node not in entries:
                    raise KeyError("logical node has no observation")
                entries = {node: entries[node]}
            matches = [(target, entry) for entry in entries.values()]
            if not matches:
                raise KeyError("no workload observations")
        for obj, entry in matches:
            if (
                not obj["statusCurrent"]
                or not obj["hierarchyComplete"]
                or obj["terminating"]
                or not current_observation(obj.get("workloadsObservedAt"))
            ):
                raise ValueError("workload observation is stale")
            scale = obj.get("scaling") or {}
            source = scale.get("source")
            if source and not any(
                record["uid"] == source["uid"]
                and record["generation"] == scale.get("sourceGeneration")
                and (record.get("scaling") or {}).get("remoteScaleRevision", "") == (scale.get("sourceRemoteScaleRevision") or "")
                for record in records
            ):
                raise ValueError("replica source observation is stale")
            if metric not in entry["values"]:
                raise KeyError("unknown workload metric")
            values.append(entry["values"][metric])

    # Validate every contribution before summing; one invalid source invalidates the aggregate.
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        raise ValueError("workload signal is not a finite number")
    return {
        "value": sum(values),
        "namespace": snapshot["namespace"],
        "uid": target["uid"],
        "generation": target["generation"],
        "kind": kind,
        "name": name,
        "node": node,
        "metric": metric,
        "fresh": True,
    }
