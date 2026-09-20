"""
Verify preferred adaptive scheduling preserves certificates, limits and boundary isolation.
"""

from __future__ import annotations

import networkx as nx
import pytest
from attrs import evolve

from polyad.graph.cheeger import compute_cheeger
from polyad.graph.pid import AccuracyTargetPID, CacheTargetConfig, certificate_gap
from polyad.graph.reduction import clear_reduction_cache
from polyad.graph.refresh import begin_refresh, finish_refresh
from polyad.operator.policies.cheeger import computation_limits
from polyad_types import CheegerComputation, CheegerReduction


@pytest.fixture(autouse=True)
def clear_histories():
    """
    Keep every test independent of earlier partition and controller histories.
    """
    clear_reduction_cache()
    yield
    clear_reduction_cache()


def settings(**changes):
    """
    Enable bounded reduction on a small graph without changing global feature gates.
    """
    return CheegerComputation(reduction=CheegerReduction(enabled=True, components=2, supernodes=4, **changes))


def test_preferred_default_still_requires_reduction_and_numeric_callers_remain_exact():
    """
    Opt-in and exact numeric semantics do not depend on the new scheduler preference.
    """
    graph = nx.complete_graph(list("abcdefgh"))
    assert not CheegerReduction().enabled and CheegerReduction().strategy == "AdaptivePID"
    disabled = compute_cheeger(graph, maximum=8)
    numeric = compute_cheeger(graph, settings())
    assert disabled.exact and numeric.exact and disabled.scheduler == numeric.scheduler == {}


def test_preferred_scheduler_refreshes_and_reports_both_loops():
    """
    Persistent easy policies exercise actual scheduled refreshes without using oracle feedback.
    """
    graph = nx.complete_graph(list("abcdefgh"))
    reports = [compute_cheeger(graph, settings(), maximum=8, cache_scope="one") for _ in range(24)]
    assert all(item.reason == "BoundsSatisfied" and item.lowerBound <= 4 <= item.upperBound for item in reports)
    assert any(item.scheduler["refreshScheduled"] for item in reports)
    assert any(item.scheduler["outer"]["updated"] for item in reports)
    for item in reports:
        assert 1 <= item.scheduler["inner"]["intervalAfter"] <= 8
        assert 0 <= item.scheduler["outer"]["targetAfter"] <= 0.8
        if item.scheduler["refreshScheduled"]:
            assert item.stage == "FreshSpectralReduction" and not item.scheduler["inner"]["failure"]


def test_cache_first_is_an_explicit_rollback():
    """
    Legacy scheduling keeps using a valid partition instead of periodic refreshes.
    """
    graph = nx.complete_graph(list("abcdefgh"))
    reports = [compute_cheeger(graph, settings(strategy="CacheFirst"), maximum=8) for _ in range(24)]
    assert reports[0].stage == "FreshSpectralReduction"
    assert all(item.stage == "CachedQuotient" and not item.scheduler for item in reports[1:])


def test_scopes_and_membership_isolate_partition_and_pid_history():
    """
    Identical node names in different applications cannot share refresh history.
    """
    graph = nx.complete_graph(list("abcdefgh"))
    first = compute_cheeger(graph, settings(), maximum=8, cache_scope="one")
    cached = compute_cheeger(graph, settings(), maximum=8, cache_scope="one")
    other = compute_cheeger(graph, settings(), maximum=8, cache_scope="two")
    assert first.stage == other.stage == "FreshSpectralReduction" and cached.stage == "CachedQuotient"
    assert first.scheduler["inner"]["ageBefore"] == other.scheduler["inner"]["ageBefore"] == 0
    graph.remove_node("h")
    assert compute_cheeger(graph, settings(), maximum=8, cache_scope="one").stage == "FreshSpectralReduction"


def test_unresolved_bounds_still_need_exact_search():
    """
    PID scheduling cannot turn a quotient estimate into an exact scalar or an unsupported policy pass.
    """
    graph = nx.path_graph(list("abcdefgh"))
    reports = [compute_cheeger(graph, settings(), minimum=0.25) for _ in range(8)]
    assert all(item.exact and item.upperBound == 0.25 for item in reports)
    limited = compute_cheeger(graph, evolve(settings(), maxCuts=1), minimum=0.25)
    assert not limited.exact and limited.reason == "CutBudget"


def test_administrator_owns_time_goal_and_can_disable_pid(monkeypatch):
    """
    Local policy cannot supersede the administrator's time target or rollback choice.
    """
    graph = nx.complete_graph(list("abcdefgh"))
    local = settings(targetSeconds=1)
    limits = settings(targetSeconds=0.02, feedback="ComputationTime")
    report = compute_cheeger(graph, local, limits=limits, maximum=8)
    assert report.scheduler["outer"]["timeTargetSeconds"] == 0.02
    assert not compute_cheeger(graph, local, limits=settings(strategy="CacheFirst"), maximum=8).scheduler
    assert not compute_cheeger(graph, settings(strategy="CacheFirst"), limits=limits, maximum=8).scheduler
    monkeypatch.setenv("POLYAD_CHEEGER_REDUCTION_STRATEGY", "CacheFirst")
    monkeypatch.setenv("POLYAD_CHEEGER_REDUCTION_TARGET_SECONDS", "0.02")
    assert computation_limits().reduction.strategy == "CacheFirst"
    assert computation_limits().reduction.targetSeconds == 0.02


def test_scheduler_cannot_bypass_cache_or_churn_gates():
    """
    A controller request does not grant cache use that the operator prohibited.
    """
    graph = nx.complete_graph(list("abcdefgh"))
    no_cache = settings(cache=False)
    assert not compute_cheeger(graph, no_cache, maximum=8).scheduler
    with pytest.raises(ValueError, match="cache is disabled"):
        compute_cheeger(graph, settings(), limits=no_cache, maximum=8)
    compute_cheeger(graph, settings(maxEdgeChurn=0), maximum=8)
    graph.remove_edge("a", "b")
    changed = compute_cheeger(graph, settings(maxEdgeChurn=0), maximum=8)
    assert changed.stage == "FreshSpectralReduction" and changed.scheduler["inner"]["failure"]


def test_outer_updates_use_completed_time_and_skip_concurrent_stale_tickets():
    """
    Versioned updates avoid holding locks during computation or counting one history twice.
    """
    graph = nx.complete_graph(list("abcdefgh"))
    config = settings(feedback="ComputationTime").reduction
    first, concurrent = begin_refresh(graph, config), begin_refresh(graph, config)
    assert not finish_refresh(first, 0.015, cache_attempted=True, refreshed=True)["updateSkipped"]
    assert finish_refresh(concurrent, 0.015, cache_attempted=True, refreshed=True)["updateSkipped"]
    for _ in range(3):
        report = finish_refresh(begin_refresh(graph, config), 0.015, cache_attempted=True, refreshed=False)
    assert report["outer"]["updated"] and report["outer"]["targetAfter"] > 0.25


def test_lru_eviction_and_reset_do_not_revive_old_feedback():
    """
    Limit retained history and discard results from evicted or cleared tickets.
    """
    config = settings(cacheEntries=1).reduction
    graph = nx.complete_graph(list("abcdefgh"))
    old = begin_refresh(graph, config)
    graph.graph["cheegerCacheScope"] = "other"
    current = begin_refresh(graph, config)
    assert finish_refresh(old, 0.1, cache_attempted=True, refreshed=True)["updateSkipped"]
    clear_reduction_cache()
    assert finish_refresh(current, 0.1, cache_attempted=True, refreshed=True)["updateSkipped"]


@pytest.mark.parametrize(
    "changes", [{"strategy": "unknown"}, {"targetSeconds": True}, {"targetSeconds": 0}, {"targetSeconds": float("nan")}]
)
def test_invalid_scheduler_configuration_is_rejected(changes):
    """
    Reject unbounded or ambiguous goals before starting feedback.
    """
    with pytest.raises(ValueError):
        CheegerReduction(**changes)


@pytest.mark.parametrize("lower,upper,gap", [(0, None, 1), (0, 0, 0), (0, 3, 1), (2, 2, 0), (2, 2.5, 0.2)])
def test_accuracy_signal_handles_missing_and_zero_bounds(lower, upper, gap):
    """
    Missing or zero lower bounds are never mistaken for precise positive estimates.
    """
    assert certificate_gap(lower, upper) == pytest.approx(gap)


def test_accuracy_pid_responds_to_uncertainty_without_time_or_oracle_inputs():
    """
    High uncertainty requests fresh work and tight certificates permit reuse.
    """
    uncertain = AccuracyTargetPID(CacheTargetConfig())
    precise = AccuracyTargetPID(CacheTargetConfig())
    for _ in range(4):
        bad = uncertain.observe(0, 1, 0.25)
        good = precise.observe(1, 1, 0.25)
    assert bad["observedGap"] == 1 and bad["relativeErrorBound"] is None
    assert bad["normalizedError"] < 0 and bad["targetAfter"] == 0 and bad["antiWindup"]
    assert good["normalizedError"] > 0 and good["targetAfter"] > 0.25
    for _ in range(100):
        saturated = uncertain.observe(0, 1, 0.25)
    assert saturated["integral"] == 0 and uncertain.target == 0


def test_accuracy_goal_has_a_certified_relative_error_interpretation():
    """
    A per-observation target hit bounds all exact constants inside that interval.
    """
    controller = AccuracyTargetPID(CacheTargetConfig())
    report = controller.observe(2, 2.5, 0.25)
    assert report["objectiveMet"] and report["relativeErrorBound"] == pytest.approx(0.25)
    for truth in (2, 2.1, 2.5):
        assert (2.5 - truth) / truth <= report["relativeErrorBound"]


def test_accuracy_goal_change_discards_mixed_windows_and_recovers_from_saturation():
    """
    Setpoint changes cannot mix incompatible objectives or create derivative kicks.
    """
    controller = AccuracyTargetPID(CacheTargetConfig())
    for _ in range(4):
        controller.observe(0, 1, 0.25)
    assert controller.target == 0
    for _ in range(3):
        controller.observe(1, 1, 0.25)
    report = controller.observe(1, 1, 1)
    assert report["windowReset"] and report["samples"] == 1 and not report["updated"]
    for _ in range(3):
        report = controller.observe(1, 1, 1)
    assert report["updated"] and report["derivative"] == 0 and controller.target > 0


def test_production_accuracy_feedback_does_not_hide_poor_estimates_behind_exact_fallback():
    """
    The controller sees pre-fallback uncertainty even when the final scalar is exact.
    """
    graph = nx.path_graph(list("abcdefgh"))
    reports = [compute_cheeger(graph, settings(), minimum=0.25) for _ in range(8)]
    assert all(item.exact and item.lowerBound == item.upperBound for item in reports)
    assert all(item.scheduler["feedback"] == "CertificateGap" for item in reports)
    assert all(item.scheduler["outer"]["observedGap"] > 0 for item in reports)
    assert all(item.scheduler["refreshScheduled"] for item in reports[4:])
    assert all(item.scheduler["outer"]["targetAfter"] == 0 for item in reports[3:])


def test_admin_accuracy_settings_override_policy_and_reset_history(monkeypatch):
    """
    A workload cannot replace the administrator's error objective with a time goal.
    """
    graph = nx.complete_graph(list("abcdefgh"))
    report = compute_cheeger(graph, settings(feedback="ComputationTime"), limits=settings(targetRelativeError=0.1), maximum=8)
    assert report.scheduler["feedback"] == "CertificateGap"
    assert report.scheduler["outer"]["relativeErrorTarget"] == 0.1
    fresh = compute_cheeger(graph, settings(targetRelativeError=0.5), maximum=8)
    assert fresh.scheduler["inner"]["ageBefore"] == 0
    monkeypatch.setenv("POLYAD_CHEEGER_REDUCTION_FEEDBACK", "CertificateGap")
    monkeypatch.setenv("POLYAD_CHEEGER_REDUCTION_TARGET_RELATIVE_ERROR", "0.1")
    assert computation_limits().reduction.targetRelativeError == 0.1


@pytest.mark.parametrize(
    "changes", [{"feedback": "unknown"}, {"targetRelativeError": 0}, {"targetRelativeError": True}, {"targetRelativeError": float("inf")}]
)
def test_invalid_accuracy_settings_are_rejected(changes):
    """
    Enforce finite, bounded and unambiguous public controller settings.
    """
    with pytest.raises(ValueError):
        CheegerReduction(**changes)


@pytest.mark.parametrize("lower,upper", [(-1, 1), (float("nan"), 1), (1, float("inf")), (1, float("nan")), (2, 1)])
def test_invalid_certificates_cannot_enter_accuracy_feedback(lower, upper):
    """
    Reject invalid input instead of letting NaN or inverted bounds poison PID history.
    """
    with pytest.raises(ValueError):
        certificate_gap(lower, upper)


@pytest.mark.parametrize("target", [0, -1, True, float("nan"), float("inf")])
def test_accuracy_controller_rejects_invalid_objectives(target):
    """
    Validate direct controller callers as well as public graph-policy settings.
    """
    with pytest.raises(ValueError):
        AccuracyTargetPID(CacheTargetConfig()).observe(1, 2, target)
