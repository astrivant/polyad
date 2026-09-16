"""
Keep operator graph families out of workload-facing observation streams.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_types.resources import BOUNDARY_KINDS, GROUP

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.api import API

INTERNAL = f"{GROUP}/internal"


async def public_observation(api: API, obj: dict[str, Any], *, reserved_graph: tuple[str, str] | None = None) -> bool:
    """
    Verify that an observation and its graph ancestry belong to application work.

    Args:
        api (API): Fresh reader for the observation's cluster.
        obj (dict[str, Any]): Observed resource, including ownership and request targets.
        reserved_graph (tuple[str, str] | None): Local operator Graph namespace and name.

    Returns:
        bool: False for internal resources or unresolved, replaced or cyclic ancestry.
    """
    remaining = 64

    async def visit(current: dict[str, Any], path: frozenset[tuple[str, str, str, str]]) -> bool:
        nonlocal remaining
        meta = current["metadata"]
        kind, namespace, name, uid = current["kind"], meta["namespace"], meta["name"], meta["uid"]
        identity = kind, namespace, name, uid
        if remaining == 0 or identity in path:
            return False
        remaining -= 1
        if meta.get("labels", {}).get(INTERNAL) == "true" or (kind == "Graph" and (namespace, name) == reserved_graph):
            return False
        references = {
            (owner["kind"], owner["name"], owner["uid"])
            for owner in meta.get("ownerReferences", [])
            if owner.get("apiVersion", "").startswith(f"{GROUP}/") and owner.get("kind") in {*BOUNDARY_KINDS, "Composition"}
        }
        if kind in {"Rewrite", "Activation", "TemporaryConnection"}:
            spec = current.get("spec", {})
            target_kind, target_name = spec.get("kind", "Graph"), spec.get("graph")
            if target_kind not in BOUNDARY_KINDS or not target_name:
                return False
            references.add((target_kind, target_name, spec.get("graphUid", "")))
        for parent_kind, parent_name, parent_uid in references:
            if parent_kind == "Graph" and (namespace, parent_name) == reserved_graph:
                return False
            parent = await api.get(parent_kind, namespace, parent_name)
            if parent is None or (parent_uid and parent["metadata"]["uid"] != parent_uid):
                return False
            if not await visit(parent, path | {identity}):
                return False
        return True

    return await visit(obj, frozenset())
