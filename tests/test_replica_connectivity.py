"""
Verify replica connection patterns, custom edges and live scaling constraints.
"""

from __future__ import annotations

import asyncio

import networkx as nx
import pytest

from polyad.compiler import asts
from polyad.compiler.passes.network import scope_label, traffic
from polyad.graph import ReplicaConnection, ReplicaConnectivity, Replication, graph_cheeger
from polyad.graph.replication import replica_topology
from polyad.graph.topology import converter, topology
from polyad.operator.controller import Controller
from polyad.operator.network import context
from tests.test_operator import FakeAPI, resource, template
from tests.test_replication import group, policy_family, start_family, turn


@pytest.mark.parametrize(
    ("mode", "pairs", "cheeger"),
    [
        ("Independent", set(), 0),
        ("Chain", {(0, 1), (1, 2), (2, 3)}, 0.5),
        ("Ring", {(0, 1), (1, 2), (2, 3), (3, 0)}, 1),
        ("Star", {(0, 1), (0, 2), (0, 3)}, 1),
        ("FullMesh", {(a, b) for a in range(4) for b in range(4) if a != b}, 2),
        ("Custom", {(0, 2), (2, 3)}, 0),
    ],
)
@pytest.mark.parametrize("bidirectional", [False, True])
def test_modes_project_directed_connections_and_undirected_cheeger(mode, pairs, cheeger, bidirectional):
    """
    Preserve admission independence while applying the declared data-flow pattern.
    """
    settings = {"mode": mode, "bidirectional": bidirectional and mode != "Independent"}
    if mode == "Custom":
        settings["edges"] = [{"source": f"replica-{a}", "target": f"replica-{b}"} for a, b in sorted(pairs)]
    graph = topology(group(4, connectivity=settings)["spec"], "ReplicaGroup")
    expected = pairs | {(b, a) for a, b in pairs} if bidirectional else pairs
    assert {(edge.source, edge.target) for edge in graph.connections} == {(f"replica-{a}", f"replica-{b}") for a, b in expected}
    assert len(graph.connections) == len(expected)
    assert all(not node.requires for node in graph.nodes)
    projected = nx.Graph()
    projected.add_nodes_from(node.name for node in graph.nodes)
    projected.add_edges_from((edge.source, edge.target) for edge in graph.connections)
    assert graph_cheeger(projected) == cheeger


@pytest.mark.parametrize("mode", ["Independent", "Chain", "Ring", "Star", "FullMesh", "Custom"])
@pytest.mark.parametrize("count", [0, 1, 2])
def test_small_counts_have_no_self_connections_or_duplicate_edges(mode, count):
    """
    Degenerate patterns remain valid at zero, one and two copies.
    """
    graph = topology(group(count, connectivity={"mode": mode})["spec"], "ReplicaGroup")
    assert len(graph.nodes) == count
    expected = 0 if count < 2 or mode in {"Independent", "Custom"} else (2 if mode in {"Ring", "FullMesh"} else 1)
    assert len(graph.connections) == expected
    assert all(edge.source != edge.target for edge in graph.connections)


def test_custom_edges_activate_by_ordinal_and_retain_port_grants():
    """
    Keep dormant custom edges for future replicas and deduplicate symmetric grants.
    """
    spec = group(
        2,
        maxReplicas=4,
        connectivity={
            "mode": "Custom",
            "bidirectional": True,
            "edges": [
                {"source": "replica-0", "target": "replica-1", "ports": [{"port": 8080}]},
                {"source": "replica-1", "target": "replica-0", "ports": [{"port": 9090, "protocol": "UDP"}]},
                {"source": "replica-1", "target": "replica-3"},
            ],
        },
    )["spec"]
    graph = topology(spec, "ReplicaGroup")
    assert len(graph.connections) == 4
    for source, target in [("replica-0", "replica-1"), ("replica-1", "replica-0")]:
        assert {
            (port.port, port.protocol)
            for edge in graph.connections
            if (edge.source, edge.target) == (source, target)
            for port in edge.ports
        } == {
            (8080, "TCP"),
            (9090, "UDP"),
        }
    assert len(topology({**spec, "replicas": 4}, "ReplicaGroup").connections) == 6
    assert not topology({**spec, "replicas": 1}, "ReplicaGroup").connections


@pytest.mark.parametrize(
    "settings",
    [
        {"mode": "Unknown"},
        {"mode": "Independent", "ports": [{"port": 80}]},
        {"mode": "Independent", "bidirectional": True},
        {"mode": "Custom", "ports": [{"port": 80}]},
        {"mode": "Chain", "edges": [{"source": "replica-0", "target": "replica-1"}]},
        {"mode": "Custom", "edges": [{"source": "replica-0", "target": "replica-0"}]},
        {"mode": "Custom", "edges": [{"source": "replica-00", "target": "replica-1"}]},
        {"mode": "Custom", "edges": [{"source": "replica-0", "target": "replica-32"}]},
        {"mode": "Custom", "edges": [{"source": "replica-0", "target": "replica-1"}] * 2},
        {"mode": "Ring", "ports": [{"port": 0}]},
    ],
)
def test_invalid_connectivity_fails_before_projection(settings):
    """
    Reject unsupported modes, ambiguous edges and ordinals beyond the group's bounds.
    """
    with pytest.raises(ValueError):
        topology(group(connectivity=settings)["spec"], "ReplicaGroup")


def test_public_models_default_to_independent_copies():
    """
    Keep existing declarations compatible and expose typed custom connections.
    """
    policy = converter.structure(group()["spec"], Replication)
    assert policy.connectivity == ReplicaConnectivity()
    assert not replica_topology(group()["spec"])["connections"]
    assert ReplicaConnectivity(mode="Custom", edges=(ReplicaConnection("replica-0", "replica-1"),)).mode == "Custom"


@pytest.mark.parametrize("mode", ["Ring", "FullMesh"])
def test_retiring_siblings_keep_their_pattern(mode):
    """
    Rebuild edges over live retiring ordinals as well as requested copies.
    """
    projected = replica_topology(group(1, connectivity={"mode": mode})["spec"], retained=["replica-3", "replica-0"])
    assert [node["name"] for node in projected["nodes"]] == ["replica-0", "replica-3"]
    assert {(edge["source"], edge["target"]) for edge in projected["connections"]} == {
        ("replica-0", "replica-3"),
        ("replica-3", "replica-0"),
    }


@pytest.mark.parametrize(
    ("mode", "initial", "requested", "bounds"),
    [("Chain", 4, 6, {"minimum": 0.5}), ("Ring", 4, 6, {"minimum": 1}), ("FullMesh", 4, 5, {"maximum": 2}), ("Star", 2, 1, {"minimum": 1})],
)
def test_cheeger_bound_blocks_scale_changes_before_mutation(mode, initial, requested, bounds):
    """
    Evaluate the selected pattern again before a native controller is created or retired.
    """

    async def scenario():
        api = FakeAPI(
            group(initial, connectivity={"mode": mode}, rules=["bottleneck"]),
            resource("Daemon", "worker", {"template": template(True)}),
            resource("GraphRule", "bottleneck", {"enforcement": "Referenced", "relation": "connections", "cheeger": bounds}),
        )
        await turn(api)
        assert len(api.children("Deployment")) == initial
        root = api.objects[("ReplicaGroup", "test", "copies")]
        root["spec"]["replicas"] = requested
        root["metadata"]["generation"] += 1
        api.calls.clear()
        with pytest.raises(ValueError, match="cheeger="):
            await turn(api)
        assert not any(method in {"POST", "DELETE"} for method, _, _ in api.calls)
        assert not root["status"]["scaleCurrent"]

    asyncio.run(scenario())


def test_shared_source_rebuilds_connections_and_preserves_existing_copies():
    """
    Check inherited counts with the instance's mode and rules at both boundaries.
    """

    async def scenario():
        api = policy_family(bound=20, replicas=4)
        source = api.objects[("ReplicaGroup", "test", "copies")]
        source["spec"].update(connectivity={"mode": "Ring"}, rules=["ring"])
        api.objects[("GraphRule", "test", "ring")] = resource(
            "GraphRule", "ring", {"enforcement": "Referenced", "relation": "connections", "cheeger": {"minimum": 1}}
        )
        (instance,) = await start_family(api)
        original = {child["metadata"]["uid"] for child in api.children("Deployment")}
        source["spec"]["replicas"] = 5
        source["metadata"]["generation"] += 1
        await turn(api, instance["metadata"]["name"])
        assert original < {child["metadata"]["uid"] for child in api.children("Deployment")}
        assert instance["status"]["metrics"]["topology"]["connections"]["edgeCount"] == 5
        source["spec"]["replicas"] = 6
        source["metadata"]["generation"] += 1
        with pytest.raises(ValueError, match="cheeger="):
            await turn(api, instance["metadata"]["name"])
        assert len(api.children("Deployment")) == 5

    asyncio.run(scenario())


def test_custom_scale_out_waits_for_an_edge_connecting_the_new_copy():
    """
    Block a new isolated custom vertex until its declaration satisfies the rule.
    """

    async def scenario():
        api = FakeAPI(
            group(
                3,
                rules=["connected"],
                connectivity={
                    "mode": "Custom",
                    "edges": [
                        {"source": "replica-0", "target": "replica-1"},
                        {"source": "replica-1", "target": "replica-2"},
                    ],
                },
            ),
            resource("Daemon", "worker", {"template": template(True)}),
            resource("GraphRule", "connected", {"enforcement": "Referenced", "relation": "connections", "cheeger": {"minimum": 0.5}}),
        )
        await turn(api)
        original = {child["metadata"]["uid"] for child in api.children("Deployment")}
        root = api.objects[("ReplicaGroup", "test", "copies")]
        root["spec"]["replicas"] = 4
        root["metadata"]["generation"] += 1
        with pytest.raises(ValueError, match="cheeger=0"):
            await turn(api)
        assert len(api.children("Deployment")) == 3
        root["spec"]["connectivity"]["edges"].append({"source": "replica-2", "target": "replica-3"})
        root["metadata"]["generation"] += 1
        await turn(api)
        assert original < {child["metadata"]["uid"] for child in api.children("Deployment")}
        assert root["status"]["scaleCurrent"]

    asyncio.run(scenario())


def test_connected_retiring_sibling_does_not_appear_isolated():
    """
    Keep a sibling's retiring copies connected while checking another group's scale-out.
    """

    async def scenario():
        api = policy_family(bound=10, replicas=3, uses=("left", "right"))
        api.objects[("ReplicaGroup", "test", "copies")]["spec"].update(connectivity={"mode": "Ring"}, rules=["ring"])
        api.objects[("GraphRule", "test", "ring")] = resource(
            "GraphRule", "ring", {"enforcement": "Referenced", "relation": "connections", "cheeger": {"minimum": 1}}
        )
        left, right = await start_family(api)
        left["spec"].update(inheritReplicas=False, replicas=2)
        left["metadata"]["generation"] += 1
        await turn(api, left["metadata"]["name"])
        assert sum(bool(child["metadata"].get("deletionTimestamp")) for child in api.children("Deployment")) == 1
        right["spec"].update(inheritReplicas=False, replicas=4)
        right["metadata"]["generation"] += 1
        await turn(api, right["metadata"]["name"])
        assert len(api.children("Deployment")) == 7

    asyncio.run(scenario())


def test_nested_network_connections_follow_inherited_replica_count():
    """
    Resolve new ordinal branches and ring grants through a shared group's live count.
    """

    async def scenario():
        source = group(3, "Graph", templateOnly=True)
        instance = group(
            2,
            "Graph",
            replicaSource={"name": "source", "uid": source["metadata"]["uid"]},
            connectivity={"mode": "Ring", "ports": [{"port": 8080}]},
            network={"allowWithin": False, "allowDNS": False},
        )
        source["metadata"]["name"] = "source"
        instance["metadata"]["uid"] = "instance"
        api = FakeAPI(source, instance)
        controller = Controller(api)
        child = asts.to_document(controller.child(instance, "replica-2", "Graph", {"nodes": []}))
        child["metadata"].update(uid="nested", resourceVersion="1")
        _, scopes = await context(api, child, "worker")
        grants = traffic(scopes, "egress")
        assert grants == [
            {
                "namespace": "test",
                "labels": {scope_label("test", "ReplicaGroup", "copies", "replica-0"): "true"},
                "ports": [("TCP", 8080)],
                "principals": [],
                "methods": [],
                "paths": [],
            }
        ]

    asyncio.run(scenario())
