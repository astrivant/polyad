"""
Resolve exact endpoint ancestry without widening or forwarding a service request.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from polyad.events.visibility import public_observation
from polyad.exceptions.api import Conflict, Forbidden, Unavailable
from polyad.operator.clusters.federation import INVENTORY, PARENT
from polyad_types.graphs.topology import topology
from polyad_types.resources import BOUNDARY_KINDS, GROUP, VERSION

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad_types.api.discovery import ServiceEndpoint

__all__ = (
    "identities",
    "path",
)


async def path(
    peer: ServiceEndpoint,
    resolve: Callable[[str], tuple[API, str]],
    local_cluster: str,
) -> list[tuple[API, dict[str, Any], str, str]]:
    """
    Read the endpoint and every locally or remotely owned enclosing boundary.

    Args:
        peer (ServiceEndpoint): UID-fenced service address.
        resolve (Callable[[str], tuple[API, str]]): Administrator-controlled transport resolver.
        local_cluster (str): Name substituted for an omitted local cluster.

    Returns:
        list[tuple[API, dict[str, Any], str, str]]: Adapter, boundary document, branch node and cluster, starting at the service.
    """
    cluster = peer.cluster or local_cluster
    api, namespace = resolve(cluster)
    if namespace != peer.namespace:
        raise Forbidden("endpoint namespace is outside this operator's registered scope")
    current = await api.get(peer.kind, namespace, peer.graph)
    if current is None or current["metadata"]["uid"] != peer.graphUid:
        raise Conflict("endpoint graph is absent or replaced")
    branch, seen, result = peer.node, set(), []

    # Walk verified graph incarnations with a hard bound, carrying the local branch at each ancestor.
    for _ in range(64):
        meta = current["metadata"]
        identity = (cluster, meta["uid"])
        if identity in seen or meta.get("deletionTimestamp") or current["spec"].get("templateOnly") or current["spec"].get("suspend"):
            raise Conflict("endpoint graph ancestry is cyclic, deleting or unavailable")
        if current.get("status", {}).get("phase") == "Stopped":
            raise Conflict("endpoint graph is stopped")
        seen.add(identity)
        spec = current["spec"]
        if current["kind"] == "ReplicaGroup":
            from polyad.operator.reconciliation.replication import effective_spec

            spec, _ = await effective_spec(api, current)
        if branch not in {node.name for node in topology(spec, current["kind"]).nodes}:
            raise Conflict("endpoint or enclosing branch no longer exists")
        if not await public_observation(api, current):
            raise Forbidden("application connections cannot address internal operator graphs")
        result.append((api, current, branch, cluster))
        remote = meta.get("annotations", {}).get(PARENT)
        if remote:
            if meta.get("ownerReferences"):
                raise Conflict("remote endpoint also has local owners")
            parent_cluster, parent_namespace, kind, name, uid, branch = json.loads(remote)
            parent_api, registered = resolve(parent_cluster)
            if registered != parent_namespace or kind not in BOUNDARY_KINDS:
                raise Forbidden("remote parent leaves registered graph scope")
            parent = await parent_api.get(kind, parent_namespace, name)
            expected = {"cluster": cluster, "namespace": namespace, "kind": current["kind"], "name": meta["name"], "node": branch}
            if (
                parent is None
                or parent["metadata"]["uid"] != uid
                or expected not in json.loads(parent["metadata"].get("annotations", {}).get(INVENTORY, "[]"))
            ):
                raise Conflict("remote parent no longer owns this endpoint branch")
            api, namespace, cluster, current = parent_api, parent_namespace, parent_cluster, parent
            continue
        owners = [owner for owner in meta.get("ownerReferences", []) if owner.get("controller") and owner.get("kind") in BOUNDARY_KINDS]
        if not owners:
            return result
        if len(owners) != 1 or owners[0].get("apiVersion") != f"{GROUP}/{VERSION}":
            raise Conflict("endpoint graph has ambiguous ownership")
        owner = owners[0]
        parent = await api.get(owner["kind"], namespace, owner["name"])
        if parent is None or parent["metadata"]["uid"] != owner["uid"]:
            raise Conflict("endpoint parent was replaced")
        branch, current = meta.get("labels", {}).get(f"{GROUP}/node", ""), parent
    raise Unavailable("endpoint ancestry exceeds the supported depth")


def identities(path: list[tuple[API, dict[str, Any], str, str]]) -> list[dict[str, str]]:
    """
    Project verified paths into access-policy identities.

    Args:
        path (list[tuple[API, dict[str, Any], str, str]]): Fresh endpoint ancestry.

    Returns:
        list[dict[str, str]]: Service graph followed by its enclosing identities.
    """
    return [
        {"cluster": cluster, "kind": obj["kind"], **{key: obj["metadata"][key] for key in ("namespace", "name", "uid")}}
        for _, obj, _, cluster in path
    ]
