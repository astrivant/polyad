"""
Verify scalable graph families against delayed writes, deletion and source changes.
"""

from __future__ import annotations

import asyncio

import pytest
from openapi_spec_validator import validate

from polyad.metrics.builder import MetricsAPIBuilder
from polyad.metrics.store import MetricsStore
from polyad.operator.controller import Controller, Pending
from polyad.operator.replication import replica_selector
from tests.test_metrics_api import snapshot
from tests.test_operator import FakeAPI, resource, template


async def turn(api, name="copies", kind="ReplicaGroup"):
    """
    Reconcile a fresh controller while accepting asynchronous cleanup waits.
    """
    try:
        await Controller(api).reconcile((kind, "test", name))
    except Pending:
        pass


def group(replicas=2, kind="Daemon", **spec):
    """
    Build a bounded executable group referencing a reusable definition.
    """
    return resource("ReplicaGroup", "copies", {"replicas": replicas, "template": {"kind": kind, "ref": "worker"}, **spec})


def test_replica_growth_keeps_existing_uids_and_scale_zero_waits_for_cleanup():
    """
    Scale an ordinary daemon abstraction without replacing existing ordinal identities.
    """

    async def scenario():
        api = FakeAPI(group(), resource("Daemon", "worker", {"template": template(True)}))
        await turn(api)
        original = {child["metadata"]["uid"] for child in api.children("Deployment")}
        root = api.objects[("ReplicaGroup", "test", "copies")]
        root["spec"]["replicas"] = 3
        root["metadata"]["generation"] += 1
        await turn(api)
        assert original < {child["metadata"]["uid"] for child in api.children("Deployment")}
        for child in api.children("Deployment"):
            assert child["spec"]["template"]["metadata"]["labels"][replica_selector(root["metadata"]["uid"])] == "true"
        root["spec"]["replicas"] = 0
        root["metadata"]["generation"] += 1
        await turn(api)
        assert root["status"]["replicas"] == 3
        assert all(child["metadata"].get("deletionTimestamp") for child in api.children("Deployment"))
        for child in api.children("Deployment"):
            del api.objects[("Deployment", "test", child["metadata"]["name"])]
        await turn(api)
        assert root["status"]["replicas"] == 0
        assert root["status"]["ready"]
        assert not root["status"]["metrics"]["topologyError"]

    asyncio.run(scenario())


def test_finite_replicas_finish_before_scale_in():
    """
    Keep an active finite workload until completion, then observe its finalizer cleanup.
    """

    async def scenario():
        api = FakeAPI(group(1, "Workload"), resource("Workload", "worker", {"template": template(False)}))
        await turn(api)
        root = api.objects[("ReplicaGroup", "test", "copies")]
        root["spec"]["replicas"] = 0
        root["metadata"]["generation"] += 1
        await turn(api)
        job = api.children("Job")[0]
        assert not job["metadata"].get("deletionTimestamp")
        job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        await turn(api)
        assert job["metadata"]["deletionTimestamp"]

    asyncio.run(scenario())


def test_reusable_group_scales_all_uses_and_instance_can_opt_out():
    """
    Propagate one reusable group's count without changing or replacing its generated instances.
    """

    async def scenario():
        definition = group(templateOnly=True)
        parent = resource(
            "Graph",
            "root",
            {"mode": "persistent", "nodes": [{"name": name, "kind": "ReplicaGroup", "ref": "copies"} for name in ("first", "second")]},
        )
        api = FakeAPI(definition, parent, resource("Daemon", "worker", {"template": template(True)}))
        await turn(api, "root", "Graph")
        instances = [child for child in api.children("ReplicaGroup") if not child["spec"].get("templateOnly")]
        assert len(instances) == 2
        for child in instances:
            await turn(api, child["metadata"]["name"])
            await turn(api, child["metadata"]["name"])
        assert len(api.children("Deployment")) == 4
        instances[0]["spec"]["inheritReplicas"] = False
        instances[0]["metadata"]["generation"] += 1
        source = api.objects[("ReplicaGroup", "test", "copies")]
        source["spec"]["replicas"] = 3
        source["metadata"]["generation"] += 1
        await turn(api, "root", "Graph")
        for child in instances:
            assert not child["metadata"].get("deletionTimestamp")
            await turn(api, child["metadata"]["name"])
        assert len(api.children("Deployment")) == 5
        await turn(api)
        assert source["status"]["instanceCount"] == 1
        assert source["status"]["replicas"] == 3
        assert source["status"]["scaleCurrent"]
        assert instances[0]["status"]["replicas"] == 2

    asyncio.run(scenario())


def test_nested_group_status_and_rules_count_replication():
    """
    Replicate entire finite graphs and reject mathematical limits before creating excess work.
    """

    async def scenario():
        graph = resource("Graph", "worker", {"templateOnly": True, "nodes": []})
        api = FakeAPI(group(2, "Graph"), graph)
        await turn(api)
        children = [item for item in api.children("Graph") if not item["spec"].get("templateOnly")]
        assert len(children) == 2
        for item in children:
            await turn(api, item["metadata"]["name"], "Graph")
            await turn(api, item["metadata"]["name"], "Graph")
        await turn(api)
        root = api.objects[("ReplicaGroup", "test", "copies")]
        assert root["status"]["readyReplicas"] == 2
        assert root["status"]["metrics"]["rollup"]["graphCount"] == 3
        api.objects[("GraphRule", "test", "bounded")] = resource("GraphRule", "bounded", {"limits": {"nodes": 2}})
        root["spec"]["replicas"] = 3
        root["metadata"]["generation"] += 1
        with pytest.raises(ValueError, match="exceeds"):
            await turn(api)
        assert len([item for item in api.children("Graph") if not item["spec"].get("templateOnly")]) == 2

    asyncio.run(scenario())


def test_keda_scalar_endpoint_aggregates_definition_and_rejects_stale_observations():
    """
    Serve a single finite scalar and never present absent or stale demand as zero.
    """

    async def scenario():
        api = FakeAPI(group(), resource("Daemon", "worker", {"template": template(True)}))
        await turn(api)
        store = MetricsStore()
        store.publish(snapshot(list(api.objects.values())), graph_labels=True)
        app = MetricsAPIBuilder().with_store(store).build().test_client()
        validate(app.get("/openapi.json").json)
        assert app.get("/v1/workloads/ReplicaGroup/copies/replicas").json["value"] == 2
        await turn(api)
        store.publish(snapshot(list(api.objects.values())))
        assert app.get("/v1/workloads/Daemon/worker/executions").json["value"] == 2
        assert app.get("/v1/workloads/ReplicaGroup/copies/executions?node=replica-0").json["value"] == 1
        assert app.get("/v1/workloads/ReplicaGroup/absent/replicas").status_code == 404
        assert app.get("/v1/workloads/Daemon/worker/no-such-signal").status_code == 404
        root = api.objects[("ReplicaGroup", "test", "copies")]
        root["status"]["scaleObservedAt"] = "2000-01-01T00:00:00+00:00"
        root["status"]["workloadsObservedAt"] = "2000-01-01T00:00:00+00:00"
        store.publish(snapshot(list(api.objects.values())))
        assert app.get("/v1/workloads/ReplicaGroup/copies/replicas").status_code == 503
        assert app.get("/v1/workloads/Daemon/worker/executions").status_code == 503

    asyncio.run(scenario())


def test_replica_group_composition_resolves_template_ids():
    """
    Keep replication templates within composition auditing and cycle detection.
    """
    from polyad.compiler.passes.composition import CompositionItem, CompositionRequest, compile_composition

    request = CompositionRequest(
        requestId="replicate",
        rootId="copies",
        objects=(
            CompositionItem(id="copies", kind="ReplicaGroup", spec={"replicas": 2, "template": {"refId": "worker"}}),
            CompositionItem(id="worker", kind="Daemon", spec={"template": template(True)}),
        ),
    )
    result = compile_composition(request, "test")
    assert result["copies"].spec["template"]["kind"] == "Daemon"
    assert result["copies"].spec["template"]["ref"] == result["worker"].metadata.name


def test_new_unobserved_use_blocks_definition_metric():
    """
    A new graph reference must not make aggregate demand look smaller than the actual namespace.
    """

    async def scenario():
        api = FakeAPI(group(), resource("Daemon", "worker", {"template": template(True)}))
        await turn(api)
        await turn(api)
        unknown = resource("Graph", "new-use", {"mode": "persistent", "nodes": [{"kind": "Daemon", "ref": "worker", "name": "consumer"}]})
        store = MetricsStore()
        store.publish(snapshot([*api.objects.values(), unknown]))
        client = MetricsAPIBuilder().with_store(store).build().test_client()
        assert client.get("/v1/workloads/Daemon/worker/executions").status_code == 503

    asyncio.run(scenario())
