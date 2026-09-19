"""
Translate ID-addressed request compositions into auditable Kubernetes definitions.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
    from typing import Any

from polyad.compiler.registry import RESOURCE_MODELS as RESOURCE_REGISTRY
from polyad_types import resources as asts
from polyad_types.api.requests import CompositionRequest, identity
from polyad_types.graphs.topology import topology
from polyad_types.resources.base import SpecResource
from polyad_types.serialization import converter


def request_name(request_id: str) -> str:
    """
    Derive a collision-resistant Kubernetes receipt name from the request ID.

    Args:
        request_id (str): Stable request identity, reused when retrying a submission.

    Returns:
        str: Deterministic Composition name.
    """
    return "composition-" + hashlib.sha256(identity(request_id).encode()).hexdigest()[:32]


def compile_composition(request: CompositionRequest, namespace: str, *, owner_uid: str | None = None) -> dict[str, asts.Resource]:
    """
    Resolve request IDs into deterministic manifest names and preserve provenance.

    Args:
        request (CompositionRequest): Validated composition envelope.
        namespace (str): Operator namespace; the client cannot choose another namespace.
        owner_uid (str | None): Persisted Composition UID, omitted for compiler previews.

    Returns:
        dict[str, asts.Resource]: Definition IDs mapped to typed manifests, with templates before the root.
    """
    digest = request.digest()
    by_id = {item.id: item for item in request.objects}
    names = {
        item.id: "definition-" + hashlib.sha256(f"{request.requestId}/{item.id}".encode()).hexdigest()[:40] for item in request.objects
    }
    uses: nx.DiGraph[str] = nx.DiGraph()
    uses.add_nodes_from(by_id)
    specs = {}
    node_count = 0
    for item in request.objects:
        spec = copy.deepcopy(item.spec)
        if item.kind in asts.BOUNDARY_KINDS:
            spec["templateOnly"] = item.id != request.rootId
            body = spec
            if item.kind == "ReplicaGroup":
                target_id = spec["template"].pop("refId")
                target = by_id.get(target_id)
                if target is None or "ref" in spec["template"] or "kind" in spec["template"]:
                    raise ValueError("replica template requires a composition refId")
                spec["template"].update(kind=target.kind, ref=names[target.id])
                if "replicaSource" in spec:
                    raise ValueError("replicaSource is assigned by the compiler")
                uses.add_edge(item.id, target.id)
            for node in body.get("nodes", []):
                if node.get("cluster"):
                    raise ValueError(
                        "composition refId definitions are cluster-local; submit remote PolyGraph nodes with installed target-cluster refs"
                    )
                node_id = identity(node.pop("id"))
                target = by_id.get(node.pop("refId"))
                if target is None:
                    raise ValueError("node refId does not identify a request definition")
                if {"name", "ref", "kind", "gate"} & node.keys():
                    raise ValueError("composition nodes use id/refId instead of name/ref/kind")
                uses.add_edge(item.id, target.id)
                node.update(name=node_id, id=node_id, kind=target.kind, ref=names[target.id])
                for edge in node.get("requires", []):
                    if "node" in edge:
                        raise ValueError("composition dependencies use nodeId")
                    edge["node"] = identity(edge.pop("nodeId"))
                if "gateId" in node:
                    gate_id = node.pop("gateId")
                    if gate_id not in by_id or by_id[gate_id].kind != "Gate":
                        raise ValueError("gateId must identify a Gate definition")
                    node["gate"] = names[gate_id]
                    uses.add_edge(item.id, gate_id)
            for edge in body.get("connections", []):
                if {"source", "target"} & edge.keys():
                    raise ValueError("composition connections use sourceId/targetId")
                edge["source"] = identity(edge.pop("sourceId"))
                edge["target"] = identity(edge.pop("targetId"))
            if "shutdownPolicy" in body:
                raise ValueError("composition policies use shutdownPolicyId")
            if "shutdownPolicyId" in body:
                policy_id = body.pop("shutdownPolicyId")
                if policy_id not in by_id or by_id[policy_id].kind != "ShutdownPolicy":
                    raise ValueError("shutdownPolicyId must identify a ShutdownPolicy")
                body["shutdownPolicy"] = names[policy_id]
                uses.add_edge(item.id, policy_id)
            parsed = topology(body, item.kind)
            node_count += len(parsed.nodes)
        specs[item.id] = spec
    if node_count > 4096 or not nx.is_directed_acyclic_graph(uses):
        raise ValueError("composition contains recursive references or more than 4096 nodes")
    if nx.descendants(uses, request.rootId) | {request.rootId} != set(by_id):
        raise ValueError("all composition definitions must be reachable from rootId")
    manifests: dict[str, asts.Resource] = {}
    for item_id in reversed(list(nx.topological_sort(uses))):
        item = by_id[item_id]
        cls = RESOURCE_REGISTRY[item.kind]
        assert issubclass(cls, SpecResource)
        annotations = {
            f"{asts.GROUP}/request-id": request.requestId,
            f"{asts.GROUP}/object-id": item.id,
            f"{asts.GROUP}/request-hash": digest,
        }
        if owner_uid:
            annotations[f"{asts.GROUP}/composition-uid"] = owner_uid
        labels = {f"{asts.GROUP}/request": request_name(request.requestId)[12:]}
        if owner_uid:
            labels[f"{asts.GROUP}/owner"] = owner_uid
        annotations[f"{asts.GROUP}/desired-hash"] = hashlib.sha256(json.dumps(specs[item.id], sort_keys=True).encode()).hexdigest()[:12]
        manifests[item.id] = cls(
            metadata=asts.ObjectMeta(
                name=names[item.id],
                namespace=namespace,
                labels=labels,
                annotations=annotations,
                ownerReferences=(
                    asts.OwnerReference(
                        apiVersion=f"{asts.GROUP}/{asts.VERSION}",
                        kind="Composition",
                        name=request_name(request.requestId),
                        uid=owner_uid,
                        controller=True,
                        blockOwnerDeletion=True,
                    ),
                )
                if owner_uid
                else None,
            ),
            spec=specs[item.id],
        )
    return manifests


def receipt_spec(request: CompositionRequest) -> dict[str, str]:
    """
    Encode arbitrary workload JSON as a CEL-visible immutable string in the receipt.

    Args:
        request (CompositionRequest): Composition accepted by the ID compiler.

    Returns:
        dict[str, str]: CR specification with immutable identity and canonical payload.
    """
    document = converter.unstructure(request)
    document["objects"].sort(key=lambda item: item["id"])
    return {"requestId": request.requestId, "document": json.dumps(document, sort_keys=True, allow_nan=False)}


def read_receipt(spec: dict[str, Any]) -> CompositionRequest:
    """
    Decode a durable receipt and verify its visible identity agrees with the payload.

    Args:
        spec (dict[str, Any]): Persisted Composition specification.

    Returns:
        CompositionRequest: Validated request envelope for compilation.
    """
    request = converter.structure(json.loads(spec["document"]), CompositionRequest)
    if request.requestId != spec["requestId"]:
        raise ValueError("Composition requestId does not match its document")
    return request
