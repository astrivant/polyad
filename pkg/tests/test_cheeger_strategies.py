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
from polyad_benchmarks.studies.cheeger_strategies.experiment import (
    Experiment,
    edge_churn,
    pid_report,
    policy_decision,
    rewire,
    trace_selector,
)
from polyad_benchmarks.studies.cheeger_strategies.pid import AccuracyTargetPID, CacheTargetConfig, CacheTargetPID, RefreshConfig, RefreshPID

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
        "pidFeedback": {**config["pidFeedback"], "observationsPerPhase": 4},
        "pidAccuracy": {**config["pidAccuracy"], "observationsPerPhase": 4},
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
        experiment.pid_feedback()
        experiment.pid_accuracy()
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
        "pid-feedback",
        "pid-accuracy",
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
        assert row["cpuSeconds"] > 0
        assert row["averageMillicores"] == pytest.approx(1000 * row["cpuSeconds"] / row["durationSeconds"])
        assert row["millicoreSeconds"] == pytest.approx(1000 * row["cpuSeconds"])
        if "accuracyPid" in row:
            assert row["accuracyPid"]["observedGap"] == pytest.approx(row["certificateGap"])
            assert row["accuracyPid"]["relativeErrorTarget"] == row.get("relativeErrorTarget", 0.25)
        if row["upperBound"] is not None:
            snapshot = experiment.graphs[row["graphId"]]
            graph = nx.Graph(snapshot["edges"])
            graph.add_nodes_from(snapshot["nodes"])
            assert nx.cut_size(graph, row["cut"]) / min(len(row["cut"]), len(graph) - len(row["cut"])) == pytest.approx(row["upperBound"])
        if row["decision"] == "Unknown":
            assert row["decisionMatchesOracle"] is None


def test_cpu_clock_excludes_oracle_and_audit_but_wraps_computation(monkeypatch):
    """
    Measure the algorithm itself rather than charging independent reference work to it.
    """
    from polyad_benchmarks.studies.cheeger_strategies import experiment as implementation

    experiment = Experiment(recipe())
    graph = experiment.graph("path", 8, 11)
    events = []
    snapshot, fresh, decision = experiment.snapshot, implementation.fresh_spectral_reduction, implementation.policy_decision
    ticks = iter((10.0, 10.003))

    def observed_snapshot(graph):
        events.append("oracle")
        return snapshot(graph)

    def observed_fresh(*args, **kwargs):
        events.append("compute")
        return fresh(*args, **kwargs)

    def observed_decision(*args, **kwargs):
        events.append("audit")
        return decision(*args, **kwargs)

    def cpu_clock():
        events.append("cpu")
        return next(ticks)

    monkeypatch.setattr(experiment, "snapshot", observed_snapshot)
    monkeypatch.setattr(implementation, "fresh_spectral_reduction", observed_fresh)
    monkeypatch.setattr(implementation, "policy_decision", observed_decision)
    monkeypatch.setattr(implementation.time, "process_time", cpu_clock)
    experiment.record(graph, graph, experiment.settings(), {"maximum": 8}, {}, "Fresh spectral")
    assert events == ["oracle", "oracle", "cpu", "compute", "cpu", "audit", "audit"]
    assert experiment.records[0]["millicoreSeconds"] == pytest.approx(3)


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


def test_pid_failure_is_cache_entry_not_a_wrong_certificate():
    """
    Count even a successful, certified cache hit as the requested controller failure.
    """
    experiment = Experiment(recipe())
    settings = experiment.settings()
    graph = experiment.graph("path", 8, 11)
    controller = RefreshPID(RefreshConfig())
    try:
        experiment.prime(graph, settings)
        first = pid_report(graph, settings.reduction, controller)
        second = pid_report(graph, settings.reduction, controller)
        third = pid_report(graph, settings.reduction, controller)
        assert first["cacheHit"] and first["pid"]["failure"]
        assert first["pid"]["intervalBefore"] == 4
        assert first["pid"]["intervalAfter"] == pytest.approx(3)
        assert second["pid"]["intervalAfter"] == pytest.approx(2.6)
        assert third["stage"] == "FreshSpectralReduction"
        assert third["pid"]["refreshScheduled"] and not third["pid"]["failure"]
        assert not third["cacheHit"]
        assert [attempt["stage"] for attempt in third["attempts"]] == ["FreshSpectralReduction"]
        assert third["pid"]["derivative"] == -1
        assert third["pid"]["intervalAfter"] == pytest.approx(3.4)
    finally:
        clear_reduction_cache()


def test_pid_cache_miss_records_both_attempts_and_refreshes():
    """
    A missing partition incurs a controller failure and real, timed fresh work.
    """
    experiment = Experiment(recipe())
    clear_reduction_cache()
    try:
        report = pid_report(experiment.graph("path", 8, 11), experiment.settings().reduction, RefreshPID(RefreshConfig()))
        assert report["pid"]["failure"] and report["pid"]["refreshed"]
        assert not report["pid"]["refreshScheduled"] and not report["cacheHit"]
        assert report["pid"]["ageAfter"] == 0
        assert [attempt["stage"] for attempt in report["attempts"]] == ["CachedQuotient", "FreshSpectralReduction"]
        assert report["totalEvaluatedCuts"] == sum(attempt["evaluatedCuts"] for attempt in report["attempts"])
    finally:
        clear_reduction_cache()


def test_pid_clamps_intervals_and_prevents_integral_windup():
    """
    Saturating failures cannot accumulate hidden integral debt or invalid periods.
    """
    controller = RefreshPID(RefreshConfig(proportionalGain=100, integralGain=1))
    for _ in range(100):
        state = controller.observe(cache_attempted=True, refreshed=False)
        assert state["intervalAfter"] == 1
        assert state["antiWindup"] and state["integral"] == 0
    state = controller.observe(cache_attempted=False, refreshed=True)
    assert state["intervalAfter"] == pytest.approx(4.2)

    # Integration must also stop when a positive target pushes the upper limit.
    controller = RefreshPID(RefreshConfig(proportionalGain=100, targetCacheRate=1))
    for _ in range(100):
        state = controller.observe(cache_attempted=False, refreshed=True)
        assert state["intervalAfter"] == 8
        assert state["antiWindup"] and state["integral"] == 0


def test_zero_pid_gains_reproduce_a_fixed_refresh_interval():
    """
    A disabled feedback gain preserves an ordinary periodic refresh schedule.
    """
    controller = RefreshPID(RefreshConfig(proportionalGain=0, integralGain=0, derivativeGain=0, initialInterval=3))
    actions = []
    for _ in range(9):
        scheduled = controller.refresh_due()
        actions.append(scheduled)
        state = controller.observe(cache_attempted=not scheduled, refreshed=scheduled)
        assert state["intervalAfter"] == 3
        assert state["integral"] == 0
    assert actions == [False, False, True] * 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"proportionalGain": -1},
        {"integralGain": float("nan")},
        {"derivativeGain": float("inf")},
        {"targetCacheRate": 2},
        {"minInterval": 0},
        {"initialInterval": 9},
        {"maxInterval": 2},
        {"enabled": 1},
        {"proportionalGain": True},
    ],
)
def test_pid_rejects_invalid_controls(overrides):
    """
    Reject nonfinite or inconsistent controls before the expensive study begins.
    """
    with pytest.raises(ValueError):
        RefreshConfig(**overrides)


def test_pid_churn_replays_matched_graphs_with_independent_repeat_state():
    """
    Preserve controller history across levels but never between timing repeats.
    """
    config = {**recipe(), "edgeReplacement": [0, 0.05, 0.15, 0.3, 0.6], "repetitions": 2}
    experiment = Experiment(config)
    try:
        experiment.pid_churn()
    finally:
        clear_reduction_cache()
    for topology in config["topologies"]:
        baseline = experiment.graph(topology, 8, 11)
        histories = []
        for repeat in range(2):
            rows = [row for row in experiment.records if row["topology"] == topology and row["repeat"] == repeat]
            assert [row["step"] for row in rows] == list(range(5))
            assert rows[0]["pid"]["intervalBefore"] == 4
            assert rows[0]["pid"]["ageBefore"] == 0
            for row in rows:
                expected, _ = experiment.snapshot(rewire(baseline, row["replacement"], 11))
                assert row["graphId"] == expected
                assert row["intervalContainsExact"] and row["decisionMatchesOracle"] is not False
                assert row["pid"]["failure"] == row["pid"]["cacheAttempted"]
            histories.append([row["pid"] for row in rows])
        assert histories[0] == histories[1]


@pytest.mark.parametrize("block", [None, {"enabled": False}])
def test_pid_comparator_is_optional_and_does_not_change_old_recipes(block):
    """
    Missing or disabled experiment configuration produces no extra measurements.
    """
    config = recipe()
    if block is None:
        config.pop("pidRefresh")
    else:
        config["pidRefresh"] = block
    experiment = Experiment(config)
    experiment.pid_churn()
    assert not experiment.records


def test_outer_pid_changes_target_only_after_a_complete_past_window():
    """
    Expensive observations increase requested reuse without acting on future samples.
    """
    controller = CacheTargetPID(CacheTargetConfig(updateEvery=4))
    for _ in range(3):
        report = controller.observe(0.002, 0.001)
        assert not report["updated"] and controller.target == 0.25
        assert report["normalizedError"] is None
    report = controller.observe(0.002, 0.001)
    assert report["updated"] and report["samples"] == 4
    assert report["normalizedError"] == pytest.approx(1)
    assert controller.target == pytest.approx(0.5)
    for _ in range(4):
        report = controller.observe(0.0001, 0.001)
    assert controller.target < 0.25


def test_outer_pid_resets_partial_windows_and_saturates_without_windup():
    """
    Time-goal changes cannot mix objectives or accumulate unbounded actuator debt.
    """
    controller = CacheTargetPID(CacheTargetConfig(updateEvery=4, proportionalGain=100))
    for _ in range(3):
        controller.observe(0.1, 0.001)
    report = controller.observe(0.001, 0.002)
    assert report["windowReset"] and report["samples"] == 1
    assert not report["updated"]
    for _ in range(3):
        report = controller.observe(0.001, 0.002)
    assert report["derivative"] == 0
    assert report["antiWindup"] and report["integral"] == 0
    assert controller.target == 0
    for _ in range(20):
        report = controller.observe(1, 0.002)
    assert report["antiWindup"] and report["integral"] == 0
    assert controller.target == 0.8


def test_changing_inner_target_does_not_introduce_derivative_kick():
    """
    Only changed cache events, not outer setpoint adjustments, drive the derivative.
    """
    controller = RefreshPID(RefreshConfig())
    controller.observe(cache_attempted=True, refreshed=False)
    controller.set_target(0.5)
    report = controller.observe(cache_attempted=True, refreshed=False)
    assert report["targetCacheRate"] == 0.5
    assert report["error"] == 0.5 and report["derivative"] == 0


@pytest.mark.parametrize(
    "settings",
    [
        {"updateEvery": 1},
        {"updateEvery": True},
        {"initialCacheRate": 2},
        {"minCacheRate": 0.9},
        {"integralGain": -1},
        {"derivativeGain": float("nan")},
    ],
)
def test_outer_pid_validates_configuration(settings):
    """
    Reject malformed outer-loop parameters before a measured replay starts.
    """
    with pytest.raises(ValueError):
        CacheTargetConfig(**settings)


def test_feedback_with_zero_outer_gains_matches_fixed_target_actions():
    """
    Isolate the outer feedback effect with paired trajectories and identical initial state.
    """
    config = recipe()
    config["pidFeedback"]["controller"] = {
        **config["pidFeedback"]["controller"],
        "proportionalGain": 0,
        "integralGain": 0,
        "derivativeGain": 0,
    }
    experiment = Experiment(config)
    try:
        experiment.pid_feedback()
    finally:
        clear_reduction_cache()
    fixed = [row for row in experiment.records if row["strategy"] == "Fixed-target PID"]
    adaptive = [row for row in experiment.records if row["strategy"] == "Adaptive-target PID"]
    assert len(fixed) == len(adaptive) == 20
    for left, right in zip(fixed, adaptive, strict=True):
        assert left["graphId"] == right["graphId"]
        assert left["pid"] == right["pid"]
        assert left["upperBound"] == right["upperBound"]
        assert left["cut"] == right["cut"]
        assert right["outerPid"]["targetAfter"] == 0.25
        assert right["outerPid"]["observedDurationSeconds"] <= right["durationSeconds"]
        assert right["outerPid"]["updated"] == (right["phaseStep"] == 3)


@pytest.mark.parametrize("block", [None, {"enabled": False}])
def test_outer_pid_replay_is_optional(block):
    """
    Keep old and explicitly disabled recipes free of additional feedback work.
    """
    config = recipe()
    if block is None:
        config.pop("pidFeedback")
    else:
        config["pidFeedback"] = block
    experiment = Experiment(config)
    experiment.pid_feedback()
    assert not experiment.records


def test_accuracy_comparator_matches_runtime_scheduling_for_the_same_certificates():
    """
    Study refresh decisions follow the production controller without using study truth.
    """
    experiment = Experiment(recipe())
    graph = experiment.graph("path", 8, 11)
    settings = experiment.settings(maxEdgeChurn=1)
    experiment.prime(graph, settings)
    runtime = [trace_selector(graph, settings, {"maximum": 8}) for _ in range(12)]
    experiment.prime(graph, settings)
    accuracy = AccuracyTargetPID(CacheTargetConfig())
    inner = RefreshPID(RefreshConfig(targetCacheRate=accuracy.target))
    try:
        for reference in runtime:
            report = pid_report(graph, settings.reduction, inner, force_refresh=accuracy.target == 0)
            feedback = accuracy.observe(report["lowerBound"], report["upperBound"], settings.reduction.targetRelativeError)
            inner.set_target(accuracy.target)
            assert report["stage"] == reference["stage"]
            assert feedback == reference["scheduler"]["outer"]
    finally:
        clear_reduction_cache()
