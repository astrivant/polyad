"""
Exercise queue-envelope correctness, applicability checks and optional numerical isolation.
"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import subprocess
import sys
from dataclasses import replace

import pytest

from polyad_sdk.symbiosis.models import Environment
from polyad_sdk.symbiosis.reachability import Envelope, Interaction, Observation, QueueModel, ReachabilityStrategy, Relationship
from polyad_sdk.symbiosis.reachability.hj import AnalysisBudget, analyze


def model(**changes):
    """
    Use a saturated producer whose spare consumer can absorb a rerouted share.
    """
    return replace(QueueModel(("busy", "spare"), (10, 35), (20, 20), (20, 20), (25, 30), (0.25, 0.75)), **changes)


@pytest.mark.parametrize(
    ("relationship", "effects"),
    [
        ("neutralism", (0, 0)),
        ("mutualism", (5, 5)),
        ("commensalism", (5, 0)),
        ("parasitism", (5, -5)),
        ("competition", (-5, -5)),
        ("amensalism", (-5, 0)),
    ],
)
def test_interaction_effects_have_explicit_direction_and_capacity_cost(relationship, effects):
    """
    Calibrated effects must match the label and remain physically possible.
    """
    interaction = Interaction(Relationship(relationship), effects)
    assert model(interaction=interaction).effective_capacities == (10 + effects[0], 35 + effects[1])
    with pytest.raises(ValueError):
        Interaction(Relationship(relationship), (999, -999) if relationship != "parasitism" else (1, 1))
    with pytest.raises(ValueError, match="exceed"):
        model(interaction=Interaction(Relationship.PARASITISM, (1, -36)))


@pytest.mark.parametrize(
    "changes",
    [
        {"shares": (1, 1)},
        {"shares": (True, 0)},
        {"capacities": (float("nan"), 1)},
        {"arrival_bounds": (30, 25)},
        {"limits": (0, 20)},
        {"targets": (21, 20)},
        {"names": ("same", "same")},
        {"warmup_max": float("inf")},
        {"unit": ""},
    ],
)
def test_invalid_physical_models_fail_before_computation(changes):
    """
    Invalid rates, identities and nonconserving routes cannot produce an envelope.
    """
    with pytest.raises(ValueError):
        model(**changes)


def test_rerouting_and_readiness_change_the_reachable_contract():
    """
    Spare capacity helps only when routed work can reach a ready consumer.
    """
    prepared = Envelope(model(), "rev", 2, 1, 100)
    assert prepared.assess((0, 0))[0]
    assert not replace(prepared, model=model(shares=(1.0, 0.0))).assess((0, 0))[0]
    warming = replace(prepared, model=model(warmup_max=2))
    assert not warming.assess((0, 0, 2))[0]
    assert warming.assess((0, 0, 0))[0]


def test_peak_before_readiness_is_checked_even_when_terminal_queue_recovers():
    """
    A safe final backlog cannot hide an earlier queue overflow.
    """
    warming = model(capacities=(100, 100), warmup_max=2)
    peak, terminal = warming.bounds((0, 10, 1), 2)
    assert peak[1] == 32.5 and terminal[1] == 0
    assert not Envelope(warming, "rev", 2, 1, 100).assess((0, 10, 1))[0]


def test_bounds_dominate_piecewise_arrivals_and_faster_service():
    """
    Independently integrate changing arrivals and readiness below the analytic upper bound.
    """
    tested = model(warmup_max=1)
    start, horizon, step = (5.0, 4.0, 0.5), 2.0, 0.001
    peak, terminal = tested.bounds(start, horizon)
    queues, measured_peak = list(start[:2]), list(start[:2])
    for index in range(round(horizon / step)):
        moment = index * step
        arrival = 25 if index % 3 else 30
        for axis, capacity in enumerate(tested.effective_capacities):
            service = capacity + 1 if axis == 0 or moment >= start[-1] else 0
            queues[axis] = max(0, queues[axis] + (arrival * tested.shares[axis] - service) * step)
            measured_peak[axis] = max(measured_peak[axis], queues[axis])
    assert all(actual <= bound + 1e-6 for actual, bound in zip(queues, terminal, strict=True))
    assert all(actual <= bound + 1e-6 for actual, bound in zip(measured_peak, peak, strict=True))


def test_artifact_roundtrip_preserves_effects_and_rejects_tampering():
    """
    Portable JSON revalidates both the physical model and its fingerprint.
    """
    envelope = Envelope(model(interaction=Interaction(Relationship.PARASITISM, (5, -5))), "rev", 2, 1, 100)
    assert Envelope.loads(envelope.dumps()) == envelope
    data = json.loads(envelope.dumps())
    data["model"]["shares"] = [0.5, 0.5]
    with pytest.raises(ValueError, match="fingerprint"):
        Envelope.loads(json.dumps(data))
    for invalid in ("[]", '{"version": 2}', '"wrong"', " " * 65537):
        with pytest.raises(ValueError):
            Envelope.loads(invalid)


@pytest.mark.parametrize("case", ["expired", "future", "stale", "revision", "action", "axes", "uncertainty", "context"])
def test_guard_rejects_inapplicable_or_uncertain_evidence(case):
    """
    A historical positive analysis cannot authorize a different action or stale state.
    """
    envelope = Envelope(model(), "rev", 2, 1, 100)
    view = Environment(None, {}, {}, case != "context", None)
    observed = Observation((0, 0), (0, 0), 10, "rev", envelope.model.fingerprint)
    changes = {
        "future": {"observed_at": 11},
        "stale": {"observed_at": 8},
        "revision": {"revision": "new"},
        "action": {"fingerprint": model(shares=(1.0, 0.0)).fingerprint},
        "axes": {"state": (0,)},
        "uncertainty": {"uncertainty": (-1, 0)},
    }
    observed = replace(observed, **changes.get(case, {}))
    strategy = ReachabilityStrategy(
        "route", lambda _: None, artifact=lambda: envelope, observe=lambda _: observed, clock=lambda: 101 if case == "expired" else 10
    )
    assert strategy.evaluate(view).state == "unknown"


def test_guard_accounts_for_measurement_error_and_inflight_arrivals():
    """
    Error and age consume queue headroom even when a point measurement looks safe.
    """
    envelope = Envelope(model(), "rev", 2, 1, 100)
    observed = Observation((19, 0), (0, 0), 10, "rev", envelope.model.fingerprint)
    now = 10.0
    strategy = ReachabilityStrategy("route", lambda _: None, artifact=lambda: envelope, observe=lambda _: observed, clock=lambda: now)
    view = Environment(None, {}, {}, True, None)
    assert strategy.evaluate(view).satisfied
    now += 0.1
    assert strategy.evaluate(view).state == "blocked"
    now = 10
    observed = replace(observed, uncertainty=(2, 0))
    assert strategy.evaluate(view).state == "blocked"


def test_core_and_analysis_entrypoint_do_not_import_optional_numerics():
    """
    A normal service can import its guard and budgets without loading JAX or the operator.
    """
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from polyad_sdk.symbiosis.reachability import ReachabilityStrategy
from polyad_sdk.symbiosis.reachability.hj import AnalysisBudget
assert not any(name in sys.modules for name in ('jax', 'hj_reachability', 'flax', 'numpy', 'polyad'))
assert AnalysisBudget(points_per_axis=100, max_points=2_000_000).estimate(3)['valueBytes'] == 4_000_000
""",
        ],
        check=True,
    )


def test_grid_budget_rejects_exponential_growth_before_starting_workers():
    """
    Point and workspace limits are checked before any optional import or allocation.
    """
    before = multiprocessing.active_children()
    with pytest.raises(ValueError, match="budget"):
        analyze(model(warmup_max=2), budget=AnalysisBudget(points_per_axis=100))
    with pytest.raises(ValueError, match="budget"):
        AnalysisBudget(max_workspace_bytes=100).estimate(2)
    assert multiprocessing.active_children() == before


@pytest.mark.skipif(importlib.util.find_spec("hj_reachability") is None, reason="optional reachability extra is not installed")
def test_real_hj_backend_classifies_simple_interior_states_and_joins():
    """
    Numerical backward safety agrees with analytic bounds away from the boundary.
    """
    tested = QueueModel(("consumer",), (0,), (100,), (100,), (10, 10), (1.0,))
    before = multiprocessing.active_children()
    result = analyze(tested, horizon=1, budget=AnalysisBudget(points_per_axis=33), samples=((10,), (99,)))
    assert result["samples"][0]["numericalMargin"] > 0
    assert result["samples"][1]["numericalMargin"] < 0
    assert result["samples"][0]["analyticMargin"] == 80
    assert result["samples"][1]["analyticMargin"] == -9
    assert result["peakRssBytes"] is None or result["peakRssBytes"] > result["valueBytes"]
    assert result["platform"] == "cpu" and result["dtype"] == "float32"
    assert result["cpuSeconds"] is None or result["cpuSeconds"] > 0
    assert multiprocessing.active_children() == before


def test_analysis_deadline_terminates_and_joins_child():
    """
    Deadline expiry never leaves an import or computation process behind.
    """
    before = multiprocessing.active_children()
    with pytest.raises(TimeoutError):
        analyze(model(), budget=AnalysisBudget(timeout_seconds=0.001))
    assert multiprocessing.active_children() == before
