"""
Exercise durable pulses, admission policies and daemon scale-out through real reconciliation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from kubernetes.client.exceptions import ApiException

from polyad.api.activations import ActivationStore
from polyad.api.app import Conflict
from polyad.api.builder import APIBuilder
from polyad.compiler.activation import ActivationRequest
from polyad.compiler.asts import GROUP
from polyad.graph.activation import ActivationPolicy
from polyad.operator.controller import Controller, Pending
from polyad.operator.coordination import Coordinator
from tests.test_operator import FakeAPI, resource, template


def setup(mode="Queue", *, daemon=False, **policy):
    """
    Build a persistent graph whose target only starts after a pulse.
    """
    graph = resource(
        "Graph",
        "pulsing",
        {"mode": "persistent", "nodes": [{"name": "target", "kind": "Daemon" if daemon else "Workload", "ref": "target"}]},
    )
    definition = resource(
        "Daemon" if daemon else "Workload", "target", {"template": template(daemon), "activation": {"mode": mode, **policy}}
    )
    api = FakeAPI(graph, definition)
    return api, Controller(api), ActivationStore(api, "test")


def pulse(request_id="pulse-one"):
    """
    Address the concrete graph incarnation.
    """
    return ActivationRequest(requestId=request_id, graph="pulsing", graphUid="uid-pulsing", node="target")


async def turn(controller):
    """
    Allow expected asynchronous waits while letting other errors fail the test.
    """
    try:
        await controller.reconcile(("Graph", "test", "pulsing"))
    except Pending:
        pass


def collect(api):
    """
    Simulate garbage collection after the test observed a deletion request.
    """
    for key, value in list(api.objects.items()):
        if value["metadata"].get("deletionTimestamp"):
            del api.objects[key]


def test_pulse_waits_for_admission_and_retry_does_not_duplicate():
    """
    Preserve an accepted pulse across API timeout, replica change and repeated reconciliation.
    """

    async def scenario():
        api, controller, store = setup()
        await turn(controller)
        assert not api.children("Job")
        api.fail_create_after_commit = True
        with pytest.raises(ApiException):
            await store.submit(pulse())
        first = await store.submit(pulse())
        second = await ActivationStore(api, "test").submit(pulse())
        assert first["uid"] == second["uid"]
        await turn(controller)
        await turn(Controller(api))
        assert len(api.children("Job")) == 1
        assert (await store.lookup("pulse-one"))["status"]["phase"] == "Running"
        request = pulse("conflict")
        await store.submit(request)
        with pytest.raises(Conflict):
            await store.submit(ActivationRequest(requestId="conflict", graph="other", graphUid="other", node="target"))
        coordinator = Coordinator(api, "test", identity="replica")
        assert await coordinator.shard_for(("Activation", "test", first["name"])) == await coordinator.shard_for(
            ("Graph", "test", "pulsing")
        )

    asyncio.run(scenario())


def test_queued_pulse_waits_for_completed_execution_cleanup():
    """
    Start a fresh Job only after its predecessor's foreground deletion is observed.
    """

    async def scenario():
        api, controller, store = setup()
        await store.submit(pulse())
        await turn(controller)
        original = api.children("Job")[0]
        await store.submit(pulse("pulse-two"))
        await turn(controller)
        assert len(api.children("Job")) == 1
        original["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        await turn(controller)
        assert original["metadata"]["deletionTimestamp"]
        assert len(api.children("Job")) == 1
        collect(api)
        await turn(controller)
        assert len(api.children("Job")) == 1
        assert api.children("Job")[0]["metadata"]["uid"] != original["metadata"]["uid"]
        assert (await store.lookup("pulse-one"))["status"]["phase"] == "Completed"
        assert (await store.lookup("pulse-two"))["status"]["phase"] == "Running"

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,expected", [("Reject", "Rejected"), ("Queue", "Pending"), ("Coalesce", "Superseded")])
def test_busy_policy_decisions(mode, expected):
    """
    Apply explicit busy-target semantics without overlapping executions.
    """

    async def scenario():
        api, controller, store = setup(mode)
        await store.submit(pulse())
        await turn(controller)
        await store.submit(pulse("pulse-two"))
        for record in api.children("Activation"):
            record["metadata"]["creationTimestamp"] = "2026-01-01T00:00:01Z"
        await store.submit(pulse("pulse-three"))
        api.children("Activation")[-1]["metadata"]["creationTimestamp"] = "2026-01-01T00:00:02Z"
        await turn(controller)
        assert (await store.lookup("pulse-two"))["status"]["phase"] == expected
        assert len(api.children("Job")) == 1

    asyncio.run(scenario())


def test_parallel_daemons_have_independent_selectors_and_stop_releases_capacity():
    """
    Bound replica groups and keep stop requests durable until deletion finishes.
    """

    async def scenario():
        api, controller, store = setup("Parallel", daemon=True, maxConcurrent=2, replicasPerActivation=3)
        for name in ("one", "two", "three"):
            await store.submit(pulse(name))
        await turn(controller)
        deployments = api.children("Deployment")
        assert len(deployments) == 2
        assert all(item["spec"]["replicas"] == 3 for item in deployments)
        assert deployments[0]["spec"]["selector"] != deployments[1]["spec"]["selector"]
        active = next(record for record in api.children("Activation") if record.get("status", {}).get("phase") == "Running")
        await store.stop(active["spec"]["requestId"])
        await turn(controller)
        assert any(item["metadata"].get("deletionTimestamp") for item in deployments)
        assert len(api.children("Deployment")) == 2
        collect(api)
        await turn(controller)
        assert len(api.children("Deployment")) == 2
        assert (await store.lookup(active["spec"]["requestId"]))["status"]["phase"] == "Stopped"

    asyncio.run(scenario())


def test_frequency_bounds_and_overdue_observation():
    """
    Fence parallel starts by durable admission timestamps and surface missed deadlines.
    """

    async def scenario():
        api, controller, store = setup("Parallel", maxConcurrent=2, minIntervalSeconds=30, maxIntervalSeconds=60)
        await store.submit(pulse())
        await turn(controller)
        await store.submit(pulse("pulse-two"))
        await turn(controller)
        assert len(api.children("Job")) == 1
        for record in api.children("Activation"):
            if record.get("status", {}).get("admittedAt"):
                record["status"]["admittedAt"] = (datetime.now(UTC) - timedelta(seconds=31)).isoformat()
        await turn(controller)
        assert len(api.children("Job")) == 2
        for record in api.children("Activation"):
            record["status"]["admittedAt"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
        await turn(controller)
        graph = api.objects[("Graph", "test", "pulsing")]
        assert graph["status"]["activations"]["target"]["overdue"]
        assert graph["status"]["failed"]

    asyncio.run(scenario())


def test_explicit_deadline_policy_submits_one_durable_timer_pulse():
    """
    Generate repeated timer intent idempotently before its admission is observed.
    """

    async def scenario():
        api, controller, _ = setup(maxIntervalSeconds=60, onDeadline="Activate")
        await turn(controller)
        assert len(api.children("Activation")) == 1
        await turn(controller)
        await turn(controller)
        assert len(api.children("Activation")) == 1
        assert len(api.children("Job")) == 1

    asyncio.run(scenario())


def test_activation_http_contract_and_revision_fences():
    """
    Authenticate pulse routes and reject a request for a replaced graph incarnation.
    """
    app = (
        APIBuilder()
        .with_handlers(lambda _: {}, lambda *_: None)
        .with_activation_handlers(lambda request: {"requestId": request.requestId}, lambda _: None, lambda _: None)
        .with_bearer_token("secret")
        .build()
    )
    client = app.test_client()
    body = {"requestId": "pulse", "graph": "pulsing", "graphUid": "uid-pulsing", "node": "target"}
    assert client.post("/v1/activations", json=body).status_code == 401
    headers = {"Authorization": "Bearer secret"}
    assert client.post("/v1/activations", json=body, headers=headers).status_code == 202
    assert client.get("/v1/activations/missing", headers=headers).status_code == 404
    assert client.post("/v1/activations/missing/stop", headers=headers).status_code == 404

    async def scenario():
        api, controller, store = setup()
        await store.submit(pulse())
        api.objects[("Graph", "test", "pulsing")]["metadata"]["generation"] = 2
        await turn(controller)
        assert not api.children("Job")
        assert (await store.lookup("pulse-one"))["status"]["phase"] == "Failed"

    asyncio.run(scenario())


def test_pulse_preserves_daemon_readiness_dependencies_and_placement():
    """
    A receipt cannot bypass upstream readiness or the containing graph's enforced placement.
    """

    async def scenario():
        api, controller, store = setup()
        graph = api.objects[("Graph", "test", "pulsing")]
        graph["spec"]["placement"] = {"nodeSelector": {"pool": "worker"}, "enforce": True}
        graph["spec"]["nodes"][0]["requires"] = [{"node": "producer", "condition": "ready"}]
        graph["spec"]["nodes"].append({"name": "producer", "kind": "Daemon", "ref": "producer"})
        api.objects[("Daemon", "test", "producer")] = resource("Daemon", "producer", {"template": template(True)})
        await store.submit(pulse())
        await turn(controller)
        assert not api.children("Job")
        producer = api.children("Deployment")[0]
        producer["status"] = {"observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}
        await turn(controller)
        job = api.children("Job")[0]
        assert job["spec"]["template"]["spec"]["nodeSelector"] == {"pool": "worker"}
        assert not producer["metadata"].get("deletionTimestamp")

    asyncio.run(scenario())


def test_subgraph_pulses_and_suspension_retain_receipts():
    """
    Own fresh graph instances while preserving receipt identity through suspension and cleanup.
    """

    async def scenario():
        api, controller, store = setup()
        parent = api.objects[("Graph", "test", "pulsing")]
        parent["spec"]["nodes"][0]["kind"] = "Graph"
        api.objects[("Graph", "test", "target")] = resource(
            "Graph",
            "target",
            {
                "nodes": [],
                "templateOnly": True,
                "activation": {"mode": "Parallel", "maxConcurrent": 2},
            },
        )
        await store.submit(pulse())
        await store.submit(pulse("pulse-two"))
        await turn(controller)
        children = [graph for graph in api.children("Graph") if graph["metadata"].get("ownerReferences")]
        assert len(children) == 2
        assert all("activation" not in child["spec"] and not child["spec"]["templateOnly"] for child in children)
        parent["spec"]["suspend"] = True
        parent["metadata"]["generation"] += 1
        await turn(controller)
        assert all(child["metadata"].get("deletionTimestamp") for child in children)
        collect(api)
        await turn(controller)
        assert len(api.children("Activation")) == 2
        assert all(receipt["status"]["phase"] == "Stopped" for receipt in api.children("Activation"))

    asyncio.run(scenario())


def test_dormant_pulse_does_not_reserve_capacity(monkeypatch):
    """
    A capacity-enabled persistent graph waits for pulse intent before forecasting its target.
    """
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", "true")

    async def scenario():
        api, controller, _ = setup()
        api.objects[("Graph", "test", "pulsing")]["spec"]["capacity"] = {"backend": "Placeholders"}
        await turn(controller)
        assert not api.children("Pod") and not api.children("PodTemplate")
        graph = api.objects[("Graph", "test", "pulsing")]
        assert graph["status"]["ready"]
        assert graph["status"]["metrics"]["execution"]["pendingNodes"] == 0

    asyncio.run(scenario())


def test_parallel_pulses_preserve_logical_network_guards_and_gate_facts():
    """
    Guard every pulse with the same traffic scope and wait for all replicas at a logical gate.
    """

    async def scenario():
        api, controller, store = setup("Parallel", daemon=True, maxConcurrent=2)
        parent = api.objects[("Graph", "test", "pulsing")]
        parent["spec"]["network"] = {
            "allowWithin": False,
            "allowDNS": False,
            "ingress": [{"node": "target", "peer": {"node": "next"}, "ports": [{"port": 8080}]}],
        }
        parent["spec"]["nodes"].append({"name": "next", "kind": "Workload", "ref": "next", "gate": "ready"})
        api.objects[("Workload", "test", "next")] = resource("Workload", "next", {"template": template(False)})
        api.objects[("Gate", "test", "ready")] = resource("Gate", "ready", {"expression": {"operator": "signal", "name": "target.ready"}})
        await store.submit(pulse())
        await store.submit(pulse("pulse-two"))
        await turn(controller)
        assert api.children("NetworkPolicy") and not api.children("Deployment")
        await turn(controller)
        deployments = api.children("Deployment")
        assert len(deployments) == 2
        policy = next(
            item
            for item in api.children("NetworkPolicy")
            if item["spec"]["podSelector"]["matchLabels"][f"{GROUP}/network-node"] == "target"
        )
        selector = policy["spec"]["podSelector"]["matchLabels"]
        assert policy["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8080}]
        for item in deployments:
            assert selector.items() <= item["spec"]["template"]["metadata"]["labels"].items()
        assert not api.children("Job")
        for item in deployments:
            item["status"] = {"observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}
            await turn(controller)
            if item is deployments[0]:
                assert not api.children("Job")
        assert len(api.children("Job")) == 1
        metrics = parent["status"]["metrics"]
        assert not metrics["topologyError"]
        assert metrics["execution"]["observedNodes"] == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "policy", [{"maxConcurrent": 2}, {"minIntervalSeconds": 10, "maxIntervalSeconds": 5}, {"maxPending": 0}, {"onDeadline": "Activate"}]
)
def test_invalid_activation_policies(policy):
    """
    Reject contradictory or unbounded scheduling requests.
    """
    with pytest.raises(ValueError):
        ActivationPolicy(**policy)
