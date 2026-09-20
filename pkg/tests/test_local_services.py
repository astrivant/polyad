"""
Verify complete chart inventory and read-only native service observations.
"""

from __future__ import annotations

import asyncio
import copy
import json
from unittest.mock import Mock

import pytest
from deepdiff import DeepDiff

from polyad.events.visibility import INTERNAL, public_observation
from polyad.exceptions.reconciliation import Pending
from polyad.operator.adapters.kubernetes import API
from polyad.operator.clusters.reserved import RESOURCES, members
from polyad.operator.reconciliation.controller import Controller
from tests.test_chart import render
from tests.test_operator import resource
from tests.test_reserved_topology import operator_deployment
from tests.test_root_control_plane import ManagementAPI, manager


def inventory(objects):
    """
    Read the chart-generated identities passed to the root process.
    """
    root = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    env = {entry["name"]: entry.get("value") for entry in root["spec"]["template"]["spec"]["containers"][0]["env"]}
    return json.loads(env["POLYAD_LOCAL_SERVICES"])


@pytest.mark.parametrize("mode", ["Dense", "Distributed"])
def test_chart_inventory_covers_all_enabled_local_workloads_and_services(mode):
    """
    Observe the actual rendered names, including dependency naming overrides and database Services.
    """
    objects = render(
        f"architecture.mode={mode}",
        "kedaOperator.operator.name=custom-scaler",
        "kedaOperator.webhooks.enabled=false",
        values_files=("root-values.yaml", "local-services-values.yaml"),
    )
    groups = inventory(objects)
    assert set(groups) == {"endpoints", "keda", "dragonfly", "postgresql", "mesh", "observer"}
    targets = {(entry["kind"], entry["namespace"], entry["name"]) for entries in groups.values() for entry in entries}
    declared = {
        (obj["kind"], obj["metadata"].get("namespace", "test"), obj["metadata"]["name"])
        for obj in objects
        if obj["kind"] in {"Deployment", "StatefulSet", "DaemonSet", "Service", "Dragonfly", "Cluster"}
    }
    assert declared - {("Deployment", "test", "test-polyad")} <= targets
    assert ("Deployment", "test", "custom-scaler") in targets
    assert not any(name == "keda-admission-webhooks" for _, _, name in targets)
    assert ("StatefulSet", "test", "test-queue") in targets
    assert ("Service", "test", "test-state-rw") in targets
    assert all(set(entry) == {"kind", "namespace", "name"} for entries in groups.values() for entry in entries)
    roles = [obj for obj in objects if obj["kind"] == "Role" and obj["metadata"]["name"].endswith("service-observations")]
    assert roles and all(rule["verbs"] == ["get"] and rule["resourceNames"] for obj in roles for rule in obj["rules"])


def test_existing_keda_uses_explicit_namespace_and_named_read_permissions():
    """
    Existing autoscalers join the model without installing another controller or broadening write access.
    """
    objects = render("keda.observation.namespace=scaling", values_files=("root-values.yaml",))
    assert not any(obj["kind"] == "Deployment" and obj["metadata"]["name"] == "keda-operator" for obj in objects)
    assert {target["namespace"] for target in inventory(objects)["keda"]} == {"scaling"}
    role = next(obj for obj in objects if obj["kind"] == "Role" and obj["metadata"].get("namespace") == "scaling")
    assert {tuple(rule["verbs"]) for rule in role["rules"]} == {("get",)}
    assert not any(obj["kind"] == "ClusterRole" and obj["metadata"]["name"].endswith("service-observations") for obj in objects)
    objects = render("keda.observation.enabled=false", values_files=("root-values.yaml",))
    assert "keda" not in inventory(objects)


def test_local_services_roll_up_health_without_native_writes_or_public_events(monkeypatch):
    """
    Missing and stale services affect root readiness while suspension and deletion preserve their native owners.
    """
    monkeypatch.setenv("POLYAD_ROOT_DEPLOYMENT", "root")
    monkeypatch.setenv("POLYAD_SELF_GRAPH", "operators")
    targets = {
        "keda": [{"kind": "Deployment", "namespace": "scaling", "name": "keda"}],
        "endpoints": [{"kind": "Service", "namespace": "test", "name": "metrics"}],
        "postgresql": [{"kind": "Cluster", "namespace": "test", "name": "state"}],
        "collectors": [{"kind": "Service", "namespace": "test", "name": "telemetry"}],
    }
    monkeypatch.setenv("POLYAD_LOCAL_SERVICES", json.dumps(targets))

    async def run():
        keda = operator_deployment("keda")
        keda["metadata"].update(namespace="scaling", labels={})
        service = resource("Service", "metrics")
        database = resource("Cluster", "state", {"instances": 3})
        database["status"] = {"instances": 3, "readyInstances": 3, "conditions": [{"type": "Ready", "status": "True"}]}
        natives = [operator_deployment("root"), keda, service, database, resource("Service", "telemetry")]
        api = ManagementAPI(*natives)
        pools = manager(api, ManagementAPI())
        controller = Controller(api)
        await pools.topology()

        async def settle():
            for _ in range(10):
                for key, obj in list(api.objects.items()):
                    if key[0] in {"Graph", "PolyGraph"} and not obj["spec"].get("templateOnly"):
                        try:
                            await controller.reconcile(key)
                        except Pending:
                            pass

        await settle()
        poly = api.objects["PolyGraph", "test", "operators"]
        assert poly["status"]["ready"]
        root_group = (await api.owned("test", poly["metadata"]["uid"]))[0]
        assert {node["name"] for node in root_group["spec"]["nodes"]} == {"bootstrap", *targets}
        for graph in api.children("Graph"):
            assert not await public_observation(api, graph)
        keda_key = "Deployment", "scaling", "keda"
        api.objects[keda_key]["metadata"]["generation"] += 1
        await settle()
        assert not poly["status"]["ready"]
        del api.objects[keda_key]
        await settle()
        assert not poly["status"]["ready"]
        api.objects[keda_key] = copy.deepcopy(keda)
        await settle()
        assert poly["status"]["ready"]
        branch = next(obj for obj in await api.owned("test", root_group["metadata"]["uid"]) if RESOURCES in obj["metadata"]["annotations"])
        key = "Graph", "test", branch["metadata"]["name"]
        api.objects[key]["spec"]["suspend"] = True
        await controller.reconcile(key)
        assert api.objects[key]["status"]["phase"] == "Suspended"
        api.objects[key]["metadata"]["deletionTimestamp"] = "now"
        await controller.reconcile(key)
        for native in natives:
            assert not DeepDiff(native, api.objects[native["kind"], native["metadata"]["namespace"], native["metadata"]["name"]])
        assert all(kind in {"Graph", "PolyGraph", "Daemon"} for _, kind, _ in api.calls)

    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["secret", "public", "unbound"])
def test_observation_bindings_reject_secrets_and_mismatched_graphs(invalid):
    """
    Observation annotations cannot authorize arbitrary kinds or silently ignore unbound graph nodes.
    """
    target = {"kind": "Secret" if invalid == "secret" else "Service", "namespace": "test", "name": "service"}
    graph = resource("Graph", "services", {"mode": "persistent", "nodes": [{"name": "service", "kind": "Resource", "ref": "service"}]})
    graph["metadata"].update(
        labels={} if invalid == "public" else {INTERNAL: "true"}, annotations={RESOURCES: json.dumps({"service": target})}
    )
    if invalid == "unbound":
        graph["spec"]["nodes"][0]["ref"] = "another-service"
    with pytest.raises(ValueError):
        asyncio.run(members(ManagementAPI(), graph))


def test_postgresql_cluster_adapter_only_permits_reads():
    """
    Route Cluster observations to CloudNativePG while refusing mutations through that adapter path.
    """
    api = API.__new__(API)
    api.client = Mock()
    asyncio.run(api.get("Cluster", "test", "state"))
    assert api.client.call_api.call_args.args == ("/apis/postgresql.cnpg.io/v1/namespaces/test/clusters/state", "GET")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(ValueError, match="read-only"):
            asyncio.run(api.request(method, "Cluster", "test", "state"))
    assert api.client.call_api.call_count == 1
