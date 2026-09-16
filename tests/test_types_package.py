"""
Check shared model identity, serialization and consumer validation contracts.
"""

from __future__ import annotations

import pytest
from cattrs.errors import ForbiddenExtraKeysError

from polyad import graph
from polyad.compiler.registry import RESOURCE_MODELS
from polyad_client import Event as ClientEvent
from polyad_types import (
    ActivationRequest,
    Cheeger,
    CompositionItem,
    CompositionRequest,
    ConnectionRequest,
    Event,
    Graph,
    GraphMetrics,
    NetworkPort,
    Node,
    ObjectMeta,
    ReplicaConnectivity,
    ReplicaTemplate,
    Replication,
    StructuralRule,
    Topology,
    from_dict,
    from_document,
    to_dict,
    to_document,
)
from polyad_types.topology import GraphNode, PolyGraph


def test_operator_and_client_share_the_public_models():
    """
    Prevent independent class copies from drifting across consumers.
    """
    assert RESOURCE_MODELS["Graph"] is Graph
    assert graph.Topology is Topology
    assert graph.StructuralRule is StructuralRule
    assert graph.Replication is Replication
    assert ClientEvent is Event


@pytest.mark.parametrize(
    "model",
    [
        Topology(nodes=(Node("work", "Workload", "worker"),)),
        StructuralRule(relation="connections", cheeger=Cheeger(minimum=0.5)),
        Replication(ReplicaTemplate("Graph", "pipeline"), connectivity=ReplicaConnectivity(mode="Ring")),
        ActivationRequest("pulse", "pipeline", "uid", "work"),
        CompositionRequest("request", "root", (CompositionItem("root", "Graph", {"nodes": []}),)),
        ConnectionRequest("edge", "test", "Graph", "pipeline", "uid", "first", "second", 60, (NetworkPort(8080),)),
        Event("1-0", "topology", {"graph": "pipeline"}),
        GraphMetrics(observedGeneration=2),
    ],
)
def test_models_round_trip_through_the_public_codec(model):
    """
    Reconstruct nested typed values through the shared JSON codec.
    """
    assert from_dict(to_dict(model), type(model)) == model


def test_generic_polygraph_keeps_typed_nodes():
    """
    Preserve generic graph references through serialization without the scheduler.
    """
    model = PolyGraph[GraphNode](nodes=(GraphNode(name="pipeline", kind="Graph", ref="template"),))
    restored = from_dict(to_dict(model), PolyGraph[GraphNode])
    assert restored == model
    assert isinstance(restored.nodes[0], GraphNode)


def test_manifest_extensions_survive_without_sharing_mutable_input():
    """
    Keep Kubernetes extensions while isolating a consumer's mutable documents.
    """
    model = Graph(metadata=ObjectMeta(name="pipeline", extra={"managedFields": [{"manager": "polyad"}]}), spec={"nodes": []})
    document = to_document(model)
    assert from_document(document) == model
    assert from_dict(document, Graph) == model
    document["metadata"]["managedFields"][0]["manager"] = "changed"
    assert model.metadata.extra["managedFields"][0]["manager"] == "polyad"
    assert document["kind"] == "Graph"


def test_configuration_decoding_rejects_invalid_or_unrecognized_fields():
    """
    Retain constructor constraints and strict input decoding for public configs.
    """
    with pytest.raises(ValueError, match="minimum must not exceed"):
        from_dict({"minimum": 2, "maximum": 1}, Cheeger)
    with pytest.raises(ForbiddenExtraKeysError, match="Extra fields"):
        from_dict({"unexpected": True}, StructuralRule)
    with pytest.raises(ValueError, match="ttlSeconds"):
        ConnectionRequest("edge", "test", "Graph", "pipeline", "uid", "first", "second", 0)
