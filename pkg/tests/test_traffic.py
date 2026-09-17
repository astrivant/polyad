"""
Exercise percentage routing to graph replicas independently of their connection topology.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from deepdiff import DeepDiff

from polyad.compiler.passes.network import scope_label
from polyad.compiler.passes.traffic import capacity_weights, step_weights
from polyad.operator.policies.rule_state import check_live_rules
from polyad.operator.policies.rules import RuleViolation
from polyad.operator.policies.throughput import SAMPLE, reconcile_throughput
from polyad.operator.policies.traffic import ensure_routes
from polyad.operator.reconciliation.controller import Controller, Pending, child_name
from polyad_types import ThroughputSample, TrafficDestination, TrafficRoute, TrafficSample, TrafficWeights
from polyad_types.codec import to_dict
from polyad_types.resources import encode_body, to_document
from polyad_types.topology import topology
from tests.test_operator import FakeAPI, resource, template
from tests.test_throughput import FeedbackAPI


def child(parent, node, kind, spec):
    """
    Build a persisted execution with the compiler's actual ownership and naming scheme.
    """
    document = to_document(Controller(FeedbackAPI()).child(parent, node, kind, spec))
    result = resource(kind, document["metadata"]["name"], document["spec"])
    result["metadata"].update(document["metadata"])
    return result


def fixture(mode="Observe", traffic_mode="Tiers"):
    """
    Route a producer to two copies of a Graph, each containing its own entrypoint.
    """
    destinations = [{"target": f"copies/replica-{index}", "weight": 50, "minWeight": 10, "maxWeight": 90} for index in range(2)]
    tier = {"threshold": 100, "cheeger": {"minimum": 1}}
    if traffic_mode == "Tiers":
        tier["trafficWeights"] = [{"route": "work", "weights": {"copies/replica-0": 80, "copies/replica-1": 20}}]
    root = resource(
        "Graph",
        "pipeline",
        {
            "mode": "persistent",
            "nodes": [
                {"name": "producer", "kind": "Daemon", "ref": "producer"},
                {"name": "copies", "kind": "ReplicaGroup", "ref": "workers"},
            ],
            "connections": [{"source": "producer", "target": "copies", "ports": [{"port": 8080}]}],
            "network": {"mesh": True},
            "traffic": [{"name": "work", "source": "producer", "service": "pipeline-entry", "port": 8080, "destinations": destinations}],
            "throughput": {
                "mode": mode,
                "trafficMode": traffic_mode,
                "unit": "records",
                "tiers": [tier],
                "minSamples": 2,
                "sustainedSeconds": 10,
                "sampleMaxAgeSeconds": 30,
                "cooldownSeconds": 60,
                "maxWeightStep": 10,
            },
        },
    )
    definition = resource(
        "Graph", "worker", {"templateOnly": True, "mode": "persistent", "nodes": [{"name": "entry", "kind": "Daemon", "ref": "entry"}]}
    )
    group_def = resource(
        "ReplicaGroup", "workers", {"templateOnly": True, "template": {"kind": "Graph", "ref": "worker"}, "replicas": 2, "maxReplicas": 4}
    )
    group = child(root, "copies", "ReplicaGroup", {**group_def["spec"], "templateOnly": False})
    copies = [child(group, f"replica-{index}", "Graph", {**definition["spec"], "templateOnly": False}) for index in range(2)]
    service = resource("Service", "pipeline-entry", {"selector": {"app": "pipeline-entry"}, "ports": [{"name": "http", "port": 8080}]})
    return FeedbackAPI(root, definition, group_def, group, *copies, service, resource("Daemon", "entry"), resource("Daemon", "producer"))


async def report(api, second, *, measurements=True, stale=False, completed=50):
    """
    Report aggregate work and optional revision-fenced measurements of both graph copies.
    """
    root = await api.get("Graph", "test", "pipeline")
    group = await api.get("ReplicaGroup", "test", child_name(root, "copies"))
    traffic = []
    if measurements:
        for index, capacity in enumerate((20, 80)):
            kind = {"Daemon": "Deployment", "Workload": "Job"}.get(group["spec"]["template"]["kind"], group["spec"]["template"]["kind"])
            replica = await api.get(kind, "test", child_name(group, f"replica-{index}"))
            traffic.append(
                TrafficSample(
                    "work",
                    f"copies/replica-{index}",
                    "replaced" if stale else replica["metadata"]["uid"],
                    replica["metadata"]["generation"],
                    capacity / 2,
                    capacity / 2,
                )
            )
    now = datetime(2026, 9, 17, tzinfo=UTC) + timedelta(seconds=second)
    sample = ThroughputSample(
        "pipeline",
        root["metadata"]["uid"],
        root["metadata"]["generation"],
        now.isoformat(),
        "records",
        200,
        completed,
        traffic=tuple(traffic),
    )
    root["metadata"].setdefault("annotations", {})[SAMPLE] = json.dumps(to_dict(sample))
    api.objects[("Graph", "test", "pipeline")] = root
    changed = await reconcile_throughput(Controller(api), root, now=now)
    return changed, await api.get("Graph", "test", "pipeline")


@pytest.mark.parametrize(("mode", "changed", "expected"), [("Observe", False, 50), ("Adapt", True, 60)])
def test_tier_weights_change_traffic_to_graph_copies_without_changing_edges(mode, changed, expected):
    """
    Calibrated percentages move by ten points only after sustained demand and rule checks.
    """

    async def run():
        api = fixture(mode)
        before = copy.deepcopy(api.objects[("Graph", "test", "pipeline")]["spec"])
        assert not (await report(api, 0))[0]
        actual, root = await report(api, 10)
        assert actual is changed
        assert root["spec"]["traffic"][0]["destinations"][0]["weight"] == expected
        assert not DeepDiff(before["connections"], root["spec"]["connections"])
        assert root["status"]["throughput"]["currentCheeger"] == root["status"]["throughput"]["proposedCheeger"] == 1
        assert root["status"]["throughput"]["targetTraffic"][0]["weights"]["copies/replica-0"] == 80
        if changed:
            await report(api, 11)
            assert not (await report(api, 21))[0]
            assert (await api.get("Graph", "test", "pipeline"))["status"]["throughput"]["phase"] == "CoolingDown"

    asyncio.run(run())


@pytest.mark.parametrize("completed", [50, 200])
def test_headroom_rebalances_even_before_aggregate_throughput_falls(completed):
    """
    Per-copy measured capacity produces a different split from the calibrated tier mode.
    """

    async def run():
        api = fixture("Adapt", "Headroom")
        assert not (await report(api, 0, completed=completed))[0]
        changed, root = await report(api, 10, completed=completed)
        assert changed
        assert [item["weight"] for item in root["spec"]["traffic"][0]["destinations"]] == [40, 60]
        assert root["status"]["throughput"]["targetTraffic"][0]["weights"] == {"copies/replica-0": 20, "copies/replica-1": 80}

    asyncio.run(run())


@pytest.mark.parametrize(("measurements", "stale"), [(False, False), (True, True)])
def test_headroom_requires_complete_current_replica_reports(measurements, stale):
    """
    Missing measurements and replaced replica UIDs cannot cause automatic changes.
    """

    async def run():
        api = fixture("Adapt", "Headroom")
        await report(api, 0, measurements=measurements, stale=stale)
        changed, root = await report(api, 10, measurements=measurements, stale=stale)
        assert not changed
        assert root["status"]["throughput"]["phase"] == "WaitingForTrafficSample"
        assert not (await report(api, 11))[0]
        assert (await report(api, 21))[0]

    asyncio.run(run())


def test_hard_rules_block_weight_only_adaptation():
    """
    Traffic adjustment cannot bypass a conflicting structural rule even with unchanged edges.
    """

    async def run():
        api = fixture("Adapt")
        api.objects[("GraphRule", "test", "hard")] = resource("GraphRule", "hard", {"relation": "connections", "limits": {"edges": 0}})
        await report(api, 0)
        changed, root = await report(api, 10)
        assert not changed
        assert root["status"]["throughput"]["phase"] == "NoAllowedLayout"

    asyncio.run(run())


def test_native_routes_select_each_graph_replica_and_clean_up(monkeypatch):
    """
    Persist source-scoped subsets, refresh before use, and delete routes before subsets.
    """
    monkeypatch.setenv("POLYAD_MESH_ENABLED", "true")

    async def run():
        api = fixture()
        controller = Controller(api)
        root = await api.get("Graph", "test", "pipeline")
        group_name = child_name(root, "copies")
        with pytest.raises(Pending, match="persisted"):
            await ensure_routes(controller, root)
        await ensure_routes(controller, root)
        destination = api.children("DestinationRule")[0]
        virtual = api.children("VirtualService")[0]
        assert destination["spec"]["subsets"][0]["labels"] == {
            scope_label("test", "Graph", "pipeline", "copies"): "true",
            scope_label("test", "ReplicaGroup", group_name, "replica-0"): "true",
        }
        assert destination["spec"]["subsets"][0]["labels"] != destination["spec"]["subsets"][1]["labels"]
        route = virtual["spec"]["http"][0]
        assert route["match"][0]["sourceLabels"] == {scope_label("test", "Graph", "pipeline", "producer"): "true"}
        assert [item["weight"] for item in route["route"]] == [50, 50]
        root["spec"].pop("traffic")
        root["spec"].pop("throughput")
        api.objects[("Graph", "test", "pipeline")] = root
        with pytest.raises(Pending, match="obsolete"):
            await ensure_routes(controller, root)
        assert api.calls[-1][0:2] == ("DELETE", "VirtualService")
        assert not destination["metadata"].get("deletionTimestamp")

    asyncio.run(run())


def test_route_ownership_conflict_and_disabled_mesh(monkeypatch):
    """
    Optional routing never adopts another controller's resource or writes with mesh disabled.
    """

    async def run():
        api = fixture()
        root = await api.get("Graph", "test", "pipeline")
        monkeypatch.setenv("POLYAD_MESH_ENABLED", "false")
        with pytest.raises(ValueError, match="mesh integration"):
            await ensure_routes(Controller(api), root)
        assert not api.calls
        monkeypatch.setenv("POLYAD_MESH_ENABLED", "true")
        desired = child(root, "traffic-work", "DestinationRule", {})
        desired["metadata"]["ownerReferences"] = []
        api.objects[("DestinationRule", "test", desired["metadata"]["name"])] = desired
        with pytest.raises(ValueError, match="owned by another"):
            await ensure_routes(Controller(api), root)

    asyncio.run(run())


def test_scale_in_requires_draining_the_graph_copys_traffic_weight(monkeypatch):
    """
    A child group's scaling decision must respect the parent's positive routing assignments.
    """
    monkeypatch.setenv("POLYAD_MESH_ENABLED", "true")

    async def run():
        api = fixture()
        root = await api.get("Graph", "test", "pipeline")
        with pytest.raises(Pending, match="persisted"):
            await ensure_routes(Controller(api), root)
        group = await api.get("ReplicaGroup", "test", child_name(root, "copies"))
        group["spec"]["replicas"] = 1
        api.objects[("ReplicaGroup", "test", group["metadata"]["name"])] = group
        from polyad_types.replication import replica_topology

        effective = {**group, "spec": replica_topology(group["spec"])}
        with pytest.raises(RuleViolation, match="drain its weight"):
            await check_live_rules(api, effective)
        root["spec"].pop("throughput")
        for destination, weight in zip(root["spec"]["traffic"][0]["destinations"], (100, 0), strict=True):
            destination.update(weight=weight, minWeight=0, maxWeight=100)
        api.objects[("Graph", "test", "pipeline")] = root
        with pytest.raises(Pending, match="persist zero traffic weight"):
            await check_live_rules(api, effective)
        with pytest.raises(Pending, match="persisted"):
            await ensure_routes(Controller(api), root)
        await check_live_rules(api, effective)

    asyncio.run(run())


def test_percentages_and_steps_respect_limits():
    """
    Multiple destinations preserve exactly 100 percent through bounded transitions.
    """
    graph = topology(fixture().objects[("Graph", "test", "pipeline")]["spec"])
    for first in range(10, 91):
        targets = (TrafficWeights("work", {"copies/replica-0": first, "copies/replica-1": 100 - first}),)
        changed = step_weights(graph, targets, 7)[0]
        assert sum(item.weight for item in changed.destinations) == 100
        assert all(abs(item.weight - 50) <= 7 and item.minWeight <= item.weight <= item.maxWeight for item in changed.destinations)
    route = TrafficRoute(
        "test",
        "caller",
        "service",
        80,
        (TrafficDestination("a", 50, 20, 60), TrafficDestination("b", 30, 10, 70), TrafficDestination("c", 20, 10, 40)),
    )
    assert capacity_weights(route, {"a": 100, "b": 0, "c": 0}) is None
    weights = capacity_weights(route, {"a": 100, "b": 1, "c": 1}).weights
    assert weights == {"a": 60, "b": 20, "c": 20}


@pytest.mark.parametrize("mutation", ["missing-edge", "wrong-total", "out-of-bounds", "absent-target", "no-mesh", "mixed-modes"])
def test_invalid_routing_contracts_are_rejected(mutation):
    """
    Typed configurations reject disconnected, ambiguous and contradictory routing contracts.
    """
    spec = fixture().objects[("Graph", "test", "pipeline")]["spec"]
    if mutation == "missing-edge":
        spec["connections"] = []
    elif mutation == "wrong-total":
        spec["traffic"][0]["destinations"][0]["weight"] = 40
    elif mutation == "out-of-bounds":
        spec["traffic"][0]["destinations"][0]["maxWeight"] = 60
    elif mutation == "absent-target":
        spec["traffic"][0]["destinations"][0]["target"] = "absent/replica-0"
    elif mutation == "no-mesh":
        spec["network"]["mesh"] = False
    else:
        spec["throughput"]["trafficMode"] = "Headroom"
    with pytest.raises(ValueError):
        topology(spec)


@pytest.mark.parametrize("kind", ["Workload", "Daemon", "Graph", "PolyGraph", "ReplicaGroup"])
def test_every_replica_kind_can_receive_and_rebalance_traffic(kind, monkeypatch):
    """
    Workload, daemon and composed graph copies share the same bounded percentage mechanism.
    """
    monkeypatch.setenv("POLYAD_MESH_ENABLED", "true")

    async def run():
        api = fixture("Adapt", "Headroom")
        root = await api.get("Graph", "test", "pipeline")
        group = api.objects[("ReplicaGroup", "test", child_name(root, "copies"))]
        ref = {"Workload": "task", "Daemon": "entry", "Graph": "worker", "PolyGraph": "composition", "ReplicaGroup": "nested"}[kind]
        group["spec"]["template"] = {"kind": kind, "ref": ref}
        api.objects[("ReplicaGroup", "test", "workers")]["spec"]["template"] = copy.deepcopy(group["spec"]["template"])
        api.objects[("Workload", "test", "task")] = resource("Workload", "task", {"template": template()})
        api.objects[("PolyGraph", "test", "composition")] = resource(
            "PolyGraph",
            "composition",
            {"templateOnly": True, "mode": "persistent", "nodes": [{"name": "inner", "kind": "Graph", "ref": "worker"}]},
        )
        api.objects[("ReplicaGroup", "test", "nested")] = resource(
            "ReplicaGroup", "nested", {"templateOnly": True, "replicas": 1, "template": {"kind": "Graph", "ref": "worker"}}
        )
        for index in range(2):
            api.objects.pop(("Graph", "test", child_name(group, f"replica-{index}")))
            runtime = {"Workload": "Job", "Daemon": "Deployment"}.get(kind, kind)
            spec = {**api.objects[(kind, "test", ref)]["spec"], "templateOnly": False}
            if kind == "Workload":
                spec = {"template": template()}
            elif kind == "Daemon":
                spec = {
                    "template": template(True),
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "entry"}},
                    "strategy": {"type": "RollingUpdate"},
                }
            execution = child(group, f"replica-{index}", runtime, spec)
            api.objects[(runtime, "test", execution["metadata"]["name"])] = execution
        with pytest.raises(Pending, match="persisted"):
            await ensure_routes(Controller(api), root)
        assert not (await report(api, 0))[0]
        assert (await report(api, 10))[0]
        root = await api.get("Graph", "test", "pipeline")
        with pytest.raises(Pending, match="persisted"):
            await ensure_routes(Controller(api), root)
        assert [item["weight"] for item in api.children("VirtualService")[0]["spec"]["http"][0]["route"]] == [40, 60]

    asyncio.run(run())


def test_runnable_example_admits_targets_before_the_producer_and_does_not_roll_pods(monkeypatch):
    """
    Exercise full nested reconciliation, mesh admission and a percentage-only edit from the example.
    """
    monkeypatch.setenv("POLYAD_MESH_ENABLED", "true")

    class MeshAPI(FeedbackAPI):
        async def request(self, method, kind, namespace, name="", body=None, **kwargs):
            if method == "PATCH" and isinstance(body, dict) and "finalizers" in body.get("metadata", {}):
                return await FakeAPI.request(self, method, kind, namespace, name, body, **kwargs)
            if method == "POST" and kind == "Pod" and kwargs.get("query") == [("dryRun", "All")]:
                admitted = copy.deepcopy(body)
                admitted["spec"]["initContainers"] = [{"name": "istio-proxy", "restartPolicy": "Always"}]
                return admitted
            if method == "POST":
                self.creations.append((kind, encode_body(body)["metadata"]["name"]))
            return await super().request(method, kind, namespace, name, body, **kwargs)

    async def run():
        objects = [
            resource(doc["kind"], doc["metadata"]["name"], doc["spec"])
            for doc in yaml.safe_load_all(Path("examples/traffic-balancing.yaml").read_text())
        ]
        api = MeshAPI(*objects)
        api.creations = []
        controller = Controller(api)

        async def reconcile_all():
            for _ in range(15):
                for key, obj in list(api.objects.items()):
                    if obj["kind"] in {"Graph", "PolyGraph", "ReplicaGroup"}:
                        try:
                            await controller.reconcile(key)
                        except Pending:
                            pass
                for deployment in api.children("Deployment"):
                    deployment["status"] = {
                        "observedGeneration": deployment["metadata"]["generation"],
                        "readyReplicas": 1,
                        "updatedReplicas": 1,
                        "replicas": 1,
                        "availableReplicas": 1,
                    }

        await reconcile_all()
        root = await api.get("Graph", "test", "balanced-pipeline")
        assert root["status"]["ready"], {
            key: obj.get("status", {}) for key, obj in api.objects.items() if obj["kind"] in {"Graph", "ReplicaGroup"}
        }
        assert len(api.children("Deployment")) == 3
        assert root["status"]["metrics"]["resources"]["byKind"]["VirtualService"] == 1
        assert next(index for index, (kind, _) in enumerate(api.creations) if kind == "VirtualService") < api.creations.index(
            ("Deployment", child_name(root, "producer"))
        )
        before = {item["metadata"]["name"]: item["metadata"]["uid"] for item in api.children("Deployment")}
        root["spec"]["traffic"][0]["destinations"][0]["weight"] = 70
        root["spec"]["traffic"][0]["destinations"][1]["weight"] = 30
        root["metadata"]["generation"] += 1
        api.objects[("Graph", "test", "balanced-pipeline")] = root
        await reconcile_all()
        assert {item["metadata"]["name"]: item["metadata"]["uid"] for item in api.children("Deployment")} == before
        assert not any(call[0] == "DELETE" for call in api.calls)
        assert [item["weight"] for item in api.children("VirtualService")[0]["spec"]["http"][0]["route"]] == [70, 30]

    asyncio.run(run())
