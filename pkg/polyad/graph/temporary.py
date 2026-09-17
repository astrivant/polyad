"""
Describe expiring graph connections without changing a reusable graph definition.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

ANNOTATION = "polyad.astrivant.com/temporary-connections"
CLEANUP = "polyad.astrivant.com/connection-cleanup-pending"
MAX_CONNECTIONS = 128


def deadline(receipt: dict[str, Any]) -> datetime:
    """
    Derive expiry from the API server's immutable creation timestamp.

    Args:
        receipt (dict[str, Any]): Persisted TemporaryConnection resource.

    Returns:
        datetime: Absolute UTC expiration time.
    """
    created = datetime.fromisoformat(receipt["metadata"]["creationTimestamp"].replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("receipt creation time must include a timezone")
    return created + timedelta(seconds=receipt["spec"]["ttlSeconds"])


def entries(obj: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Read the bounded operator-owned grants stored on a graph instance.

    Args:
        obj (dict[str, Any]): Graph, PolyGraph or ReplicaGroup document.

    Returns:
        dict[str, dict[str, Any]]: Grants keyed by receipt UID, including expired grants awaiting cleanup.
    """
    raw = obj["metadata"].get("annotations", {}).get(ANNOTATION, "{}")
    if len(raw.encode()) > 131072:
        raise ValueError("temporary connection annotations exceed 128 KiB")
    result = json.loads(raw)
    if not isinstance(result, dict) or len(result) > MAX_CONNECTIONS or any(not isinstance(value, dict) for value in result.values()):
        raise ValueError("temporary connection annotations must contain at most 128 grants")
    return result


def active_entries(obj: dict[str, Any], *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """
    Exclude expired grants even before the next cleanup reconciliation.

    Args:
        obj (dict[str, Any]): Persisted graph instance.
        now (datetime | None): Injected clock for deterministic validation.

    Returns:
        dict[str, dict[str, Any]]: Grants that have not reached their immutable deadline.
    """
    current = now or datetime.now(UTC)
    return {
        uid: grant
        for uid, grant in entries(obj).items()
        if grant.get("graphUid") == obj["metadata"]["uid"] and datetime.fromisoformat(grant["expiresAt"]) > current
    }


def overlay(obj: dict[str, Any], spec: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """
    Merge live temporary edges into one instance's topology while preserving static edges.

    Args:
        obj (dict[str, Any]): Graph instance carrying admitted grants.
        spec (dict[str, Any]): Original or effective replica specification.
        now (datetime | None): Optional clock used to select live grants.

    Returns:
        dict[str, Any]: Topology with active edges whose two endpoints still exist.
    """
    grants = active_entries(obj, now=now)
    if not grants:
        return spec
    if obj["kind"] == "ReplicaGroup" and "template" in spec:
        from polyad_types.replication import replica_topology

        spec = replica_topology(spec)
    result = copy.deepcopy(spec)
    names = {node["name"] for node in result.get("nodes", [])}
    connections = list(result.get("connections", []))
    for grant in grants.values():
        if grant["source"] not in names or grant["target"] not in names:
            continue
        edge = {key: grant[key] for key in ("source", "target", "ports")}
        connections.append(edge)
        if grant.get("bidirectional", False):
            connections.append({**edge, "source": edge["target"], "target": edge["source"]})
    unique: dict[str, dict[str, Any]] = {}
    for edge in connections:
        ports = sorted({(port.get("protocol", "TCP"), port["port"]) for port in edge.get("ports", [])})
        key = json.dumps({**edge, "ports": ports}, sort_keys=True)
        unique.setdefault(key, edge)
    result["connections"] = list(unique.values())
    return result
