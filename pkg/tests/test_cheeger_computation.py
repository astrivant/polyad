"""
Verify configurable exact search, preferred cuts and conservative admission under incomplete work.
"""

from __future__ import annotations

import asyncio
from itertools import combinations
from pathlib import Path

import jsonschema
import networkx as nx
import pytest
import yaml

from polyad.graph import Cheeger, CheegerComputation, Connection, Node, StructuralRule, Topology, evaluate_rule, graph_cheeger
from polyad.graph.cheeger import CheegerIncomplete, compute_cheeger
from polyad.operator.policies.cheeger import computation_limits
from polyad.operator.policies.rules import RuleViolation, check_rules
from tests.test_operator import FakeAPI, resource


def test_larger_boundaries_require_explicit_vertex_and_work_budgets():
    """
    Raising only the vertex cap cannot silently return an approximate result.
    """
    graph = nx.path_graph(21)
    with pytest.raises(CheegerIncomplete, match="20 vertices"):
        graph_cheeger(graph)
    with pytest.raises(CheegerIncomplete, match="CutBudget") as error:
        graph_cheeger(graph, CheegerComputation(maxVertices=21))
    assert error.value.result.evaluatedCuts == 524287
    assert graph_cheeger(graph, CheegerComputation(maxVertices=21, maxCuts=1048575, timeoutSeconds=15)) == pytest.approx(0.1)


def test_default_boundary_completes_every_cut_within_its_runtime_budget():
    """
    Protect the supported exact-search ceiling from hot-loop performance regressions.
    """
    result = compute_cheeger(nx.path_graph(20))
    assert result.exact and result.reason == "Complete"
    assert result.evaluatedCuts == 524287
    assert result.upperBound == pytest.approx(0.1)
    assert result.durationSeconds < result.inputs["timeoutSeconds"]


def test_priority_cuts_disprove_minima_without_claiming_an_exact_constant():
    """
    A known middle bottleneck gets checked before singleton cuts, with a rejection certificate.
    """
    graph = nx.path_graph(list("abcdef"))
    settings = CheegerComputation(maxCuts=1, priorityCuts=(("a", "b", "c"),))
    result = compute_cheeger(graph, settings, minimum=0.5)
    assert not result.exact and result.reason == "MinimumViolated"
    assert result.evaluatedCuts == 1 and result.upperBound == pytest.approx(1 / 3)
    assert set(result.cut) == set("abc")
    assert result.inputs["vertices"] == 6 and result.inputs["edges"] == 5
    assert result.inputs["maxCuts"] == 1 and result.inputs["priorityCuts"] == 1
    assert result.durationSeconds >= 0
    ordinary = compute_cheeger(graph, CheegerComputation(maxCuts=1), minimum=0.5)
    assert ordinary.reason == "CutBudget" and ordinary.upperBound == 1


def test_priority_order_and_complements_do_not_change_the_exact_answer():
    """
    Compare completed prioritized searches with an independent cut oracle.
    """
    for seed in range(12):
        graph = nx.relabel_nodes(nx.gnp_random_graph(7, 0.5, seed=seed), lambda node: str(node))
        expected = min(nx.cut_size(graph, subset) / size for size in range(1, 4) for subset in combinations(graph, size))
        result = compute_cheeger(
            graph,
            CheegerComputation(priorityCuts=(("0", "1"), ("2", "3", "4", "5", "6"), ("0", "1"), ("absent",), tuple(graph))),
        )
        assert result.exact and result.upperBound == pytest.approx(expected)
        if nx.is_connected(graph):
            assert result.evaluatedCuts == 63 and result.skippedPriorityCuts == 2


def test_a_good_preferred_cut_cannot_hide_a_bad_unexplored_cut():
    """
    Bounds passing the sampled partition cannot authorize an incomplete graph calculation.
    """
    graph = Topology(
        mode="persistent",
        nodes=tuple(Node(name, "Daemon", name) for name in "abcdef"),
        connections=tuple(Connection(left, right) for left, right in zip("abcde", "bcdef", strict=True)),
    )
    verdict = evaluate_rule(
        StructuralRule(
            relation="connections",
            cheeger=Cheeger(minimum=0.5, maximum=1),
            cheegerComputation=CheegerComputation(maxCuts=1, priorityCuts=(("a",),)),
        ),
        graph,
        expanded_nodes=6,
        nesting_depth=1,
    )
    assert not verdict["allowed"] and "cheeger" not in verdict["measurements"]
    assert verdict["cheegerComputation"]["upperBound"] == 1
    assert "inconclusive" in verdict["violations"][0]


def test_elapsed_time_reports_incomplete_instead_of_returning_an_estimate(monkeypatch):
    """
    A deterministic deadline can expire even when cut and vertex budgets are sufficient.
    """
    readings = iter((0, 1))
    monkeypatch.setattr("polyad.graph.cheeger.time.monotonic", lambda: next(readings))
    with pytest.raises(CheegerIncomplete, match="TimeBudget") as error:
        graph_cheeger(nx.path_graph(4), CheegerComputation(timeoutSeconds=0.1))
    assert error.value.result.upperBound is None and error.value.result.evaluatedCuts == 0


def test_operator_ceilings_apply_to_live_rules_and_cannot_be_overridden(monkeypatch):
    """
    Public policy budgets can only narrow the administrator's deployment limits.
    """
    monkeypatch.setenv("POLYAD_CHEEGER_MAX_VERTICES", "21")
    monkeypatch.setenv("POLYAD_CHEEGER_MAX_CUTS", "1048575")
    monkeypatch.setenv("POLYAD_CHEEGER_TIMEOUT_SECONDS", "15")
    assert graph_cheeger(nx.path_graph(21), limits=computation_limits()) == pytest.approx(0.1)
    graph = {"mode": "persistent", "nodes": [{"name": "a", "kind": "Daemon", "ref": "worker"}]}
    rule = resource("GraphRule", "hard", {"cheeger": {}, "cheegerComputation": {"maxVertices": 22}})
    with pytest.raises(RuleViolation, match="exceeds operator ceiling 21"):
        asyncio.run(check_rules(FakeAPI(rule), "test", "Graph", graph))


@pytest.mark.parametrize(
    "settings",
    [
        {"maxVertices": True},
        {"maxVertices": 1},
        {"maxVertices": 4097},
        {"maxCuts": 0},
        {"maxCuts": 1.5},
        {"timeoutSeconds": 0},
        {"timeoutSeconds": float("nan")},
        {"timeoutSeconds": float("inf")},
        {"timeoutSeconds": True},
        {"priorityCuts": (("a", "a"),)},
        {"priorityCuts": ((),)},
        {"priorityCuts": (("",),)},
        {"priorityCuts": ("abc",)},
    ],
)
def test_computation_controls_validate_public_inputs(settings):
    """
    Reject malformed or unbounded settings before work begins.
    """
    with pytest.raises(ValueError):
        CheegerComputation(**settings)


def test_tuning_reference_and_generated_schemas_share_computation_types():
    """
    Validate the complete reference and reject negative targets and malformed search controls at admission.
    """
    root = Path(__file__).parents[2]
    documents = list(yaml.safe_load_all((root / "examples/cheeger-tuning.yaml").read_text()))
    for document in documents:
        crd = yaml.safe_load((root / f"charts/polyad-crds/crds/{document['kind'].lower()}s.yaml").read_text())
        schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        # Kubernetes OpenAPI uses the boolean exclusiveMinimum form from draft 4.
        jsonschema.Draft4Validator(schema).validate(document)
    for kind in ("graphs", "polygraphs", "rewrites", "graphrules"):
        crd = yaml.safe_load((root / f"charts/polyad-crds/crds/{kind}.yaml").read_text())
        spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
        if kind == "rewrites":
            spec = spec["topology"]["properties"]
        if kind != "graphrules":
            spec = spec["throughput"]["properties"]
            bounds = spec["tiers"]["items"]["properties"]["cheeger"]
            assert not jsonschema.Draft4Validator(bounds).is_valid({"minimum": -1})
            assert len(bounds["x-kubernetes-validations"]) == 2
        validator = jsonschema.Draft4Validator(spec["cheegerComputation"])
        assert validator.is_valid({"maxVertices": 22, "maxCuts": 2097151, "timeoutSeconds": 15, "priorityCuts": [["a", "b"]]})
        for invalid in ({"maxVertices": True}, {"maxCuts": 0}, {"timeoutSeconds": "5"}, {"priorityCuts": [["a", "a"]]}):
            assert not validator.is_valid(invalid)


def test_solver_diagnostics_survive_kubernetes_status_schema_pruning():
    """
    Every certificate field must be declared in the persisted status schema, not silently pruned by Kubernetes.
    """
    root = Path(__file__).parents[2]
    report = compute_cheeger(nx.path_graph(list("abc")), CheegerComputation(maxCuts=1)).report()
    # JSON serialization converts attrs tuples into the arrays expected by OpenAPI.
    import json

    report = json.loads(json.dumps(report))
    for kind in ("graphs", "polygraphs"):
        crd = yaml.safe_load((root / f"charts/polyad-crds/crds/{kind}.yaml").read_text())
        schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]["throughput"]
        for field in ("computation", "currentComputation"):
            certificate = schema["properties"][field]
            assert report.keys() <= certificate["properties"].keys()
            jsonschema.Draft4Validator(certificate).validate(report)
        candidate = schema["properties"]["candidateComputations"]["items"]
        assert {*report, "layout"} <= candidate["properties"].keys()
        jsonschema.Draft4Validator(candidate).validate({"layout": "chain", **report})
        assert "observedGeneration" in schema["properties"]
