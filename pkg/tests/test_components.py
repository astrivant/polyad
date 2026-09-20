"""
Verify component isolation, self-recovery ownership and fresh global scaling demand.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from polyad.exceptions.reconciliation import Pending
from polyad.operator.coordination.leases import Coordinator, root_shard
from polyad.operator.lifecycle.roles import executes, role, serves
from polyad.operator.observability.pressure import Pressure, demand
from tests.test_coordination import LeaseAPI


@pytest.mark.parametrize(
    ("component", "worker", "api", "metrics"),
    [
        ("dense", True, True, True),
        ("bootstrap", True, False, False),
        ("executor", True, False, False),
        ("gateway", False, True, False),
        ("telemetry", False, False, True),
    ],
)
def test_roles_do_not_run_unrelated_components(monkeypatch, component, worker, api, metrics):
    """
    Split replicas independently select mutation and listener responsibilities.
    """
    monkeypatch.setenv("POLYAD_COMPONENT", component)
    monkeypatch.setenv("POLYAD_API_ENABLED", "true")
    monkeypatch.setenv("POLYAD_METRICS_ENABLED", "true")
    assert executes() == worker
    assert serves("API") == api
    assert serves("METRICS") == metrics
    monkeypatch.setenv("POLYAD_COMPONENT", "unknown")
    with pytest.raises(ValueError):
        role()


def test_bootstrap_keeps_its_graph_shard_and_recovers_without_executors(monkeypatch):
    """
    An executor can never own the shard that must recreate all executor components.
    """

    async def scenario():
        api = LeaseAPI()
        monkeypatch.setenv("POLYAD_SELF_GRAPH", "control-plane")
        monkeypatch.setenv("POLYAD_COMPONENT", "bootstrap")
        bootstrap = Coordinator(api, "test", "bootstrap")
        await bootstrap.tick()
        assert len(bootstrap.owned) == 32
        monkeypatch.setenv("POLYAD_COMPONENT", "executor")
        executor = Coordinator(api, "test", "executor", planner=False)
        await executor.tick()
        await bootstrap.tick()
        assignments = json.loads(
            api.objects["Lease", "test", "polyad-leader"]["metadata"]["annotations"]["polyad.astrivant.com/assignments"]
        )
        reserved = str(root_shard("Graph", "test", "control-plane"))
        assert assignments[reserved] == "bootstrap"
        assert set(assignments.values()) == {"bootstrap", "executor"}
        assert [key for key, value in assignments.items() if value == "bootstrap"] == [reserved]
        assert not executor.leader

    asyncio.run(scenario())


def test_http_pressure_counts_open_streams_and_releases_on_disconnect():
    """
    Streaming subscribers consume capacity for their entire response lifetime.
    """
    pressure = Pressure()

    def app(environ, start_response):
        yield b"first"
        yield b"second"

    response = pressure.wrap(app)({}, None)
    assert next(response) == b"first"
    assert pressure.snapshot()["inFlight"] == 1
    response.close()
    assert pressure.snapshot()["inFlight"] == 0
    assert pressure.snapshot()["requestsPerSecond"] == pytest.approx(1 / 60)


def test_downstream_cluster_backlogs_are_added_once_and_stale_demand_is_rejected():
    """
    KEDA receives the global backlog instead of one randomly selected executor's local work.
    """
    snapshot = {"inbound": {"fresh": True, "total": 3}, "clusters": {"west": {"inbound": {"fresh": True, "total": 7}}}}
    assert demand(snapshot, "executor", "backlog") == 10
    snapshot["clusters"]["west"]["inbound"]["fresh"] = False
    with pytest.raises(ValueError, match="stale"):
        demand(snapshot, "executor", "backlog")
    snapshot["components"] = {"fresh": False, "roles": {"gateway": {"inFlight": 0}}}
    with pytest.raises(ValueError, match="stale"):
        demand(snapshot, "gateway", "inFlight")


def test_telemetry_discovers_executor_reports_without_claiming_leases(monkeypatch):
    """
    Split metrics replicas can aggregate root worker state while remaining read-only participants.
    """
    from polyad.operator.lifecycle import handlers

    async def scenario():
        api = LeaseAPI()
        worker = Coordinator(api, "test", "worker")
        await worker.claim("polyad-member-worker")
        monkeypatch.setenv("POLYAD_COMPONENT", "telemetry")
        reader = Coordinator(api, "test", "reader", planner=False)
        monkeypatch.setattr(handlers, "coordinator", reader)
        monkeypatch.setattr(handlers, "shared", SimpleNamespace(ping=AsyncMock()))
        original_sleep = asyncio.sleep

        async def stop_after_pass(delay):
            if delay == 5:
                raise asyncio.CancelledError
            await original_sleep(delay)

        monkeypatch.setattr(handlers.asyncio, "sleep", stop_after_pass)
        before = list(api.objects)
        with pytest.raises(asyncio.CancelledError):
            await handlers.coordination_loop()
        assert list(api.objects) == before
        assert reader.members == ["worker"]
        assert not reader.owned and not reader.leader

    asyncio.run(scenario())


def test_rendered_service_graph_creates_component_deployments_and_enforces_its_budget():
    """
    Exercise actual graph reconciliation and block a requested group expansion over the parent budget.
    """
    from polyad.operator.reconciliation.controller import Controller
    from tests.test_chart import render
    from tests.test_operator import FakeAPI, resource

    objects = render("ha=true", "architecture.mode=Distributed", "api.enabled=true", "metrics.enabled=true", "architecture.expandedNodes=9")
    definitions = [
        resource(obj["kind"], obj["metadata"]["name"], obj["spec"])
        for obj in objects
        if obj["apiVersion"] == "polyad.astrivant.com/v1alpha1"
    ]

    async def scenario():
        api = FakeAPI(*definitions)
        controller = Controller(api)
        for _ in range(5):
            for key in list(api.objects):
                if key[0] in {"Graph", "ReplicaGroup"}:
                    try:
                        await controller.reconcile(key)
                    except Pending:
                        pass
        deployments = [obj for key, obj in api.objects.items() if key[0] == "Deployment"]
        assert len(deployments) == 6
        assert {obj["spec"]["template"]["metadata"]["labels"]["polyad.astrivant.com/component"] for obj in deployments} == {
            "gateway",
            "executor",
            "telemetry",
        }
        source = api.objects["ReplicaGroup", "test", "test-executor"]
        source["spec"]["replicas"] = 3
        source["metadata"]["generation"] += 1
        instance = next(
            obj
            for key, obj in api.objects.items()
            if key[0] == "ReplicaGroup" and obj["spec"].get("replicaSource", {}).get("name") == "test-executor"
        )
        with pytest.raises(ValueError, match="expandedNodes"):
            await controller.reconcile(("ReplicaGroup", "test", instance["metadata"]["name"]))
        assert len([key for key in api.objects if key[0] == "Deployment"]) == 6

    asyncio.run(scenario())
