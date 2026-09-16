"""
Verify that managed workloads can discover their graph and activation identities.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from polyad.compiler.passes.identity import inject_environment
from polyad.operator.controller import Controller, Pending
from polyad.operator.identity import graph_ancestry
from polyad_types.resources import GROUP
from tests.test_activations import pulse, setup, turn
from tests.test_composition import settle
from tests.test_operator import FakeAPI, resource, template


def environment(pod):
    """
    Read literal values while retaining Kubernetes Downward API fields for inspection.
    """
    return {item["name"]: item.get("value", item.get("valueFrom")) for item in pod["spec"]["containers"][0]["env"]}


@pytest.mark.parametrize("kind", ["Workload", "Daemon"])
def test_every_workload_container_gets_trusted_identity(monkeypatch, kind):
    """
    Cover finite and persistent work while preserving explicit credential references.
    """
    monkeypatch.setenv("POLYAD_WORKLOAD_API_URL", "http://operator-api.orchestration.svc:8090")

    async def scenario():
        pod = template(kind == "Daemon")
        token = {"name": "POLYAD_API_TOKEN", "valueFrom": {"secretKeyRef": {"name": "caller", "key": "token"}}}
        pod["spec"]["containers"][0]["env"] = [
            {"name": "POLYAD_GRAPH_UID", "value": "spoofed"},
            {"name": "POLYAD_GRAPH_UID", "value": "duplicate"},
            token,
            {"name": "LOG_PREFIX", "value": "$(POLYAD_GRAPH_UID)/$(POLYAD_NODE_NAME)"},
        ]
        pod["spec"]["containers"][0]["envFrom"] = [{"secretRef": {"name": "application"}}]
        pod["spec"]["initContainers"] = [
            {"name": "init", "image": "test"},
            {"name": "sidecar", "image": "test", "restartPolicy": "Always"},
        ]
        spec = {"template": pod}
        definition = resource(kind, "worker", spec)
        original = copy.deepcopy(definition)
        graph = resource("Graph", "processing", {"mode": "persistent", "nodes": [{"name": "process", "kind": kind, "ref": "worker"}]})
        api = FakeAPI(graph, definition)
        try:
            await Controller(api).reconcile(("Graph", "test", "processing"))
        except Pending:
            pass
        child = api.children("Deployment" if kind == "Daemon" else "Job")[0]
        compiled = child["spec"]["template"]
        env = environment(compiled)
        assert env["POLYAD_GRAPH_UID"] == env["POLYAD_ROOT_GRAPH_UID"] == graph["metadata"]["uid"]
        assert env["POLYAD_GRAPH_NAME"] == "processing" and env["POLYAD_GRAPH_NAMESPACE"] == "test"
        assert env["POLYAD_NODE_NAME"] == env["POLYAD_RUNTIME_NODE_NAME"] == "process"
        assert env["POLYAD_NODE_PATH"] == "processing/process"
        assert env["POLYAD_DEFINITION_UID"] == definition["metadata"]["uid"]
        assert env["POLYAD_DEFINITION_KIND"] == kind
        assert env["POLYAD_RESOURCE_NAME"] == child["metadata"]["name"]
        assert env["POLYAD_API_URL"] == "http://operator-api.orchestration.svc:8090"
        assert env["POLYAD_ACTIVATION_UID"] == ""
        assert env["POLYAD_POD_UID"] == {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}}
        assert env["POLYAD_KUBERNETES_NODE_NAME"]["fieldRef"]["fieldPath"] == "spec.nodeName"
        for container in compiled["spec"]["containers"] + compiled["spec"]["initContainers"]:
            entries = container["env"]
            assert len({item["name"] for item in entries}) == len(entries)
            assert next(item for item in entries if item["name"] == "POLYAD_GRAPH_UID")["value"] == "uid-processing"
        assert token in compiled["spec"]["containers"][0]["env"]
        assert compiled["spec"]["containers"][0]["envFrom"] == [{"secretRef": {"name": "application"}}]
        assert api.objects[(kind, "test", "worker")] == original

    asyncio.run(scenario())


def test_nested_graph_and_replica_ancestry():
    """
    Traverse actual instances through nested graphs and replica groups to the root.
    """

    async def scenario():
        api = FakeAPI(
            resource("PolyGraph", "root", {"mode": "persistent", "nodes": [{"name": "copies", "kind": "ReplicaGroup", "ref": "copies"}]}),
            resource(
                "ReplicaGroup",
                "copies",
                {"templateOnly": True, "replicas": 1, "template": {"kind": "Graph", "ref": "batch"}},
            ),
            resource("Graph", "batch", {"templateOnly": True, "nodes": [{"name": "run", "kind": "Workload", "ref": "worker"}]}),
            resource("Workload", "worker", {"template": template()}),
        )
        await settle(api)
        job = api.children("Job")[0]
        env = environment(job["spec"]["template"])
        chain = json.loads(env["POLYAD_GRAPH_ANCESTRY"])
        assert [item["kind"] for item in chain] == ["PolyGraph", "ReplicaGroup", "Graph"]
        assert env["POLYAD_ROOT_GRAPH_UID"] == "uid-root"
        assert env["POLYAD_GRAPH_UID"] == job["metadata"]["ownerReferences"][0]["uid"]
        assert env["POLYAD_GRAPH_NAME"] != "batch"
        group = resource("ReplicaGroup", "copies", {"replicas": 2, "template": {"kind": "Daemon", "ref": "server"}})
        api = FakeAPI(group, resource("Daemon", "server", {"template": template(True)}))
        try:
            await Controller(api).reconcile(("ReplicaGroup", "test", "copies"))
        except Pending:
            pass
        copies = [environment(item["spec"]["template"]) for item in api.children("Deployment")]
        assert len(copies) == 2
        assert len({item["POLYAD_NODE_NAME"] for item in copies}) == 2
        assert all(item["POLYAD_GRAPH_KIND"] == "ReplicaGroup" for item in copies)

    asyncio.run(scenario())


@pytest.mark.parametrize("daemon", [False, True])
def test_parallel_activations_keep_logical_identity_and_distinct_receipts(daemon):
    """
    Per-run names and receipts must replace prototype values without changing logical targets.
    """

    async def scenario():
        api, controller, store = setup("Parallel", daemon=daemon, maxConcurrent=2)
        for name in ("one", "two"):
            await store.submit(pulse(name))
        await turn(controller)
        executions = api.children("Deployment" if daemon else "Job")
        assert len(executions) == 2
        receipts = set()
        for child in executions:
            env = environment(child["spec"]["template"])
            assert env["POLYAD_NODE_NAME"] == "target"
            assert env["POLYAD_RUNTIME_NODE_NAME"] == child["metadata"]["labels"][f"{GROUP}/runtime-node"]
            assert env["POLYAD_RESOURCE_NAME"] == child["metadata"]["name"]
            assert env["POLYAD_ACTIVATION_UID"] == child["metadata"]["annotations"][f"{GROUP}/activation-uid"]
            receipts.add(env["POLYAD_ACTIVATION_ID"])
        assert receipts == {"one", "two"}

    asyncio.run(scenario())


def test_recreated_parent_cannot_supply_a_false_root_identity():
    """
    Refuse to compile workloads against a replacement owner with the same name.
    """

    async def scenario():
        parent = resource("PolyGraph", "root")
        graph = resource("Graph", "nested")
        graph["metadata"]["ownerReferences"] = [
            {"apiVersion": parent["apiVersion"], "kind": "PolyGraph", "name": "root", "uid": "old-uid", "controller": True}
        ]
        with pytest.raises(Pending):
            await graph_ancestry(FakeAPI(parent, graph), graph)

    asyncio.run(scenario())


def test_activated_subgraph_propagates_receipt_to_descendants():
    """
    A leaf inside an activated graph receives that graph's receipt and concrete instance UID.
    """
    from polyad.api.activations import ActivationStore

    async def scenario():
        graph = resource("Graph", "pulsing", {"mode": "persistent", "nodes": [{"name": "target", "kind": "Graph", "ref": "batch"}]})
        definition = resource(
            "Graph",
            "batch",
            {"templateOnly": True, "activation": {"mode": "Queue"}, "nodes": [{"name": "run", "kind": "Workload", "ref": "worker"}]},
        )
        api = FakeAPI(graph, definition, resource("Workload", "worker", {"template": template()}))
        receipt = await ActivationStore(api, "test").submit(pulse("nested-pulse"))
        await settle(api)
        job = api.children("Job")[0]
        env = environment(job["spec"]["template"])
        assert env["POLYAD_ACTIVATION_UID"] == receipt["uid"]
        assert env["POLYAD_ACTIVATION_ID"] == "nested-pulse"
        assert env["POLYAD_GRAPH_UID"] == job["metadata"]["ownerReferences"][0]["uid"]
        assert env["POLYAD_GRAPH_UID"] != env["POLYAD_ROOT_GRAPH_UID"]

    asyncio.run(scenario())


def test_injection_is_idempotent_and_escapes_literal_values():
    """
    Context data must not become Kubernetes variable substitution or duplicate entries.
    """
    pod = {"spec": {"containers": [{"name": "main", "env": [{"name": "APP", "value": "$(POLYAD_GRAPH_UID)"}]}]}}
    inject_environment(pod, {"POLYAD_GRAPH_UID": "$(APP)"})
    original = copy.deepcopy(pod)
    inject_environment(pod, {"POLYAD_GRAPH_UID": "$(APP)"})
    assert pod == original
    assert environment(pod)["POLYAD_GRAPH_UID"] == "$$(APP)"
