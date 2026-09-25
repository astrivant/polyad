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

from polyad.exceptions.graph import CheegerIncomplete
from polyad.exceptions.policies import PolicyViolation
from polyad.graph import (
    Cheeger,
    CheegerComputation,
    CheegerReduction,
    Connection,
    Node,
    StructuralPolicy,
    Topology,
    evaluate_policy,
    graph_cheeger,
)
from polyad.graph.cheeger import compute_cheeger
from polyad.graph.reduction import clear_reduction_cache
from polyad.operator.policies.cheeger import computation_limits
from polyad.operator.policies.graph_policies import check_policies
from polyad_types.resources.registry import RESOURCE_TYPES
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
    verdict = evaluate_policy(
        StructuralPolicy(
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
    monkeypatch.setenv("POLYAD_CHEEGER_REDUCTION_ENABLED", "true")
    monkeypatch.setenv("POLYAD_CHEEGER_REDUCTION_SUPERNODES", "6")
    limits = computation_limits()
    assert limits.reduction.enabled and limits.reduction.supernodes == 6
    assert graph_cheeger(nx.path_graph(21), limits=limits) == pytest.approx(0.1)
    graph = {"mode": "persistent", "nodes": [{"name": "a", "kind": "Daemon", "ref": "worker"}]}
    rule = resource("GraphPolicy", "hard", {"cheeger": {}, "cheegerComputation": {"maxVertices": 22}})
    with pytest.raises(PolicyViolation, match="exceeds operator ceiling 21"):
        asyncio.run(check_policies(FakeAPI(rule), "test", "Graph", graph))


def test_reduction_requires_both_policy_and_operator_opt_in():
    """
    Keep exact-only behavior by default and prevent policy authors from bypassing the cluster gate.
    """
    requested = CheegerComputation(reduction=CheegerReduction(enabled=True, supernodes=4))
    with pytest.raises(ValueError, match="disabled by the operator"):
        compute_cheeger(nx.complete_graph(8), requested, limits=CheegerComputation(), minimum=3)
    ordinary = compute_cheeger(nx.complete_graph(8), minimum=3)
    assert ordinary.exact and ordinary.stage == "ExactEnumeration"


def test_fresh_spectral_certificate_can_settle_policy_without_exact_enumeration():
    """
    Let proven interval bounds stop work while retaining explicit non-exact status.
    """
    clear_reduction_cache()
    reduction = CheegerReduction(enabled=True, components=3, supernodes=4)
    result = compute_cheeger(nx.complete_graph(8), CheegerComputation(reduction=reduction), minimum=3, maximum=5)
    assert not result.exact and result.reason == "BoundsSatisfied"
    assert result.stage == "FreshSpectralReduction"
    assert result.lowerBound == pytest.approx(4)
    assert result.upperBound == pytest.approx(4)
    assert result.evaluatedCuts == 7


def test_cached_partition_is_rescored_and_uncertain_interval_escalates():
    """
    Reuse only low-churn partitions and fall through to exact search when bounds cannot decide.
    """
    clear_reduction_cache()
    settings = CheegerComputation(reduction=CheegerReduction(enabled=True, components=3, supernodes=4, maxEdgeChurn=0.5))
    graph = nx.complete_graph(8)
    compute_cheeger(graph, settings, minimum=3, maximum=5)
    cached = compute_cheeger(graph, settings, minimum=3, maximum=5)
    assert cached.stage == "CachedQuotient" and cached.reason == "BoundsSatisfied"
    uncertain = compute_cheeger(nx.path_graph(8), settings, minimum=0.1, maximum=2)
    assert uncertain.exact and uncertain.stage == "ExactEnumeration"
    assert uncertain.upperBound == pytest.approx(0.25)


def test_spectral_lower_bound_can_prove_a_maximum_violation():
    """
    Reject a maximum only when the certified lower endpoint exceeds it.
    """
    clear_reduction_cache()
    settings = CheegerComputation(reduction=CheegerReduction(enabled=True, components=3, supernodes=4))
    result = compute_cheeger(nx.complete_graph(8), settings, maximum=3)
    assert result.reason == "MaximumViolated"
    assert result.lowerBound == pytest.approx(4)


def test_structural_rule_accepts_a_certified_interval_without_reporting_an_estimate():
    """
    Authorize admission from proven bounds while reserving measurements.cheeger for exact values.
    """
    clear_reduction_cache()
    topology = Topology(
        mode="persistent",
        nodes=tuple(Node(str(index), "Daemon", str(index)) for index in range(8)),
        connections=tuple(Connection(str(left), str(right)) for left, right in nx.complete_graph(8).edges()),
    )
    reduction = CheegerReduction(enabled=True, components=3, supernodes=4)
    verdict = evaluate_policy(
        StructuralPolicy(
            relation="connections",
            cheeger=Cheeger(minimum=3, maximum=5),
            cheegerComputation=CheegerComputation(reduction=reduction),
        ),
        topology,
        expanded_nodes=8,
        nesting_depth=1,
        cheeger_limits=CheegerComputation(reduction=reduction),
    )
    assert verdict["allowed"] and "cheeger" not in verdict["measurements"]
    assert verdict["cheegerComputation"]["reason"] == "BoundsSatisfied"


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


@pytest.mark.parametrize(
    "settings",
    [
        {"enabled": 1},
        {"maxVertices": 1},
        {"components": 0},
        {"supernodes": 1},
        {"cache": "true"},
        {"cacheEntries": 0},
        {"maxEdgeChurn": 1.1},
    ],
)
def test_reduction_controls_validate_public_inputs(settings):
    """
    Reject invalid administrator and policy reduction settings before spectral work.
    """
    with pytest.raises(ValueError):
        CheegerReduction(**settings)


def test_tuning_reference_and_generated_schemas_share_computation_types():
    """
    Validate the complete reference and reject negative targets and malformed search controls at admission.
    """
    root = Path(__file__).parents[2]
    documents = list(yaml.safe_load_all((root / "examples/cheeger-tuning.yaml").read_text()))
    for document in documents:
        # Resource names include irregular plurals such as graphpolicies.
        plural = RESOURCE_TYPES[document["kind"]].plural
        crd = yaml.safe_load((root / f"charts/polyad-crds/crds/{plural}.yaml").read_text())
        schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]

        # Kubernetes OpenAPI uses the boolean exclusiveMinimum form from draft 4.
        jsonschema.Draft4Validator(schema).validate(document)
    for kind in ("graphs", "polygraphs", "rewrites", "graphpolicies"):
        crd = yaml.safe_load((root / f"charts/polyad-crds/crds/{kind}.yaml").read_text())
        spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
        if kind == "rewrites":
            spec = spec["topology"]["properties"]
        if kind != "graphpolicies":
            spec = spec["throughput"]["properties"]
            bounds = spec["tiers"]["items"]["properties"]["cheeger"]
            assert not jsonschema.Draft4Validator(bounds).is_valid({"minimum": -1})
            assert len(bounds["x-kubernetes-validations"]) == 2
        validator = jsonschema.Draft4Validator(spec["cheegerComputation"])
        assert validator.is_valid(
            {
                "maxVertices": 22,
                "maxCuts": 2097151,
                "timeoutSeconds": 15,
                "priorityCuts": [["a", "b"]],
                "reduction": {
                    "enabled": True,
                    "maxVertices": 128,
                    "components": 3,
                    "supernodes": 6,
                    "cache": True,
                    "cacheEntries": 64,
                    "maxEdgeChurn": 0.05,
                },
            }
        )
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
