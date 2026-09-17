"""
Project expiring exact-service grants into existing graph network contracts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from attrs import evolve

from polyad_types.codec import converter
from polyad_types.network import TrafficRule

if TYPE_CHECKING:
    from typing import Any

    from polyad_types.network import NetworkAccess

ANNOTATION = "polyad.astrivant.com/service-connections"


def grants(obj: dict[str, Any], *, active: bool = True) -> dict[str, dict[str, Any]]:
    """
    Read bounded service grants, excluding expired or replaced graph incarnations.

    Args:
        obj (dict[str, Any]): Exact service's graph boundary.
        active (bool): Exclude expired grants when compiling policies; false includes cleanup records.

    Returns:
        dict[str, dict[str, Any]]: Receipt-UID keyed service grants.
    """
    raw = obj["metadata"].get("annotations", {}).get(ANNOTATION, "{}")
    if len(raw.encode()) > 131072:
        raise ValueError("service connection grants exceed 128 KiB")
    entries = json.loads(raw)
    if not isinstance(entries, dict) or len(entries) > 128:
        raise ValueError("service connections require at most 128 grants per graph")
    if not active:
        return entries
    return {
        uid: grant
        for uid, grant in entries.items()
        if grant["graphUid"] == obj["metadata"]["uid"] and datetime.fromisoformat(grant["expiresAt"]) > datetime.now(UTC)
    }


def access(obj: dict[str, Any], base: NetworkAccess | None) -> NetworkAccess | None:
    """
    Add consented exceptions to the local graph contract while ancestor rules still intersect them.

    Args:
        obj (dict[str, Any]): Current graph carrying operator-owned service grants.
        base (NetworkAccess | None): Explicit graph network contract.

    Returns:
        NetworkAccess | None: Effective local contract; no grant can invent mesh or isolation enablement.
    """
    current = grants(obj)
    if not current:
        return base
    if base is None:
        raise ValueError("service connections require an explicit graph network contract")
    directions = {direction: list(getattr(base, direction)) for direction in ("ingress", "egress")}
    for grant in current.values():
        for direction in directions:
            directions[direction].extend(converter.structure(rule, TrafficRule) for rule in grant.get(direction, []))
    return evolve(base, ingress=tuple(directions["ingress"]), egress=tuple(directions["egress"]))
