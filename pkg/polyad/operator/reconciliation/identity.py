"""
Resolve workload ancestry from current Kubernetes ownership references.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_types.resources import BOUNDARY_KINDS, GROUP, VERSION

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API

__all__ = ("graph_ancestry",)


async def graph_ancestry(api: API, graph: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Walk namespace-local graph owners, checking every incarnation before admission.

    Args:
        api (API): Kubernetes reader used within the graph's owned reconciliation pass.
        graph (dict[str, Any]): Refreshed containing graph instance.

    Returns:
        list[dict[str, Any]]: Root-to-leaf chain including the containing graph.
    """
    from polyad.operator.reconciliation.controller import Pending

    chain = []
    seen = set()
    current = graph

    # Verify each owner UID before exporting ancestry; a reused resource name is a different identity.
    for _ in range(32):
        meta = current["metadata"]
        if meta["uid"] in seen:
            raise ValueError("cyclic graph ownership")
        seen.add(meta["uid"])
        chain.append(current)
        owners = [
            owner
            for owner in meta.get("ownerReferences", [])
            if owner.get("controller") and owner.get("apiVersion") == f"{GROUP}/{VERSION}" and owner.get("kind") in BOUNDARY_KINDS
        ]
        if not owners:
            return list(reversed(chain))
        if len(owners) != 1:
            raise ValueError("graph identity requires exactly one controlling graph owner")
        owner = owners[0]
        parent = await api.get(owner["kind"], meta["namespace"], owner["name"])
        if parent is None or parent["metadata"]["uid"] != owner["uid"] or parent["metadata"].get("deletionTimestamp"):
            raise Pending("waiting for the current graph owner before compiling workload identity")
        current = parent
    raise ValueError("graph identity ancestry exceeds 32 boundaries")
