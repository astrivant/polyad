"""
Verify modular adaptation, independent retries, fresh context and bounded profile proposals.
"""

from __future__ import annotations

import inspect
import logging
import os
from dataclasses import replace

import pytest

from polyad_sdk import (
    AdaptationStrategy,
    CallbackStrategy,
    Change,
    DecisionStrategy,
    Delta,
    Environment,
    FreshnessStrategy,
    ObserveStrategy,
    ResourceStrategy,
    ThresholdStrategy,
    TopologyStrategy,
)
from tests.test_sdk import RecordingService, observation
from tests.test_sdk import runtime as runtime


def configured(runtime, strategies, *, checkpoint=None):
    """
    Attach strategy components to a real SDK runtime with a deterministic observation clock.
    """
    original, _, clock, _ = runtime
    return RecordingService(original.identity, original.events, strategies=strategies, clock=clock, checkpoint=checkpoint)


def test_public_strategy_contract_and_constructor_validation(runtime):
    """
    Require real strategy implementations and freeze the component order at construction.
    """
    from polyad_sdk.symbiosis import AdaptationStrategy as Exported

    assert Exported is AdaptationStrategy and inspect.isabstract(AdaptationStrategy)
    with pytest.raises(TypeError, match="abstract.*adapt"):
        AdaptationStrategy()
    with pytest.raises(TypeError, match="implement AdaptationStrategy"):
        configured(runtime, [object()])
    original, _, _, _ = runtime
    original.events.topology.assert_not_called()
    components = [ObserveStrategy()]
    service = configured(runtime, components)
    components.clear()
    assert len(service.strategies) == 1


@pytest.mark.parametrize("options", [{}, {"strategies": []}, {"strategies": ()}])
def test_strategies_are_required_at_construction_before_any_observation(runtime, options):
    """
    Reject omitted or empty policies before clocks, clients or adaptation hooks can run.
    """
    original, _, clock, _ = runtime
    clock.reset_mock()
    with pytest.raises(ValueError, match="at least one adaptation strategy"):
        RecordingService(original.identity, original.events, clock=clock, **options)
    clock.assert_not_called()
    original.events.topology.assert_not_called()
    with pytest.raises(ValueError, match="at least one adaptation strategy"):
        RecordingService.from_environment(environ={}, **options)


def test_explicit_opt_out_preserves_application_delivery_without_inventing_strategies(runtime):
    """
    An explicit opt-out permits an empty set while the normal observation lifecycle remains intact.
    """
    original, _, clock, _ = runtime
    checkpoints = []
    service = RecordingService(original.identity, original.events, require_strategies=False, clock=clock, checkpoint=checkpoints.append)
    assert service.strategies == ()
    service.refresh()
    service.dispatch(observation())
    assert len(service.changes) == 2 and checkpoints == ["0-0", "1-0"]


def test_opt_out_does_not_disable_type_or_constraint_name_checks(runtime):
    """
    Disabling the presence requirement keeps supplied components valid and unambiguous.
    """
    original, _, _, _ = runtime
    with pytest.raises(TypeError, match="implement AdaptationStrategy"):
        RecordingService(original.identity, original.events, strategies=[object()], require_strategies=False)
    with pytest.raises(ValueError, match="unique"):
        RecordingService(
            original.identity,
            original.events,
            strategies=[FreshnessStrategy("same", lambda _: None), FreshnessStrategy("same", lambda _: None)],
            require_strategies=False,
        )
    with pytest.raises(TypeError, match="implement AdaptationStrategy"):
        RecordingService.from_environment(environ={}, strategies=[object()], require_strategies=False)


@pytest.mark.parametrize("value", [None, "false", "true", 0, 1])
def test_requirement_switch_accepts_only_booleans(runtime, value):
    """
    Environment strings and truthy values cannot silently disable required configuration.
    """
    original, _, _, _ = runtime
    with pytest.raises(TypeError, match="require_strategies must be a boolean"):
        RecordingService(original.identity, original.events, strategies=[ObserveStrategy()], require_strategies=value)
    with pytest.raises(TypeError, match="require_strategies must be a boolean"):
        RecordingService.from_environment(environ={}, strategies=[ObserveStrategy()], require_strategies=value)


def test_ordered_components_precede_service_hooks_and_checkpoint(runtime):
    """
    Preserve explicit component order through application reconciliation and cursor persistence.
    """
    calls = []
    service = configured(
        runtime,
        [
            CallbackStrategy(lambda change, current: calls.append("first")),
            CallbackStrategy(lambda change, current: calls.append("second")),
        ],
        checkpoint=lambda cursor: calls.append(("checkpoint", cursor)),
    )
    service.on_change(lambda change: calls.append(("hook", len(service.changes))))
    assert calls == []
    service.refresh()
    assert calls == ["first", "second", ("hook", 1), ("checkpoint", "0-0")]
    service.refresh()
    assert len(calls) == 4


def test_failed_component_retries_with_fresh_context_and_isolated_payload(runtime):
    """
    Resume at the failure, re-evaluate freshness and keep successful components committed locally.
    """
    calls, checkpoints = [], []
    attempts = 0

    def first(change, current):
        if change.event:
            change.event.data["resources"]["pods"] = 999
            calls.append("first")

    def second(change, current):
        nonlocal attempts
        if change.event:
            attempts += 1
            assert change.event.data["resources"]["pods"] == 3
            calls.append(("second", current.available))
            if attempts == 1:
                raise RuntimeError("component unavailable")

    service = configured(runtime, [CallbackStrategy(first), CallbackStrategy(second)], checkpoint=checkpoints.append)
    service.refresh()
    event = observation()
    event.data["resources"] = {"pods": 3}
    with pytest.raises(RuntimeError, match="component unavailable"):
        service.dispatch(event)
    assert len(service.changes) == 1 and service.cursor == "0-0"
    assert checkpoints == ["0-0"] and event.data["resources"]["pods"] == 3
    runtime[2].return_value = 200
    service.dispatch(event)
    assert calls == ["first", ("second", True), ("second", False)]
    assert len(service.changes) == 2 and service.cursor == "1-0"
    assert checkpoints == ["0-0", "1-0"]


def test_later_hook_failure_does_not_repeat_successful_strategies(runtime):
    """
    Retry a failing downstream hook without repeating strategy or service effects.
    """
    calls, failures = [], []
    service = configured(runtime, [CallbackStrategy(lambda *_: calls.append("strategy"))])

    def hook(change):
        if change.event and not failures:
            failures.append(True)
            raise RuntimeError("hook unavailable")

    service.on_change(hook)
    service.refresh()
    event = observation()
    with pytest.raises(RuntimeError, match="hook unavailable"):
        service.dispatch(event)
    service.dispatch(event)
    assert calls == ["strategy", "strategy"]
    assert len(service.changes) == 2 and service.cursor == "1-0"


def test_prefiltered_components_handle_baselines_removals_and_unavailability(runtime):
    """
    Select meaningful concerns while ensuring every component sees lost availability.
    """
    calls = []
    service = configured(
        runtime,
        [
            TopologyStrategy(lambda *_: calls.append("topology")),
            ResourceStrategy(lambda *_: calls.append("resources")),
            DecisionStrategy(lambda *_: calls.append("decision")),
        ],
    )
    service.refresh()
    assert calls == ["topology", "resources", "decision"]
    calls.clear()
    event = observation()
    event.data["resources"] = {"pods": 3}
    event.data["status"]["throughput"] = {"phase": "Proposed"}
    service.dispatch(event)
    assert calls == ["resources", "decision"]
    calls.clear()
    replacement = observation(cursor="2-0")
    service.dispatch(replacement)
    assert calls == ["resources", "decision"]
    calls.clear()
    runtime[1]["terminating"] = True
    service.refresh()
    assert calls == ["topology", "resources", "decision"]
    assert not service.view.available


@pytest.mark.parametrize("paths", [("",), ("resources..pods",), (".resources",), (1,)])
def test_callback_paths_reject_ambiguous_components(paths):
    """
    Reject malformed selections before delivery starts.
    """
    with pytest.raises(ValueError, match="delta prefixes"):
        CallbackStrategy(lambda *_: None, paths=paths)


def test_observe_strategy_uses_application_logging(runtime, caplog):
    """
    Log changed paths and availability without dumping resource payloads.
    """
    service = configured(runtime, [ObserveStrategy()])
    with caplog.at_level(logging.INFO, logger="polyad_sdk.symbiosis"):
        service.refresh()
        event = observation()
        event.data["resources"] = {"privateDetail": "do-not-log-values"}
        service.dispatch(event)
    assert "Adaptation baseline" in caplog.text and "resources" in caplog.text
    assert "do-not-log-values" not in caplog.text


def test_factory_passes_components_to_the_concrete_subclass(monkeypatch):
    """
    Preserve injected policy components while constructing authenticated clients from the environment.
    """
    for name, value in {
        "GRAPH_NAMESPACE": "test",
        "GRAPH_KIND": "Graph",
        "GRAPH_NAME": "pipeline",
        "GRAPH_UID": "uid-pipeline",
        "NODE_NAME": "source",
        "EVENTS_URL": "https://events.example",
        "EVENTS_TOKEN": "reader",
    }.items():
        monkeypatch.setenv("POLYAD_" + name, value)
    strategy = ObserveStrategy()
    service = RecordingService.from_environment(strategies=[strategy], environ=os.environ)
    assert service.strategies == (strategy,) and service.changes == []
    unchecked = RecordingService.from_environment(require_strategies=False, environ=os.environ)
    assert unchecked.strategies == () and unchecked.changes == []


def measured(value, *, available=True):
    """
    Construct an application resource view with explicit presence and availability.
    """
    view = Environment(
        {"graph": {"uid": "graph-1"}},
        {"graph-1": {"resources": value, "status": {}}},
        {},
        available,
        None if available else "stale",
    )
    return Change(view, view, (Delta(("resources",), "added", None, value),), False), view


def test_thresholds_propose_profiles_without_assuming_they_were_applied():
    """
    Retain hysteresis, read committed state and leave rejected proposals eligible for reconsideration.
    """
    active, proposals = ["interactive"], []
    strategy = ThresholdStrategy(
        "queue.depth",
        low=2,
        high=8,
        idle="interactive",
        busy="batch",
        active=lambda: active[0],
        propose=lambda profile, *_: proposals.append(profile),
    )
    strategy.adapt(*measured({"queue": {"depth": 8}}))
    strategy.adapt(*measured({"queue": {"depth": 9}}))
    assert proposals == ["batch", "batch"] and active == ["interactive"]
    active[0] = "batch"
    strategy.adapt(*measured({"queue": {"depth": 9}}))
    strategy.adapt(*measured({"queue": {"depth": 5}}))
    assert proposals == ["batch", "batch"]
    strategy.adapt(*measured({"queue": {"depth": 2}}))
    assert proposals[-1] == "interactive"


@pytest.mark.parametrize(
    "value", [None, {}, {"backlog": True}, {"backlog": "9"}, {"backlog": float("nan")}, {"backlog": float("inf")}, {"backlog": 10**1000}]
)
def test_thresholds_hold_when_measurements_are_unknown_or_invalid(value):
    """
    Never infer an idle or busy profile from missing or unusable observations.
    """
    proposals = []
    strategy = ThresholdStrategy(
        "backlog", low=2, high=8, idle="idle", busy="busy", active=lambda: "busy", propose=lambda *args: proposals.append(args)
    )
    strategy.adapt(*measured(value))
    strategy.adapt(*measured({"backlog": 0}, available=False))
    assert proposals == []


def test_thresholds_ignore_unrelated_changes_and_reject_unknown_profiles():
    """
    Keep topology notifications from becoming demand samples or selecting an unapproved profile.
    """
    strategy = ThresholdStrategy(
        "backlog",
        low=2,
        high=8,
        idle="idle",
        busy="busy",
        active=lambda: "unapproved",
        propose=lambda *_: pytest.fail("unexpected proposal"),
    )
    change, current = measured({"backlog": 12})
    strategy.adapt(replace(change, deltas=(Delta(("topology",), "added", None, {}),)), current)
    with pytest.raises(ValueError, match="approved pair"):
        strategy.adapt(change, current)


def test_thresholds_reconsider_fresh_pressure_after_topology_recovers():
    """
    Previously unusable measurements can justify a proposal when admission context recovers.
    """
    proposals = []
    strategy = ThresholdStrategy(
        "backlog", low=2, high=8, idle="idle", busy="busy", active=lambda: "idle", propose=lambda target, *_: proposals.append(target)
    )
    strategy.adapt(*measured({"backlog": 12}, available=False))
    change, current = measured({"backlog": 12})
    strategy.adapt(replace(change, deltas=(Delta(("available",), "changed", False, True),)), current)
    assert proposals == ["busy"]


@pytest.mark.parametrize(
    "options", [{"low": 8}, {"low": True}, {"high": float("nan")}, {"high": 10**1000}, {"idle": "busy"}, {"metric": "queue..depth"}]
)
def test_threshold_configuration_is_validated_before_use(options):
    """
    Reject invalid thresholds and profile catalogs during component construction.
    """
    config = dict(metric="backlog", low=2, high=8, idle="idle", busy="busy", active=lambda: "idle", propose=lambda *_: None)
    config.update(options)
    with pytest.raises(ValueError):
        ThresholdStrategy(**config)
