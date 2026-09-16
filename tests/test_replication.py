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


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
def test_replica_growth_keeps_existing_uids_and_scale_zero_waits_for_cleanup(kind):
    """
    Scale an ordinary daemon abstraction without replacing existing ordinal identities.
    """

    async def scenario():
        api = FakeAPI(group(), resource("Daemon", "worker", {"template": template(True)}))
        if kind == "StatefulSet":
            from tests.test_statefulsets import stateful_spec

            api.objects[("Daemon", "test", "worker")]["spec"].update(stateful_spec())
        await turn(api)
        original = {child["metadata"]["uid"] for child in api.children(kind)}
        root = api.objects[("ReplicaGroup", "test", "copies")]
        root["spec"]["replicas"] = 3
        root["metadata"]["generation"] += 1
        await turn(api)
        assert original < {child["metadata"]["uid"] for child in api.children(kind)}
        for child in api.children(kind):
            assert child["spec"]["template"]["metadata"]["labels"][replica_selector(root["metadata"]["uid"])] == "true"
        root["spec"]["replicas"] = 0
        root["metadata"]["generation"] += 1
        await turn(api)
        assert root["status"]["replicas"] == 3
        assert all(child["metadata"].get("deletionTimestamp") for child in api.children(kind))
        for child in api.children(kind):
            del api.objects[(kind, "test", child["metadata"]["name"])]
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
    from polyad.compiler.passes.composition import compile_composition
    from polyad_types.requests import CompositionItem, CompositionRequest

    request = CompositionRequest(
        requestId="replicate",
        rootId="copies",
        objects=(
            CompositionItem(
                id="copies",
                kind="ReplicaGroup",
                spec={
                    "replicas": 2,
                    "template": {"refId": "worker"},
                    "connectivity": {
                        "mode": "Custom",
                        "edges": [{"source": "replica-0", "target": "replica-1", "ports": [{"port": 8080}]}],
                    },
                },
            ),
            CompositionItem(id="worker", kind="Daemon", spec={"template": template(True)}),
        ),
    )
    result = compile_composition(request, "test")
    assert result["copies"].spec["template"]["kind"] == "Daemon"
    assert result["copies"].spec["template"]["ref"] == result["worker"].metadata.name
    assert result["copies"].spec["connectivity"] == request.objects[0].spec["connectivity"]


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


def policy_family(*, bound=3, replicas=2, uses=("workers",), scope="Boundary", **constraints):
    """
    Put optional constraints on a PolyGraph containing independently scalable groups.
    """
    policy = resource(
        "GraphRule", "budget", {"enforcement": "Referenced", "scope": scope, "limits": {"expandedNodes": bound}, **constraints}
    )
    parent = resource(
        "PolyGraph",
        "root",
        {"mode": "persistent", "rules": ["budget"], "nodes": [{"name": name, "kind": "ReplicaGroup", "ref": "copies"} for name in uses]},
    )
    return FakeAPI(parent, policy, group(replicas, templateOnly=True), resource("Daemon", "worker", {"template": template(True)}))


async def start_family(api):
    """
    Materialize the parent and each group's initial daemon copies.
    """
    await turn(api, "root", "PolyGraph")
    instances = [item for item in api.children("ReplicaGroup") if not item["spec"].get("templateOnly")]
    for instance in instances:
        await turn(api, instance["metadata"]["name"])
        await turn(api, instance["metadata"]["name"])
    return instances


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
def test_polygraph_boundary_rule_blocks_child_scale_out(kind):
    """
    A non-inherited parent budget still constrains independently scaled descendants.
    """

    async def scenario():
        api = policy_family()
        if kind == "StatefulSet":
            from tests.test_statefulsets import stateful_spec

            api.objects[("Daemon", "test", "worker")]["spec"].update(stateful_spec())
        (instance,) = await start_family(api)
        assert instance["spec"].get("rules", []) == []
        instance["spec"].update(inheritReplicas=False, replicas=3)
        instance["metadata"]["generation"] += 1
        api.calls.clear()
        with pytest.raises(ValueError, match="expandedNodes=4"):
            await turn(api, instance["metadata"]["name"])
        assert not any(method in {"POST", "DELETE"} for method, _, _ in api.calls)
        assert not instance["status"]["scaleCurrent"]
        assert len(api.children(kind)) == 2

    asyncio.run(scenario())


def test_shared_scale_request_reserves_all_uses_in_parent_budget():
    """
    Recompute all inheriting uses instead of checking a single enlarged child in isolation.
    """

    async def scenario():
        api = policy_family(bound=7, uses=("left", "right"))
        instances = await start_family(api)
        source = api.objects[("ReplicaGroup", "test", "copies")]
        source["spec"]["replicas"] = 3
        source["metadata"]["generation"] += 1
        for instance in instances:
            with pytest.raises(ValueError, match="expandedNodes=8"):
                await turn(api, instance["metadata"]["name"])
        assert len(api.children("Deployment")) == 4
        await turn(api)
        assert not source["status"]["scaleCurrent"]

    asyncio.run(scenario())


def test_pending_sibling_removal_does_not_release_parent_budget():
    """
    Count terminating siblings until Kubernetes actually removes their execution resources.
    """

    async def scenario():
        api = policy_family(bound=6, uses=("left", "right"))
        left, right = await start_family(api)
        for instance in (left, right):
            instance["spec"]["inheritReplicas"] = False
        left["spec"]["replicas"] = 0
        left["metadata"]["generation"] += 1
        await turn(api, left["metadata"]["name"])
        right["spec"]["replicas"] = 3
        right["metadata"]["generation"] += 1
        with pytest.raises(ValueError, match="expandedNodes=7"):
            await turn(api, right["metadata"]["name"])
        assert len(api.children("Deployment")) == 4
        for key, item in list(api.objects.items()):
            if item["kind"] == "Deployment" and item["metadata"].get("deletionTimestamp"):
                del api.objects[key]
        await turn(api, right["metadata"]["name"])
        assert len(api.children("Deployment")) == 3

    asyncio.run(scenario())


def test_refreshed_shapes_block_scale_in_before_deletion():
    """
    Reject a requested zero count when a newly selected subtree rule requires connectivity.
    """

    async def scenario():
        api = policy_family(bound=2, replicas=1, scope="Subtree", shapes=["connected"])
        (instance,) = await start_family(api)
        source = api.objects[("ReplicaGroup", "test", "copies")]
        source["spec"]["replicas"] = 0
        source["metadata"]["generation"] += 1
        api.calls.clear()
        with pytest.raises(ValueError, match="required shape: connected"):
            await turn(api, instance["metadata"]["name"])
        assert not any(method == "DELETE" for method, _, _ in api.calls)
        assert not instance["status"]["scaleCurrent"]

    asyncio.run(scenario())


def test_rule_update_between_replica_creations_stops_the_next_action():
    """
    Recompute constraints after each write instead of reusing a verdict for the whole batch.
    """

    class ChangingAPI(FakeAPI):
        async def request(self, method, kind, namespace, name="", body=None, **kwargs):
            result = await super().request(method, kind, namespace, name, body, **kwargs)
            if method == "POST" and kind == "Deployment":
                rule = self.objects[("GraphRule", "test", "limit")]
                rule["spec"]["limits"]["nodes"] = 1
                rule["metadata"]["generation"] += 1
            return result

    async def scenario():
        api = ChangingAPI(
            group(), resource("Daemon", "worker", {"template": template(True)}), resource("GraphRule", "limit", {"limits": {"nodes": 2}})
        )
        with pytest.raises(ValueError, match="nodes=2"):
            await turn(api)
        assert len(api.children("Deployment")) == 1

    asyncio.run(scenario())


def test_live_cheeger_change_blocks_nested_scaling():
    """
    Refresh an ancestor's Cheeger bound before a descendant scale action.
    """

    async def scenario():
        api = policy_family(bound=10, uses=("left", "right"), relation="connections", cheeger={"minimum": 1})
        root = api.objects[("PolyGraph", "test", "root")]
        root["spec"]["connections"] = [{"source": "left", "target": "right"}]
        instances = await start_family(api)
        assert any(report["measurements"].get("cheeger") == 1 for report in root["status"]["structuralRules"])
        root["spec"]["connections"] = []
        root["metadata"]["generation"] += 1
        source = api.objects[("ReplicaGroup", "test", "copies")]
        source["spec"]["replicas"] = 3
        source["metadata"]["generation"] += 1
        api.calls.clear()
        with pytest.raises(ValueError, match="cheeger=0"):
            await turn(api, instances[0]["metadata"]["name"])
        assert not any(method in {"POST", "DELETE"} for method, _, _ in api.calls)

    asyncio.run(scenario())


def test_rule_update_between_removals_stops_scale_in():
    """
    Recheck lower-bound shape constraints before every destructive scaling action.
    """

    class ChangingAPI(FakeAPI):
        async def delete(self, obj):
            await super().delete(obj)
            self.objects[("GraphRule", "test", "limit")]["spec"]["shapes"] = ["connected"]

    async def scenario():
        api = ChangingAPI(group(), resource("Daemon", "worker", {"template": template(True)}), resource("GraphRule", "limit"))
        await turn(api)
        api.objects[("ReplicaGroup", "test", "copies")]["spec"]["replicas"] = 0
        with pytest.raises(ValueError, match="required shape: connected"):
            await turn(api)
        assert sum(bool(child["metadata"].get("deletionTimestamp")) for child in api.children("Deployment")) == 1

    asyncio.run(scenario())


def test_changed_source_during_family_evaluation_defers_writes():
    """
    Detect a scale request changing while its family's expensive constraints are computed.
    """

    class ChangingAPI(FakeAPI):
        change_source = False

        async def request(self, method, kind, namespace, name="", body=None, **kwargs):
            result = await super().request(method, kind, namespace, name, body, **kwargs)
            if method == "GET" and kind == "GraphRule" and self.change_source:
                self.change_source = False
                source = self.objects[("ReplicaGroup", "test", "copies")]
                source["spec"]["replicas"] = 3
                source["metadata"]["generation"] += 1
            return result

    async def scenario():
        original = policy_family(bound=8)
        api = ChangingAPI(*original.objects.values())
        (instance,) = await start_family(api)
        api.change_source = True
        api.calls.clear()
        await turn(api, instance["metadata"]["name"])
        assert not any(method in {"POST", "DELETE"} for method, _, _ in api.calls)
        assert not instance["status"]["scaleCurrent"]
        assert "changed during" in instance["status"]["message"]

    asyncio.run(scenario())


def test_refreshed_spectral_rule_blocks_descendant_scaling():
    """
    Reevaluate an ancestor spectrum instead of trusting its last successful status.
    """

    async def scenario():
        api = policy_family(bound=10, uses=("left", "right"), relation="connections", spectrum={"maxRadius": 1})
        root = api.objects[("PolyGraph", "test", "root")]
        root["spec"]["connections"] = [{"source": "left", "target": "right"}]
        instance, _ = await start_family(api)
        rule = api.objects[("GraphRule", "test", "budget")]
        rule["spec"]["spectrum"]["maxRadius"] = 0.5
        rule["metadata"]["generation"] += 1
        api.objects[("ReplicaGroup", "test", "copies")]["spec"]["replicas"] = 3
        api.calls.clear()
        with pytest.raises(ValueError, match="radius=1"):
            await turn(api, instance["metadata"]["name"])
        assert not any(method in {"POST", "DELETE"} for method, _, _ in api.calls)

    asyncio.run(scenario())
