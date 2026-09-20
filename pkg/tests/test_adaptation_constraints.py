"""
Exercise application difficulty and recovery through real SDK mutation observations.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from polyad_sdk import (
    ConnectionPermissionStrategy,
    ConstraintStrategy,
    DecisionGuardStrategy,
    Event,
    FreshnessStrategy,
    PeerAvailabilityStrategy,
    ResourceBudgetStrategy,
    ThresholdStrategy,
)
from tests.test_adaptation_strategies import configured, measured
from tests.test_sdk import observation
from tests.test_sdk import runtime as runtime


def test_constraint_contract_and_names_preserve_independent_blockers(runtime):
    """
    Reject incomplete assessment implementations and duplicate names before observation.
    """
    assert inspect.isabstract(ConstraintStrategy)
    with pytest.raises(TypeError, match="abstract.*evaluate"):
        ConstraintStrategy("capacity", lambda _: None)
    with pytest.raises(ValueError, match="unique"):
        configured(runtime, [FreshnessStrategy("same", lambda _: None), FreshnessStrategy("same", lambda _: None)])


def test_rollout_withdraws_unready_or_terminating_peers_then_recovers(runtime):
    """
    A replacement replica exists before it is usable; drain without routing to it early.
    """
    results, ready = {}, {"uid-sink"}
    strategy = PeerAvailabilityStrategy(
        "send",
        lambda result: results.update(send=result),
        usable=lambda peer: any(item["uid"] in ready for item in peer["node"]["executions"] if not item["terminating"]),
    )
    service = configured(runtime, [strategy])
    service.refresh()
    assert results["send"].satisfied
    peer = runtime[1]["outgoing"][0]["node"]
    old = peer["executions"][0]
    old["terminating"] = True
    service.refresh()
    assert not results["send"].satisfied
    peer["executions"].append(dict(old, uid="replacement", terminating=False))
    service.refresh()
    assert results["send"].state == "blocked"

    # Local health changes need no invented Kubernetes event: reassess at admission.
    ready.add("replacement")
    assert strategy.evaluate(service.view).satisfied
    peer["executions"] = [dict(old, uid="replacement", terminating=False, replicas=0)]
    service.refresh()
    assert results["send"].state == "blocked"
    peer["executions"][0]["replicas"] = 1
    service.refresh()
    assert results["send"].satisfied
    runtime[1]["outgoing"] = []
    service.refresh()
    assert results["send"].state == "blocked"


def test_logical_peer_minimum_is_not_replaced_by_replica_count(runtime):
    """
    Ten copies of one peer do not satisfy a policy requiring two distinct destinations.
    """
    strategy = PeerAvailabilityStrategy("redundancy", lambda _: None, usable=lambda _: True, minimum=2)
    service = configured(runtime, [strategy])
    runtime[1]["outgoing"][0]["node"]["executions"][0]["replicas"] = 10
    service.refresh()
    assert strategy.evaluate(service.view).state == "blocked"
    assert strategy.evaluate(replace(service.view, available=False)).state == "unknown"


def test_topology_difficulty_stops_new_admission_and_recovers(runtime):
    """
    Stop assigning under stale or draining state; permit consideration after recovery.
    """
    assessments = []
    strategy = FreshnessStrategy("admission", assessments.append)
    service = configured(runtime, [strategy])
    assert strategy.evaluate(service.view).state == "unknown"
    service.refresh()
    assert assessments[-1].satisfied
    runtime[2].return_value = 140
    assert strategy.evaluate(service.view).state == "blocked"
    runtime[1]["observedAt"] = 140
    service.refresh()
    assert strategy.evaluate(service.view).satisfied
    runtime[1]["terminating"] = True
    service.refresh()
    assert assessments[-1].state == "blocked"


def test_temporary_permission_waits_for_active_then_expires_or_is_revoked(runtime):
    """
    Consent events update admission without implicitly approving or renewing a connection.
    """
    assessments = []
    strategy = ConnectionPermissionStrategy("temporary-sink", assessments.append, receipt=lambda: "uid-connection")
    service = configured(runtime, [strategy])
    service.refresh()
    assert assessments[-1].state == "blocked"
    event = observation(2)
    event.data["connection"]["expiresAt"] = datetime.fromtimestamp(120, UTC).isoformat()
    event.data["connection"]["status"] = {"phase": "Pending"}
    service.dispatch(event)
    assert assessments[-1].state == "blocked"
    event = observation(2, cursor="2-0")
    event.data["connection"].update(expiresAt=datetime.fromtimestamp(120, UTC).isoformat(), status={"phase": "Active"})
    service.dispatch(event)
    assert assessments[-1].satisfied
    runtime[2].return_value = 121
    runtime[1]["observedAt"] = 121
    assert not strategy.evaluate(service.view).satisfied
    service.dispatch(Event("", "heartbeat", {}))
    assert assessments[-1].state == "blocked"
    event = Event("3-0", event.event, event.data)
    event.data["connection"]["expiresAt"] = datetime.fromtimestamp(150, UTC).isoformat()
    service.dispatch(event)
    assert assessments[-1].satisfied
    event = Event("4-0", event.event, event.data)
    event.data["connection"]["revokeRequested"] = True
    service.dispatch(event)
    assert assessments[-1].state == "blocked"


@pytest.mark.parametrize("value", [None, {}, {"pods": True}, {"pods": -1}, {"pods": "3"}, {"pods": float("inf")}, {"pods": 10**1000}])
def test_resource_unknowns_never_become_free_capacity(value):
    """
    Missing, expired and invalid values hold changes until fresh evidence arrives.
    """
    strategy = ResourceBudgetStrategy("roll-overlap", lambda _: None, metric="pods", maximum=4, reserve=2)
    assert strategy.evaluate(measured(value)[1]).state == "unknown"
    assert strategy.evaluate(measured({"pods": 0}, available=False)[1]).state == "unknown"


def test_rollout_capacity_accounts_for_old_and_new_workers_and_clears_independently(runtime):
    """
    A feasible steady count can still exceed overlap limits; another guard stays authoritative.
    """
    constraints, proposals = {}, []
    active = ["interactive"]
    budget = ResourceBudgetStrategy("overlap", lambda result: constraints.update(overlap=result), metric="pods", maximum=4, reserve=2)
    freshness = FreshnessStrategy("freshness", lambda result: constraints.update(freshness=result))
    pressure = ThresholdStrategy(
        "backlog",
        low=2,
        high=8,
        idle="interactive",
        busy="batch",
        active=lambda: active[0],
        propose=lambda target, *_: proposals.append(target),
    )
    service = configured(runtime, [budget, freshness, pressure])
    service.refresh()
    assert constraints["overlap"].state == "unknown" and constraints["freshness"].satisfied
    event = observation()
    event.data["resources"] = {"pods": 3, "backlog": 12}
    service.dispatch(event)
    assert constraints["overlap"].state == "blocked" and constraints["freshness"].satisfied
    assert proposals == ["batch"] and active == ["interactive"]
    assert not all(result.satisfied for result in constraints.values())
    event = Event("2-0", event.event, event.data)
    event.data["resources"]["pods"] = 2
    service.dispatch(event)
    assert all(result.satisfied for result in constraints.values())
    active[0] = "batch"  # The application supervisor now admits and commits the profile.
    event = Event("3-0", event.event, event.data)
    event.data["resources"]["backlog"] = 0
    service.dispatch(event)
    assert proposals[-1] == "interactive"
    runtime[2].return_value = 140
    assert not all(guard.evaluate(service.view).satisfied for guard in (budget, freshness))


@pytest.mark.parametrize(
    "phase",
    [
        "Recommended",
        "CoolingDown",
        "Stabilizing",
        "TemporaryConnectionsActive",
        "NoAllowedLayout",
        "CapacityUnavailable",
        "ComputationLimited",
        "StaleSample",
        "FuturePhase",
    ],
)
def test_dependent_actions_wait_for_operator_resolution(phase):
    """
    Uncommitted, constrained and unfamiliar decisions cannot release dependent actions.
    """
    guard = DecisionGuardStrategy("operator-profile", lambda _: None)
    _, view = measured({})
    assert guard.evaluate(view).state == "unknown"
    view.observations["graph-1"]["status"]["throughput"] = {"phase": phase}
    assert guard.evaluate(view).state == "blocked"
    view.observations["graph-1"]["status"]["throughput"]["phase"] = "Applied"
    assert guard.evaluate(view).satisfied
    assert guard.evaluate(replace(view, available=False)).state == "unknown"


def test_decision_guard_can_require_only_a_committed_change():
    """
    A handoff may require Applied while an independent action can accept Satisfied.
    """
    guard = DecisionGuardStrategy("handoff", lambda _: None, allowed=("Applied",))
    _, view = measured({})
    view.observations["graph-1"]["status"]["throughput"] = {"phase": "Satisfied"}
    assert guard.evaluate(view).state == "blocked"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: FreshnessStrategy("", lambda _: None),
        lambda: FreshnessStrategy("fresh", None),
        lambda: PeerAvailabilityStrategy("peers", lambda _: None, usable=lambda _: True, minimum=True),
        lambda: PeerAvailabilityStrategy("peers", lambda _: None, usable=None),
        lambda: ConnectionPermissionStrategy("path", lambda _: None, receipt=None),
        lambda: ResourceBudgetStrategy("roll", lambda _: None, metric="pods", maximum=2, reserve=3),
        lambda: ResourceBudgetStrategy("roll", lambda _: None, metric="pods", maximum=float("nan")),
        lambda: ResourceBudgetStrategy("roll", lambda _: None, metric="pods", maximum=10**1000),
        lambda: ResourceBudgetStrategy("roll", lambda _: None, metric="pods", maximum=3, reserve=-1),
        lambda: ResourceBudgetStrategy("roll", lambda _: None, metric="pods..count", maximum=3),
        lambda: DecisionGuardStrategy("decision", lambda _: None, allowed="Applied"),
        lambda: DecisionGuardStrategy("decision", lambda _: None, allowed=()),
    ],
)
def test_invalid_constraint_configuration_fails_before_observation(factory):
    """
    Reject malformed or unbounded policies before any application work is considered.
    """
    with pytest.raises((TypeError, ValueError)):
        factory()
