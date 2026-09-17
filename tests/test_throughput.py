"""
Exercise independent application targets, hard structural limits and bounded feedback decisions.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from attrs import evolve
from cattrs.errors import CattrsError
from kubernetes.client.exceptions import ApiException

from polyad.api.errors import Conflict, Forbidden
from polyad.api.throughput import report_throughput
from polyad.operator.policies.throughput import SAMPLE, STATE, reconcile_throughput
from polyad.operator.reconciliation.controller import Controller
from polyad_types import GraphAccess, ThroughputSample
from polyad_types.codec import converter, to_dict
from polyad_types.resources import GROUP
from polyad_types.topology import ThroughputPolicy
from tests.test_operator import FakeAPI, resource


class FeedbackAPI(FakeAPI):
    """
    Model merge patches and generation changes for atomic topology/state commits.
    """

    async def request(self, method, kind, namespace, name="", body=None, **kwargs):
        """
        Commit metadata and topology together after checking the resource version.
        """
        if method == "PATCH" and isinstance(body, dict) and not kwargs.get("status"):
            key = kind, namespace, name
            current = self.objects[key]
            if current["metadata"]["resourceVersion"] != body["metadata"]["resourceVersion"]:
                raise ApiException(status=409)
            current["metadata"].setdefault("annotations", {}).update(body["metadata"].get("annotations", {}))
            if "spec" in body:
                current["spec"].update(copy.deepcopy(body["spec"]))
                current["metadata"]["generation"] += 1
            current["metadata"]["resourceVersion"] = str(int(current["metadata"]["resourceVersion"]) + 1)
            self.calls.append((method, kind, name))
            return copy.deepcopy(current)
        return await super().request(method, kind, namespace, name, body, **kwargs)


def fixture(mode="Observe", maximum=None):
    """
    Begin with a four-node chain and approve only its ring alternative.
    """
    chain = [{"source": left, "target": right} for left, right in zip("abc", "bcd", strict=True)]
    ring = [*chain, {"source": "d", "target": "a"}]
    graph = resource(
        "Graph",
        "pipeline",
        {
            "mode": "persistent",
            "nodes": [{"name": name, "kind": "Daemon", "ref": name} for name in "abcd"],
            "connections": chain,
            "throughput": {
                "mode": mode,
                "unit": "records",
                "sustainedSeconds": 10,
                "sampleMaxAgeSeconds": 30,
                "minSamples": 2,
                "tiers": [{"offeredPerSecond": 100, "cheeger": {"minimum": 1}}],
                "layouts": [{"name": "ring", "connections": ring}],
            },
        },
    )
    rule = resource("GraphRule", "hard", {"relation": "connections", "cheeger": {"minimum": 0.5, "maximum": maximum}})
    return FeedbackAPI(graph, rule)


async def feed(api, second):
    """
    Publish one fresh measurement and run a leased feedback evaluation.
    """
    clock = datetime(2026, 9, 16, tzinfo=UTC) + timedelta(seconds=second)
    graph = await api.get("Graph", "test", "pipeline")
    sample = ThroughputSample("pipeline", graph["metadata"]["uid"], graph["metadata"]["generation"], clock.isoformat(), "records", 200, 50)
    graph["metadata"].setdefault("annotations", {})[SAMPLE] = json.dumps(to_dict(sample))
    api.objects[("Graph", "test", "pipeline")] = graph
    changed = await reconcile_throughput(Controller(api), graph, now=clock)
    return changed, await api.get("Graph", "test", "pipeline")


@pytest.mark.parametrize(("mode", "changed", "phase", "edges"), [("Observe", False, "Recommended", 3), ("Adapt", True, "Applied", 4)])
def test_modes_keep_hard_bounds_independent(mode, changed, phase, edges):
    """
    Observe recommends without mutations; Adapt commits a layout after sustained demand.
    """

    async def run():
        api = fixture(mode)
        hard = copy.deepcopy(api.objects[("GraphRule", "test", "hard")])
        assert (await feed(api, 0))[0] is False
        actual, graph = await feed(api, 10)
        assert actual is changed
        assert graph["status"]["throughput"]["phase"] == phase
        assert graph["status"]["throughput"]["target"]["minimum"] == 1
        assert len(graph["spec"]["connections"]) == edges
        assert api.objects[("GraphRule", "test", "hard")] == hard
        if changed:
            assert len(json.loads(graph["metadata"]["annotations"][STATE])["changes"]) == 1
            assert graph["metadata"]["generation"] == 2

    asyncio.run(run())


def test_conflicting_static_upper_bound_blocks_adaptation():
    """
    A target cannot override a hard maximum even when every offered layout is unsuitable.
    """

    async def run():
        api = fixture("Adapt", maximum=0.75)
        await feed(api, 0)
        changed, graph = await feed(api, 10)
        assert not changed
        assert graph["status"]["throughput"]["phase"] == "NoAllowedLayout"
        assert len(graph["spec"]["connections"]) == 3

    asyncio.run(run())


def test_replays_stale_samples_and_low_demand_do_not_restructure():
    """
    Reconciliation frequency cannot manufacture sustained application measurements.
    """

    async def run():
        api = fixture("Adapt")
        _, graph = await feed(api, 0)
        first = datetime(2026, 9, 16, tzinfo=UTC)
        for second in (10, 20, 31):
            assert not await reconcile_throughput(Controller(api), graph, now=first + timedelta(seconds=second))
            graph = await api.get("Graph", "test", "pipeline")
        assert graph["status"]["throughput"]["phase"] == "StaleSample"
        raw = json.loads(graph["metadata"]["annotations"][SAMPLE])
        raw.update(observedAt=(first + timedelta(seconds=40)).isoformat(), offeredPerSecond=0, completedPerSecond=0)
        graph["metadata"]["annotations"][SAMPLE] = json.dumps(raw)
        api.objects[("Graph", "test", "pipeline")] = graph
        assert not await reconcile_throughput(Controller(api), graph, now=first + timedelta(seconds=40))
        graph = await api.get("Graph", "test", "pipeline")
        assert graph["status"]["throughput"]["phase"] == "BelowDemandThreshold"

    asyncio.run(run())


def test_cooldown_survives_topology_edits():
    """
    Administrative revisions do not reset the rolling change budget.
    """

    async def run():
        api = fixture("Adapt")
        await feed(api, 0)
        assert (await feed(api, 10))[0]
        graph = api.objects[("Graph", "test", "pipeline")]
        graph["spec"]["connections"].pop()
        graph["metadata"]["generation"] += 1
        await feed(api, 20)
        changed, graph = await feed(api, 30)
        assert not changed
        assert graph["status"]["throughput"]["phase"] == "CoolingDown"

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet", "DaemonSet"])
def test_capacity_changes_restart_stabilization(kind):
    """
    Native capacity changes require new sustained measurements before topology adaptation.
    """

    async def run():
        api = fixture("Adapt")
        child = resource(kind, "worker", {"replicas": 1} if kind != "DaemonSet" else {})
        child["apiVersion"] = "apps/v1"
        child["metadata"]["ownerReferences"] = [
            {"apiVersion": f"{GROUP}/v1alpha1", "kind": "Graph", "name": "pipeline", "uid": "uid-pipeline"}
        ]
        child["status"] = {"desiredNumberScheduled": 1} if kind == "DaemonSet" else {}
        api.objects[(kind, "test", "worker")] = child
        await feed(api, 0)
        if kind == "DaemonSet":
            child["status"]["desiredNumberScheduled"] = 2
        else:
            child["spec"]["replicas"] = 2
            child["metadata"]["generation"] += 1
        changed, graph = await feed(api, 10)
        assert not changed
        assert graph["status"]["throughput"]["phase"] == "Stabilizing"
        assert (await feed(api, 20))[0]

    asyncio.run(run())


def test_adaptation_checks_live_activation_instances():
    """
    A logical layout cannot bypass a node limit by hiding multiple live activation copies.
    """

    async def run():
        api = fixture("Adapt")
        api.objects[("GraphRule", "test", "hard")]["spec"]["limits"] = {"nodes": 4}
        for name in ("a-pulse-one", "a-pulse-two"):
            child = resource("Deployment", name, {"replicas": 1})
            child["apiVersion"] = "apps/v1"
            child["metadata"].update(
                ownerReferences=[{"apiVersion": f"{GROUP}/v1alpha1", "kind": "Graph", "name": "pipeline", "uid": "uid-pipeline"}],
                labels={f"{GROUP}/node": "a", f"{GROUP}/runtime-node": name},
            )
            api.objects[("Deployment", "test", name)] = child
        await feed(api, 0)
        changed, graph = await feed(api, 10)
        assert not changed
        assert graph["status"]["throughput"]["phase"] == "NoAllowedLayout"

    asyncio.run(run())


def test_measurement_intake_fences_grants_generation_and_order():
    """
    A reporter cannot address an unrelated tree or replay measurements into a new revision.
    """

    async def run():
        api = fixture()
        now = datetime.now(UTC)
        sample = ThroughputSample("pipeline", "uid-pipeline", 1, now.isoformat(), "records", 200, 50)
        with pytest.raises(Forbidden):
            await report_throughput(api, "test", sample, ())
        grants = (GraphAccess("pipeline", "test"),)
        await report_throughput(api, "test", sample, grants)
        await report_throughput(api, "test", sample, grants)
        with pytest.raises(Conflict):
            await report_throughput(api, "test", evolve(sample, generation=2), grants)
        with pytest.raises(Conflict):
            await report_throughput(api, "test", evolve(sample, completedPerSecond=60), grants)
        with pytest.raises(ValueError):
            await report_throughput(api, "test", evolve(sample, observedAt=(now - timedelta(minutes=5)).isoformat()), grants)

    asyncio.run(run())


def test_reserved_graph_rejects_application_measurements(monkeypatch):
    """
    The named reserved graph stays private even before internal labels are reconciled.
    """
    monkeypatch.setenv("POLYAD_SELF_GRAPH", "pipeline")
    monkeypatch.setenv("POLYAD_NAMESPACE", "test")
    sample = ThroughputSample("pipeline", "uid-pipeline", 1, datetime.now(UTC).isoformat(), "records", 200, 50)
    with pytest.raises(Forbidden):
        asyncio.run(report_throughput(fixture(), "test", sample, None))


@pytest.mark.parametrize("change", [{"mode": "Automatic"}, {"minSamples": 1}, {"shortfallRatio": 0}, {"maxChangesPerHour": 0}])
def test_invalid_feedback_budgets_are_rejected(change):
    """
    Validate feedback modes and bounds before operators consume application measurements.
    """
    policy = fixture().objects[("Graph", "test", "pipeline")]["spec"]["throughput"]
    with pytest.raises((ValueError, TypeError, CattrsError)):
        converter.structure({**policy, **change}, ThroughputPolicy)


def test_incomplete_computation_cannot_change_a_layout(monkeypatch):
    """
    The feedback loop inherits operator budgets and publishes an explicit blocked result.
    """
    monkeypatch.setenv("POLYAD_CHEEGER_MAX_CUTS", "1")

    async def run():
        api = fixture("Adapt")
        before = copy.deepcopy(api.objects[("Graph", "test", "pipeline")]["spec"])
        await feed(api, 0)
        changed, graph = await feed(api, 10)
        assert not changed and graph["spec"] == before
        status = graph["status"]["throughput"]
        assert status["phase"] == "ComputationLimited" and status["currentCheeger"] is None
        assert status["computation"]["reason"] == "CutBudget" and not status["computation"]["exact"]

    asyncio.run(run())


@pytest.mark.parametrize(("offered", "layout", "edges"), [(200, "ring", 4), (1500, "mesh", 6)])
@pytest.mark.parametrize("mode", ["Observe", "Adapt"])
def test_tuning_reference_selects_demand_tiers_without_relaxing_hard_bounds(offered, layout, edges, mode):
    """
    Exercise the shipped reference's timing, separate demand targets and ordered alternatives.
    """
    documents = list(yaml.safe_load_all((Path(__file__).parents[1] / "examples/cheeger-tuning.yaml").read_text()))
    objects = [resource(doc["kind"], doc["metadata"]["name"], doc["spec"]) for doc in documents]
    graph = next(obj for obj in objects if obj["kind"] == "Graph")
    graph["metadata"]["name"] = "pipeline"
    graph["spec"]["throughput"]["mode"] = mode
    api = FeedbackAPI(*objects)

    async def run():
        for second in (0, 30, 60, 90, 120):
            clock = datetime(2026, 9, 16, tzinfo=UTC) + timedelta(seconds=second)
            obj = await api.get("Graph", "test", "pipeline")
            sample = ThroughputSample(
                "pipeline", obj["metadata"]["uid"], obj["metadata"]["generation"], clock.isoformat(), "records", offered, 50
            )
            obj["metadata"].setdefault("annotations", {})[SAMPLE] = json.dumps(to_dict(sample))
            api.objects[("Graph", "test", "pipeline")] = obj
            changed = await reconcile_throughput(Controller(api), obj, now=clock)
            if second < 120:
                assert not changed
        actual = await api.get("Graph", "test", "pipeline")
        assert actual["status"]["throughput"]["recommendedLayout"] == layout
        assert actual["status"]["throughput"]["phase"] == ("Applied" if mode == "Adapt" else "Recommended")
        assert len(actual["spec"]["connections"]) == (edges if mode == "Adapt" else 3)

    asyncio.run(run())
