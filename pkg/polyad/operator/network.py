"""
Refresh ancestor policies and persist network guards before admitting workload pods.
"""

from __future__ import annotations

import copy
import os
from typing import TYPE_CHECKING

from polyad.compiler import asts
from polyad.compiler.passes.network import NetworkScope, policy_specs, scope_label
from polyad.graph.rules import StructuralRule
from polyad.graph.topology import converter, topology

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.api import API
    from polyad.operator.controller import Controller

POLICY_KINDS = asts.NETWORK_POLICY_KINDS


async def context(api: API, obj: dict[str, Any], node: str) -> tuple[dict[str, str], list[NetworkScope]]:
    """
    Resolve actual owner identities and intersect every selected ancestor rule.

    Args:
        api (API): Refreshed Kubernetes read adapter.
        obj (dict[str, Any]): Current graph instance.
        node (str): Direct workload node being compiled.

    Returns:
        tuple[dict[str, str], list[NetworkScope]]: Trusted membership labels and inherited policy scopes.
    """
    from polyad.operator.controller import Pending

    namespace = obj["metadata"]["namespace"]
    inventory = await api.request("GET", "GraphRule", namespace)
    documents = {item["metadata"]["name"]: item for item in inventory.get("items", [])}
    rules = {name: converter.structure(item["spec"], StructuralRule) for name, item in documents.items()}
    labels = {f"{asts.GROUP}/network-owner": obj["metadata"]["uid"], f"{asts.GROUP}/network-node": node}
    scopes = []
    current, branch = obj, node
    seen = set()
    for _ in range(32):
        meta, kind = current["metadata"], current["kind"]
        if meta["uid"] in seen:
            raise ValueError("cyclic graph ownership")
        seen.add(meta["uid"])
        if kind == "ReplicaGroup":
            from polyad.operator.replication import replica_selector

            labels[replica_selector(meta["uid"])] = "true"
            source = current.get("spec", {}).get("replicaSource")
            if source and current["spec"].get("inheritReplicas", True):
                labels[replica_selector(source["uid"])] = "true"
        labels[scope_label(namespace, kind, meta["name"])] = "true"
        labels[scope_label(namespace, kind, meta["name"], branch)] = "true"
        # Feedback's graph specification is applied by its epoch instance. Its identity
        # label is still inherited so peers can select all epochs through Feedback.
        if kind != "Feedback":
            graph = topology(current["spec"], kind)
            selected = {name for name, rule in rules.items() if rule.enforcement == "Namespace"} | set(graph.rules)
            if selected - rules.keys():
                raise ValueError("a referenced network GraphRule is unavailable")
            if any(documents[name]["metadata"].get("deletionTimestamp") for name in selected):
                raise Pending("a selected network GraphRule is being deleted")
            accesses = [
                graph.network,
                *(rules[name].network for name in sorted(selected) if current is obj or rules[name].scope == "Subtree"),
            ]
            for access in accesses:
                if access is not None and (current is obj or access.scope == "Subtree"):
                    if access.mesh and os.environ.get("POLYAD_MESH_ENABLED", "false").lower() != "true":
                        raise ValueError("network.mesh requires the operator's mesh integration to be enabled")
                    scopes.append(
                        NetworkScope(
                            namespace,
                            kind,
                            meta["name"],
                            branch,
                            access,
                            tuple((edge.source, edge.target, edge.ports) for edge in graph.connections),
                        )
                    )
        owners = [
            owner
            for owner in meta.get("ownerReferences", [])
            if owner.get("controller")
            and owner.get("apiVersion") == f"{asts.GROUP}/{asts.VERSION}"
            and owner.get("kind") in asts.BOUNDARY_KINDS
        ]
        if not owners:
            return labels, scopes
        owner = owners[0]
        parent = await api.get(owner["kind"], namespace, owner["name"])
        if parent is None or parent["metadata"]["uid"] != owner["uid"] or parent["metadata"].get("deletionTimestamp"):
            raise Pending("waiting for the current graph owner")
        branch = meta.get("labels", {}).get(f"{asts.GROUP}/node", "")
        if not branch:
            raise ValueError("nested graph lacks its compiler-assigned node identity")
        if parent["kind"] != "Feedback":
            parent_node = next((item for item in parent["spec"]["nodes"] if item["name"] == branch), None)
            if parent_node is None:
                raise Pending("ancestor is replacing this graph branch", phase="Draining")
        current = parent
    raise ValueError("network inheritance exceeds 32 graph boundaries")


async def ensure_policies(controller: Controller, obj: dict[str, Any], plans: dict[str, list[NetworkScope]]) -> None:
    """
    Update owned policies in place, observe them freshly, and remove obsolete guards last.

    Args:
        controller (Controller): Controller whose API adapter serializes and fences writes.
        obj (dict[str, Any]): Current graph instance owning the policies.
        plans (dict[str, list[NetworkScope]]): Network scopes for each direct workload node.

    Returns:
        None: Admission may proceed after every desired policy was freshly observed.
    """
    from polyad.operator.controller import Pending

    namespace, uid = obj["metadata"]["namespace"], obj["metadata"]["uid"]
    wanted = set()
    changed = False
    for node, scopes in plans.items():
        selector = {f"{asts.GROUP}/network-owner": uid, f"{asts.GROUP}/network-node": node}
        for kind, spec in policy_specs(selector, scopes, mesh_namespace=os.environ.get("POLYAD_ISTIO_NAMESPACE", "istio-system")).items():
            desired = asts.to_document(controller.child(obj, f"net-{node}", kind, spec))
            name = desired["metadata"]["name"]
            wanted.add((kind, name))
            current = await controller.api.get(kind, namespace, name)
            if current is None:
                await controller.api.request("POST", kind, namespace, body=desired)
                changed = True
                continue
            if current["metadata"].get("ownerReferences") != desired["metadata"]["ownerReferences"]:
                raise ValueError("refusing to adopt a network policy owned by another controller")
            if current["metadata"].get("deletionTimestamp"):
                raise Pending("waiting for deleted network guards to disappear")
            if current.get("spec") != spec:
                replacement = copy.deepcopy(current)
                replacement["spec"] = spec
                replacement["metadata"].setdefault("annotations", {}).update(desired["metadata"]["annotations"])
                await controller.api.request("PUT", kind, namespace, name, replacement)
                changed = True
    if changed:
        raise Pending("network guards persisted; refresh before workload admission")
    children = await controller.api.owned(namespace, uid)
    for child in children:
        if f"{asts.GROUP}/capacity" in child["metadata"].get("labels", {}):
            continue
        if child["kind"] in POLICY_KINDS and (child["kind"], child["metadata"]["name"]) not in wanted:
            branch = child["metadata"].get("labels", {}).get(f"{asts.GROUP}/node", "").removeprefix("net-")
            if branch not in plans and any(
                item["kind"] not in POLICY_KINDS and item["metadata"].get("labels", {}).get(f"{asts.GROUP}/node") == branch
                for item in children
            ):
                continue
            if not child["metadata"].get("deletionTimestamp"):
                await controller.api.delete(child)
            raise Pending("waiting for obsolete network guards to disappear")
