"""
Keep operator graph families out of workload-facing observation streams.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from polyad_types.resources import BOUNDARY_KINDS, GROUP

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad_types.api.auth import GraphAccess

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
        if meta.get("labels", {}).get(INTERNAL) == "true" or (kind in BOUNDARY_KINDS and (namespace, name) == reserved_graph):
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
            if parent_kind in BOUNDARY_KINDS and (namespace, parent_name) == reserved_graph:
                return False
            parent = await api.get(parent_kind, namespace, parent_name)
            if parent is None or (parent_uid and parent["metadata"]["uid"] != parent_uid):
                return False
            if not await visit(parent, path | {identity}):
                return False
        return True

    return await visit(obj, frozenset())


async def observation_ancestry(
    api: API, obj: dict[str, Any], *, cluster: str = "", resolve: Callable[[str], tuple[API, str]] | None = None
) -> list[dict[str, str]]:
    """
    Record verified graph parents for authorization of retained observations.

    Args:
        api (API): Fresh reader for this observation's cluster.
        obj (dict[str, Any]): Observed graph, workload or request.
        cluster (str): Cluster identity for this observation and its local ancestors.
        resolve (Callable[[str], tuple[API, str]] | None): Optional root registry for verified cross-cluster parents.

    Returns:
        list[dict[str, str]]: Bounded graph identities with verified owner UIDs.
    """
    result: list[dict[str, str]] = []
    pending = [(api, cluster, obj)]
    seen: set[str] = set()
    while pending:
        current_api, current_cluster, current = pending.pop()
        meta = current["metadata"]
        if meta["uid"] in seen or len(seen) >= 64:
            raise ValueError("unresolved or cyclic event ancestry")
        seen.add(meta["uid"])
        references = [
            owner
            for owner in meta.get("ownerReferences", [])
            if owner.get("apiVersion", "").startswith(f"{GROUP}/") and owner.get("kind") in BOUNDARY_KINDS
        ]
        if current["kind"] in {"Rewrite", "Activation", "TemporaryConnection"}:
            spec = current["spec"]
            references.append({"kind": spec.get("kind", "Graph"), "name": spec["graph"], "uid": spec.get("graphUid", "")})
        remote_parent = meta.get("annotations", {}).get(f"{GROUP}/remote-parent")
        if remote_parent and resolve is not None:
            parent_cluster, parent_namespace, parent_kind, parent_name, parent_uid, node = json.loads(remote_parent)
            parent_api, registered_namespace = resolve(parent_cluster)
            if registered_namespace != parent_namespace or parent_kind not in BOUNDARY_KINDS:
                raise ValueError("remote event ancestry leaves registered graph boundaries")
            parent = await parent_api.get(parent_kind, parent_namespace, parent_name)
            expected = {
                "cluster": current_cluster,
                "namespace": meta["namespace"],
                "kind": current["kind"],
                "name": meta["name"],
                "node": node,
            }
            if (
                parent is None
                or parent["metadata"]["uid"] != parent_uid
                or expected not in json.loads(parent["metadata"].get("annotations", {}).get(f"{GROUP}/remote-children", "[]"))
            ):
                raise ValueError("remote event parent does not own this child")
            if parent["metadata"].get("labels", {}).get(INTERNAL) == "true":
                raise ValueError("internal operator ancestry cannot enter application streams")
            result.append(
                {"cluster": parent_cluster, "namespace": parent_namespace, "kind": parent_kind, "name": parent_name, "uid": parent_uid}
            )
            pending.append((parent_api, parent_cluster, parent))
        for reference in references:
            parent = await current_api.get(reference["kind"], meta["namespace"], reference["name"])
            if parent is None or (reference["uid"] and parent["metadata"]["uid"] != reference["uid"]):
                raise ValueError("event ancestor is absent or replaced")
            identity = {
                "cluster": current_cluster,
                "kind": parent["kind"],
                **{key: parent["metadata"][key] for key in ("namespace", "name", "uid")},
            }
            if identity not in result:
                result.append(identity)
                pending.append((current_api, current_cluster, parent))
    return result


def permitted_observation(identity: dict[str, Any], ancestry: list[dict[str, Any]], scopes: tuple[GraphAccess, ...]) -> bool:
    """
    Restrict named credentials to explicitly assigned graph trees.

    Args:
        identity (dict[str, Any]): Event or snapshot graph identity.
        ancestry (list[dict[str, Any]]): Verified ancestry at publication time.
        scopes (tuple[GraphAccess, ...]): Administrator-declared graph grants.

    Returns:
        bool: Whether a matching direct or inherited grant permits this observation.
    """
    cluster = identity.get("cluster", os.environ.get("POLYAD_CLUSTER_NAME", ""))
    for scope in scopes:
        for candidate in [identity, *ancestry] if scope.descendants else [identity]:
            if (
                (candidate.get("cluster") or cluster) == (scope.cluster or os.environ.get("POLYAD_CLUSTER_NAME", ""))
                and candidate.get("kind") == scope.kind
                and candidate.get("name") == scope.name
                and candidate.get("namespace") == scope.namespace
                and (not scope.uid or candidate.get("uid") == scope.uid)
            ):
                return True
    return False
