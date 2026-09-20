"""
Verify mathematical meanings and mandatory policy checks across nested graph families.
"""

from __future__ import annotations

import asyncio
import math

import networkx as nx
import pytest

from polyad.exceptions.policies import RuleViolation
from polyad.graph import Connection, Dependency, Node, Spectrum, StructuralRule, Topology, evaluate_rule, graph_spectrum
from polyad.operator.policies.rules import check_rules
from polyad.operator.reconciliation.controller import Controller
from tests.test_operator import FakeAPI, resource, template


def test_spectrum_known_graphs_and_unweighted_projection():
    """
    Compute real symmetric spectra, ignoring direction, weights and self-loops.
    """
    graph = nx.DiGraph()
    graph.add_nodes_from(["a", "b", "c"])
    graph.add_edge("a", "b", weight=100)
    graph.add_edge("b", "a", weight=9)
    graph.add_edge("b", "c")
    graph.add_edge("a", "a")
    spectrum = graph_spectrum(graph)
    assert spectrum["adjacency"] == pytest.approx([-math.sqrt(2), 0, math.sqrt(2)])
    assert spectrum["laplacian"] == pytest.approx([0, 1, 3])
    assert spectrum["connectivity"] == pytest.approx(1)
    triangle = graph_spectrum(nx.complete_graph(["a", "b", "c"]))
    assert triangle["radius"] == pytest.approx(2)
    assert triangle["laplacian"] == pytest.approx([0, 3, 3])
    graph.add_node("isolated")
    assert graph_spectrum(graph)["connectivity"] == pytest.approx(0)
    assert graph_spectrum(nx.Graph())["radius"] == 0
    with pytest.raises(ValueError, match="256"):
        graph_spectrum(nx.empty_graph(257))


def test_relations_and_condensation_have_different_shapes():
    """
    Cycle rank and condensation size have explicit meanings independent of DAG depth.
    """
    graph = Topology(
        nodes=(Node("a", "Workload", "w"), Node("b", "Workload", "w", (Dependency("a"),)), Node("c", "Workload", "w", (Dependency("b"),))),
        connections=(Connection("a", "b"), Connection("b", "c"), Connection("c", "a")),
    )
    admission = evaluate_rule(
        StructuralRule(shapes=("tree",), spectrum=Spectrum(minConnectivity=1, maxRadius=math.sqrt(2))),
        graph,
        expanded_nodes=3,
        nesting_depth=1,
    )
    assert admission["allowed"]
    assert admission["measurements"]["depth"] == 3
    cyclic = evaluate_rule(
        StructuralRule(relation="connections", limits={"strongComponent": 2, "cycleRank": 0}, shapes=("acyclic",)),
        graph,
        expanded_nodes=3,
        nesting_depth=1,
    )
    assert not cyclic["allowed"]
    assert cyclic["measurements"]["depth"] == 1
    assert cyclic["measurements"]["strongComponent"] == 3
    assert cyclic["measurements"]["cycleRank"] == 1
    assert len(cyclic["violations"]) == 3


def test_namespace_policy_cannot_be_omitted_and_is_refreshed():
    """
    A rule update blocks new workload admission on the next refreshed pass.
    """

    async def scenario():
        graph = resource("Graph", "root", {"nodes": [{"name": "a", "kind": "Workload", "ref": "work"}]})
        rule = resource("GraphRule", "budget", {"limits": {"nodes": 0}})
        api = FakeAPI(graph, rule, resource("Workload", "work", {"template": template()}))
        controller = Controller(api)
        with pytest.raises(RuleViolation, match="nodes=1"):
            await controller.reconcile(("Graph", "test", "root"))
        assert not any(method == "POST" for method, _, _ in api.calls)
        api.objects[("GraphRule", "test", "budget")]["spec"]["limits"]["nodes"] = 1
        await controller.reconcile(("Graph", "test", "root"))
        assert any(kind == "Job" and method == "POST" for method, kind, _ in api.calls)
        status = api.objects[("Graph", "test", "root")]["status"]["structuralRules"][0]
        assert status["uid"] == "uid-budget" and status["allowed"]

    asyncio.run(scenario())


def test_referenced_rules_and_expanded_occurrences_survive_nesting():
    """
    Referencing one template twice counts two instances and inherits optional rules.
    """

    async def scenario():
        leaf = resource(
            "Graph",
            "leaf",
            {"templateOnly": True, "nodes": [{"name": "a", "kind": "Workload", "ref": "w"}, {"name": "b", "kind": "Workload", "ref": "w"}]},
        )
        root = {"nodes": [{"name": "left", "kind": "Graph", "ref": "leaf"}, {"name": "right", "kind": "Graph", "ref": "leaf"}]}
        optional = resource("GraphRule", "selected", {"enforcement": "Referenced", "limits": {"expandedNodes": 5}})
        api = FakeAPI(leaf, optional)
        assert await check_rules(api, "test", "Graph", root) == []
        with pytest.raises(RuleViolation, match="expandedNodes=6"):
            await check_rules(api, "test", "Graph", {**root, "rules": ["selected"]})
        optional["spec"]["limits"] = {"nodes": 1}
        api.objects[("GraphRule", "test", "selected")] = optional
        with pytest.raises(RuleViolation, match="at leaf"):
            await check_rules(api, "test", "Graph", {"rules": ["selected"], "nodes": root["nodes"][:1]})
        with pytest.raises(RuleViolation, match="unavailable"):
            await check_rules(api, "test", "Graph", {**root, "rules": ["missing"]})

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_invalid_spectral_bounds(value):
    """
    Reject non-finite, negative and boolean thresholds in the public Python API.
    """
    with pytest.raises(ValueError):
        Spectrum(maxRadius=value)


@pytest.mark.parametrize(
    ("graph", "expected"),
    [
        (nx.path_graph(4), 0.5),
        (nx.cycle_graph(4), 1),
        (nx.complete_graph(5), 3),
        (nx.empty_graph(0), 0),
        (nx.empty_graph(1), 0),
        (nx.empty_graph(3), 0),
    ],
)
def test_cheeger_known_graphs(graph, expected):
    """
    Compute exact edge expansion including degenerate and disconnected graphs.
    """
    from polyad.graph import graph_cheeger

    assert graph_cheeger(graph) == pytest.approx(expected)


def test_cheeger_projection_and_size_limit():
    """
    Ignore weights, directions, duplicate edges and loops and enforce the work cap.
    """
    from polyad.graph import graph_cheeger

    graph = nx.MultiDiGraph()
    graph.add_edges_from([("a", "b"), ("a", "b"), ("b", "a"), ("b", "c"), ("a", "a")], weight=99)
    assert graph_cheeger(graph) == 1
    assert graph_cheeger(nx.path_graph(20)) == pytest.approx(0.1)
    with pytest.raises(ValueError, match="20 vertices"):
        graph_cheeger(nx.path_graph(21))


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "1"])
def test_cheeger_invalid_bounds(value):
    """
    Reject invalid bounds for either comparison direction.
    """
    from polyad.graph import Cheeger

    for key in ("minimum", "maximum"):
        with pytest.raises(ValueError):
            Cheeger(**{key: value})
    with pytest.raises(ValueError, match="minimum"):
        Cheeger(minimum=2, maximum=1)


def test_cheeger_rule_admission_and_measurement():
    """
    Parse user bounds, persist measurements and reject admission after policy changes.
    """

    async def scenario():
        spec = {
            "nodes": [
                {"name": "a", "kind": "Workload", "ref": "w"},
                {"name": "b", "kind": "Workload", "ref": "w", "requires": [{"node": "a"}]},
            ]
        }
        rule = resource("GraphRule", "expansion", {"cheeger": {"minimum": 1, "maximum": 1}})
        api = FakeAPI(rule)
        reports = await check_rules(api, "test", "Graph", spec)
        assert reports[0]["measurements"]["cheeger"] == 1
        for bounds in ({"maximum": 0.5}, {"minimum": 2}):
            api.objects[("GraphRule", "test", "expansion")]["spec"]["cheeger"] = bounds
            with pytest.raises(RuleViolation, match="cheeger=1"):
                await check_rules(api, "test", "Graph", spec)

    asyncio.run(scenario())


def test_cheeger_matches_exhaustive_cuts():
    """
    Compare incremental cuts against an independent exhaustive oracle on small graphs.
    """
    from itertools import combinations

    from polyad.graph import graph_cheeger

    for seed in range(12):
        graph = nx.gnp_random_graph(7, 0.5, seed=seed)
        expected = min(nx.cut_size(graph, subset) / size for size in range(1, 4) for subset in combinations(graph, size))
        assert graph_cheeger(graph) == pytest.approx(expected)


@pytest.mark.parametrize("enabled", [False, True])
def test_benchmark_spectrum_retention_does_not_change_admission(enabled, monkeypatch):
    """
    Preserve computed eigenvalues across replicas only when the administrator enables their retention.
    """
    monkeypatch.setenv("POLYAD_METRICS_GRAPH_SPECTRA", str(enabled).lower())
    rule = resource("GraphRule", "spectrum", {"spectrum": {}})
    graph = {"nodes": [{"name": "a", "kind": "Workload", "ref": "work"}]}
    report = asyncio.run(check_rules(FakeAPI(rule), "test", "Graph", graph))[0]
    assert report["allowed"]
    assert report["spectrum"]["radius"] == 0
    assert ("adjacency" in report["spectrum"]) is enabled
    assert ("laplacian" in report["spectrum"]) is enabled
