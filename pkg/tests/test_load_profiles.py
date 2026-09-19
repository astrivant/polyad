"""
Exercise approved demand profiles, fixed ceilings and capacity preparation.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime

import pytest
from cattrs.errors import CattrsError

from polyad.api.workloads.throughput import report_throughput
from polyad_types import DemandSample, GraphAccess, ThroughputSample
from polyad_types.graphs.topology import topology
from tests.test_throughput import feed, fixture


def profiles(mode="Adapt", *, maximum=None):
    """
    Approve two forecast budgets with the same existing structural limits.
    """
    api = fixture(mode, maximum)
    spec = api.objects[("Graph", "test", "pipeline")]["spec"]
    spec["capacity"] = {"backend": "Placeholders", "lookaheadStages": 1, "maxPods": 4, "timeoutSeconds": 900, "retryToken": "manual"}
    spec["throughput"].update(
        trigger="Demand",
        capacityCeiling={"lookaheadStages": 3, "maxPods": 16},
        tiers=[
            {"threshold": 1, "cheeger": {"minimum": 0.5}, "capacity": {"lookaheadStages": 1, "maxPods": 4}},
            {"threshold": 100, "cheeger": {"minimum": 1}, "capacity": {"lookaheadStages": 3, "maxPods": 16}},
        ],
    )
    return api


@pytest.mark.parametrize(("value", "offered", "changed"), [(200, 0, True), (50, 2000, False), (0, 2000, False)])
def test_administrator_selects_demand_instead_of_using_arrival_rate(monkeypatch, value, offered, changed):
    """
    A queue-depth policy selects tiers from jobs, even when its work arrival rate differs.
    """
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", "true")

    async def run():
        api = profiles()
        api.objects[("Graph", "test", "pipeline")]["spec"]["throughput"]["demand"] = {"name": "queueDepth", "unit": "jobs"}
        demand = DemandSample("queueDepth", "jobs", value)
        await feed(api, 0, offered=offered, completed=offered, demand=demand)
        actual, obj = await feed(api, 10, offered=offered, completed=offered, demand=demand)
        assert actual is changed
        assert obj["status"]["throughput"]["demandValue"] == value
        assert obj["status"]["throughput"]["demandSignal"] == "queueDepth"
        assert obj["status"]["throughput"]["demandUnit"] == "jobs"

    asyncio.run(run())


@pytest.mark.parametrize("demand", [None, DemandSample("other", "jobs", 200), DemandSample("queueDepth", "bytes", 200)])
def test_missing_or_mismatched_signal_does_not_fall_back_to_arrival_rate(monkeypatch, demand):
    """
    Refresh validation rejects reports that do not match the administrator's chosen signal.
    """
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", "true")

    async def run():
        api = profiles()
        api.objects[("Graph", "test", "pipeline")]["spec"]["throughput"]["demand"] = {"name": "queueDepth", "unit": "jobs"}
        await feed(api, 0, demand=DemandSample("queueDepth", "jobs", 200))
        changed, obj = await feed(api, 10, demand=demand)
        assert not changed and obj["status"]["throughput"]["phase"] == "WaitingForDemandSignal"
        assert not (await feed(api, 20, demand=DemandSample("queueDepth", "jobs", 200)))[0]
        assert (await feed(api, 30, demand=DemandSample("queueDepth", "jobs", 200)))[0]

    asyncio.run(run())


def test_demand_intake_requires_the_selected_signal_and_current_graph():
    """
    The existing authorized intake accepts exact signals and rejects missing or undeclared ones.
    """

    async def run():
        api = profiles()
        policy = api.objects[("Graph", "test", "pipeline")]["spec"]["throughput"]
        policy["demand"] = {"name": "queueDepth", "unit": "jobs"}
        report = ThroughputSample(
            "pipeline",
            "uid-pipeline",
            1,
            datetime.now(UTC).isoformat(),
            "records",
            0,
            0,
            demand=DemandSample("queueDepth", "jobs", 200),
        )
        grants = (GraphAccess("pipeline", "test"),)
        await report_throughput(api, "test", report, grants)
        policy.pop("demand")
        with pytest.raises(ValueError, match="does not configure"):
            await report_throughput(api, "test", report, grants)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["Observe", "Adapt"])
def test_demand_prepares_layout_and_capacity_before_a_shortfall(monkeypatch, mode):
    """
    Healthy throughput can prepare an approved profile atomically without relaxing safety policy.
    """
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", "true")

    async def run():
        api = profiles(mode)
        before = copy.deepcopy(api.objects[("Graph", "test", "pipeline")]["spec"])
        rules = copy.deepcopy(api.objects[("GraphRule", "test", "hard")])
        assert not (await feed(api, 0, completed=200))[0]
        changed, obj = await feed(api, 10, completed=200)
        status = obj["status"]["throughput"]
        assert status["targetCapacity"] == status["proposedCapacity"] == {"lookaheadStages": 3, "maxPods": 16}
        assert status["currentCapacity"] == {"lookaheadStages": 1, "maxPods": 4}
        assert changed == (mode == "Adapt")
        expected = copy.deepcopy(before)
        if changed:
            expected["connections"] = [{**edge, "ports": []} for edge in before["throughput"]["layouts"][0]["connections"]]
            expected["capacity"].update(lookaheadStages=3, maxPods=16)
        assert obj["spec"] == expected
        assert api.objects[("GraphRule", "test", "hard")] == rules

    asyncio.run(run())


@pytest.mark.parametrize(
    ("enabled", "ceiling", "hard", "phase"),
    [
        (False, 32, None, "CapacityUnavailable"),
        (True, 8, None, "CapacityUnavailable"),
        (True, 32, 0.75, "NoAllowedLayout"),
    ],
)
def test_unavailable_profile_never_partially_applies(monkeypatch, enabled, ceiling, hard, phase):
    """
    A denied forecast or incompatible hard bound also blocks the associated layout change.
    """
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", str(enabled).lower())
    monkeypatch.setenv("POLYAD_CAPACITY_MAX_PODS", str(ceiling))

    async def run():
        api = profiles(maximum=hard)
        before = copy.deepcopy(api.objects[("Graph", "test", "pipeline")]["spec"])
        await feed(api, 0)
        changed, obj = await feed(api, 10)
        assert not changed and obj["spec"] == before
        assert obj["status"]["throughput"]["phase"] == phase

    asyncio.run(run())


def test_lower_demand_profile_retains_cooldown_and_can_reduce_forecast(monkeypatch):
    """
    A lower positive tier contracts only forecast settings after the shared cooldown.
    """
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", "true")

    async def run():
        api = profiles()
        await feed(api, 0)
        assert (await feed(api, 10))[0]
        for second in range(20, 321, 10):
            changed, obj = await feed(api, second, offered=50, completed=50)
            if second < 310:
                assert not changed
            if second == 30:
                assert obj["status"]["throughput"]["phase"] == "CoolingDown"
            if changed:
                assert second == 310
        assert obj["spec"]["capacity"]["maxPods"] == 4
        assert obj["spec"]["capacity"]["lookaheadStages"] == 1
        assert len(obj["spec"]["connections"]) == 4

    asyncio.run(run())


def test_default_trigger_retains_shortfall_semantics(monkeypatch):
    """
    Existing policies do not adapt while their measured application keeps up.
    """
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", "true")

    async def run():
        api = profiles()
        del api.objects[("Graph", "test", "pipeline")]["spec"]["throughput"]["trigger"]
        await feed(api, 0, completed=200)
        changed, obj = await feed(api, 10, completed=200)
        assert not changed and obj["status"]["throughput"]["phase"] == "Satisfied"
        assert obj["spec"]["capacity"]["maxPods"] == 4

    asyncio.run(run())


def test_capacity_only_profile_does_not_require_an_explicit_connections_field(monkeypatch):
    """
    A forecast adjustment can stand alone and preserves unrelated workload settings.
    """
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", "true")

    async def run():
        api = profiles()
        spec = api.objects[("Graph", "test", "pipeline")]["spec"]
        spec["nodes"] = spec["nodes"][:1]
        del spec["connections"]
        spec["throughput"]["layouts"] = []
        for tier in spec["throughput"]["tiers"]:
            tier["cheeger"] = {"minimum": 0}
        api.objects[("GraphRule", "test", "hard")]["spec"]["cheeger"] = {"minimum": 0}
        await feed(api, 0, completed=200)
        changed, obj = await feed(api, 10, completed=200)
        assert changed and "connections" not in obj["spec"]
        assert obj["spec"]["capacity"] == {**spec["capacity"], "lookaheadStages": 3, "maxPods": 16}

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["Graph", "PolyGraph"])
@pytest.mark.parametrize("invalid", ["ceiling", "plan", "active", "tier", "type", "trigger"])
def test_invalid_capacity_profiles_fail_before_reconciliation(kind, invalid):
    """
    Both graph kinds enforce explicit ceilings and bounded typed profiles.
    """
    spec = profiles().objects[("Graph", "test", "pipeline")]["spec"]
    if kind == "PolyGraph":
        for node in spec["nodes"]:
            node["kind"] = "Graph"
    topology(spec, kind)
    if invalid == "ceiling":
        del spec["throughput"]["capacityCeiling"]
    elif invalid == "plan":
        del spec["capacity"]
    elif invalid == "active":
        spec["capacity"]["maxPods"] = 17
    elif invalid == "tier":
        spec["throughput"]["tiers"][1]["capacity"]["lookaheadStages"] = 4
    elif invalid == "type":
        spec["throughput"]["tiers"][1]["capacity"]["maxPods"] = True
    else:
        spec["throughput"]["trigger"] = "Always"
    with pytest.raises((ValueError, TypeError, CattrsError)):
        topology(spec, kind)
