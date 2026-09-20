"""
Verify paired strategy measurements, independent references and real tier observations.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import networkx as nx
import pytest

from polyad.graph.reduction import clear_reduction_cache
from polyad_benchmarks import refresh
from polyad_benchmarks.studies.cheeger_strategies.experiment import Experiment, edge_churn, policy_decision, rewire, trace_selector

ROOT = Path(__file__).parents[2]


def test_refresh_registers_active_study_and_fingerprints_production_solver():
    """
    Keep the reproducible suite coupled to the actual production algorithm sources.
    """
    assert refresh.CHEEGER_STUDIES == ("cheeger-strategies",)
    assert set(refresh.CHEEGER_STUDIES) <= set(refresh.LOCAL_STUDIES)
    assert "cheeger-reduction" not in refresh.REACHABILITY_STUDIES + refresh.LOCAL_STUDIES
    sources = refresh.sources(ROOT)
    assert "pkg/polyad/graph/cheeger.py" in sources
    assert "pkg/polyad/graph/reduction.py" in sources
    assert "studies/cheeger-strategies/fixtures/scenario.json" in sources
    assert not any(path.startswith("studies/cheeger-reduction") for path in sources)


@pytest.mark.parametrize("study", ["cheeger-reduction", "cheeger-reduction-deprecated"])
def test_refresh_rejects_deprecated_study_in_new_and_prepared_runs(tmp_path, study):
    """
    Neither the original study name nor its archive directory can enter a refresh.
    """
    root = tmp_path / "refresh"
    with pytest.raises(ValueError, match="registered studies"):
        refresh.prepare(ROOT, root, (study,))
    assert not root.exists()
    refresh.write_json(root / "provenance.json", {"matrix": {"study": [study]}, "inputs": {study: "old-digest"}})
    with pytest.raises(ValueError, match="study inventory"):
        refresh.selected_studies(root)


@pytest.mark.parametrize("suite", ["cluster", "local", "reachability", "cheeger", "process", "all"])
def test_refresh_cli_suites_exclude_deprecated_study(tmp_path, monkeypatch, capsys, suite):
    """
    Every generated local and CI matrix excludes the archived experiment.
    """
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["refresh", "--ci-phase", "prepare", "--suite", suite, "--project", str(ROOT), "--root", str(tmp_path / suite)]
    )
    refresh.main()
    matrix = json.loads(capsys.readouterr().out)["study"]
    assert not any(study.startswith("cheeger-reduction") for study in matrix)
    if suite == "cheeger":
        assert matrix == ["cheeger-strategies"]


def recipe():
    """
    Reduce the published recipe while retaining every experimental axis.
    """
    config = json.loads((ROOT / "studies/cheeger-strategies/fixtures/scenario.json").read_text())
    return {
        **config,
        "fixedVertices": 8,
        "fixedComponents": 2,
        "fixedSupernodes": 4,
        "topologies": ["path", "community"],
        "seeds": [11],
        "repetitions": 1,
        "components": [1, 2],
        "supernodes": [2, 4],
        "vertexCounts": [8],
        "densities": [0.25, 0.75],
        "edgeReplacement": [0, 0.3],
        "cacheChurnLimits": [0, 0.15, 1],
        "thresholdRatios": [0.25, 1, 2],
        "cacheEntries": [1, 4],
    }


@pytest.mark.parametrize("topology", [nx.path_graph, nx.cycle_graph, nx.complete_graph])
def test_churn_is_reproducible_and_measures_achieved_changes(topology):
    """
    Preserve topology contracts even for bridges or saturated graphs.
    """
    baseline = nx.relabel_nodes(topology(10), str)
    changed = rewire(baseline, 0.3, 42)
    assert nx.is_connected(changed)
    assert set(changed) == set(baseline)
    assert changed.number_of_edges() == baseline.number_of_edges()
    assert set(changed.edges()) == set(rewire(baseline, 0.3, 42).edges())
    assert 0 <= edge_churn(baseline, changed) <= 1
    if topology is nx.complete_graph:
        assert edge_churn(baseline, changed) == 0
    else:
        assert edge_churn(baseline, changed) > 0


def test_trace_records_real_cache_refresh_and_exact_fallback():
    """
    Observe actual runtime stages without classifying them from graph size alone.
    """
    experiment = Experiment(recipe())
    settings = experiment.settings()
    graph = experiment.graph("path", 8, 11)
    clear_reduction_cache()

    # Hold topology fixed while cache state and policy precision force each production tier.
    fresh = trace_selector(graph, settings, {"maximum": 8})
    cached = trace_selector(graph, settings, {"maximum": 8})
    exact = trace_selector(graph, settings, {"minimum": 0.25})
    assert fresh["stage"] == "FreshSpectralReduction" and not fresh["cacheHit"]
    assert not any(attempt["cacheHit"] for attempt in fresh["attempts"])
    assert fresh["attempts"][-1]["certificateReturned"]
    assert cached["stage"] == "CachedQuotient" and cached["cacheHit"]
    assert cached["attempts"][0]["edgeChurn"] == 0
    assert exact["stage"] == "ExactEnumeration" and exact["exact"]
    assert exact["totalEvaluatedCuts"] == 127 + 7 + 7
    clear_reduction_cache()


def test_rewiring_is_independent_of_python_hash_randomization():
    """
    Canonicalize undirected nonedges before shuffling across separate Python processes.
    """
    code = "\n".join(
        (
            "import json, networkx as nx",
            "from polyad_benchmarks.studies.cheeger_strategies.experiment import rewire",
            "graph = nx.path_graph([f'v{i:03d}' for i in range(16)])",
            "print(json.dumps(sorted(sorted(edge) for edge in rewire(graph, 0.6, 11).edges())))",
        )
    )
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": str(seed), "PYTHONPATH": os.pathsep.join(sys.path)},
            text=True,
        )
        for seed in (1, 42)
    ]
    assert outputs[0] == outputs[1]


def test_all_sweeps_keep_oracle_certificates_and_cut_witnesses_consistent():
    """
    Audit every sampled method on reconstructable graphs and keep unknown decisions explicit.
    """
    experiment = Experiment(recipe())
    try:
        experiment.comparisons()
        experiment.thresholds()
        experiment.controls()
        experiment.timeline()
    finally:
        clear_reduction_cache()
    assert {row["sweep"] for row in experiment.records} == {
        "churn",
        "reduction",
        "size",
        "density",
        "threshold",
        "controls",
        "cache-capacity",
        "timeline",
    }
    assert {row["stage"] for row in experiment.records if row["sweep"] == "timeline"} == {
        "CachedQuotient",
        "FreshSpectralReduction",
        "ExactEnumeration",
    }
    for row in experiment.records:
        assert row["intervalContainsExact"]
        assert row["decisionMatchesOracle"] is not False
        assert row["durationSeconds"] > 0
        if row["upperBound"] is not None:
            snapshot = experiment.graphs[row["graphId"]]
            graph = nx.Graph(snapshot["edges"])
            graph.add_nodes_from(snapshot["nodes"])
            assert nx.cut_size(graph, row["cut"]) / min(len(row["cut"]), len(graph) - len(row["cut"])) == pytest.approx(row["upperBound"])
        if row["decision"] == "Unknown":
            assert row["decisionMatchesOracle"] is None


def test_policy_probe_distinguishes_cut_accuracy_from_decision_correctness():
    """
    Conservative intervals can prove a policy without knowing its exact constant.
    """
    assert policy_decision(0, 1, {"minimum": 2}) == "Violate"
    assert policy_decision(2, 5, {"minimum": 1}) == "Pass"
    assert policy_decision(0, 5, {"minimum": 1}) == "Unknown"
    assert policy_decision(0, 1, {"maximum": 2}) == "Pass"
    assert policy_decision(3, 5, {"maximum": 2}) == "Violate"
    assert policy_decision(0, None, {"maximum": 2}) == "Unknown"
