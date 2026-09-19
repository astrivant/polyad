"""
Check shared model identity, serialization and consumer validation contracts.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest
from attrs import has
from cattrs.errors import ForbiddenExtraKeysError

import polyad_types
from polyad import graph
from polyad.compiler.registry import RESOURCE_MODELS
from polyad_schemas import load_schema
from polyad_sdk import Event as ClientEvent
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
from polyad_types.graphs.topology import GraphNode, PolyGraph
from polyad_types.resources import registry


def test_operator_and_client_share_the_public_models():
    """
    Prevent independent class copies from drifting across consumers.
    """
    assert RESOURCE_MODELS["Graph"] is Graph
    assert graph.Topology is Topology
    assert graph.StructuralRule is StructuralRule
    assert graph.Replication is Replication
    assert ClientEvent is Event


def test_resource_consumers_use_the_same_registry_and_classes():
    """
    Keep compiler dispatch and public resource constructors on the canonical catalog.
    """
    assert RESOURCE_MODELS is registry.RESOURCE_REGISTRY
    assert len(registry.RESOURCE_CLASSES) == len(RESOURCE_MODELS)
    for resource in registry.RESOURCE_CLASSES:
        assert RESOURCE_MODELS[resource.resource_type.kind] is resource
        module = importlib.import_module(resource.__module__)
        assert getattr(module, resource.__name__) is resource


def test_domain_exports_and_schemas_refer_to_canonical_models():
    """
    Resolve every shared model through its defining module and its generated schema.
    """
    definitions = load_schema("models")["$defs"]
    discovered = set()
    for info in pkgutil.walk_packages(polyad_types.__path__, "polyad_types."):
        module = importlib.import_module(info.name)
        for model in vars(module).values():
            if not isinstance(model, type) or not has(model) or not model.__module__.startswith("polyad_types."):
                continue
            defining_module = importlib.import_module(model.__module__)
            assert getattr(defining_module, model.__name__) is model
            discovered.add(f"{model.__module__}.{model.__qualname__}")
    assert set(definitions) == discovered
    assert polyad_types.graphs.Cheeger is Cheeger
    assert polyad_types.graphs.PolyGraph is PolyGraph
    assert polyad_types.api.ConnectionRequest is ConnectionRequest
    assert polyad_types.networking.NetworkPort is NetworkPort
    assert polyad_types.events.Event is Event
    assert polyad_types.serialization.converter is polyad_types.converter


@pytest.mark.parametrize(
    "model",
    [
        Topology(nodes=(Node("work", "Workload", "worker"),)),
        Topology(mode="persistent", nodes=(Node("service", "Daemon", "server"),)),
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
