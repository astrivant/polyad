"""
Exercise stateful execution, storage passthrough and controller readiness through composition.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest
import yaml
from jsonschema import validate

from polyad.api.store import CompositionStore
from polyad.compiler.passes.composition import receipt_spec, request_name
from polyad.compiler.passes.daemon import compile_daemon, execution_pod
from polyad.operator.controller import Controller, Pending
from polyad.operator.graph_status import observed
from polyad_types import resources as asts
from tests.test_capacity import passes, scenario
from tests.test_composition import settle
from tests.test_composition_api import request_value
from tests.test_operator import FakeAPI, resource, template


def stateful_spec():
    """
    Describe independent durable replicas plus native transient Pod storage.
    """
    pod = template(True)
    pod["metadata"] = {"labels": {"app": "database"}}
    pod["spec"]["volumes"] = [{"name": "scratch", "emptyDir": {"sizeLimit": "1Gi"}}]
    pod["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "data", "mountPath": "/data"},
        {"name": "scratch", "mountPath": "/scratch"},
    ]
    return {
        "controller": "StatefulSet",
        "replicas": 2,
        "statefulSet": {
            "serviceName": "database",
            "podManagementPolicy": "Parallel",
            "minReadySeconds": 5,
            "revisionHistoryLimit": 3,
            "ordinals": {"start": 2},
            "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"partition": 1, "maxUnavailable": "25%"}},
            "persistentVolumeClaimRetentionPolicy": {"whenDeleted": "Retain", "whenScaled": "Delete"},
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "data", "labels": {"tier": "durable"}, "annotations": {"application": "database"}},
                    "spec": {
                        "storageClassName": "durable",
                        "accessModes": ["ReadWriteOnce"],
                        "resources": {"requests": {"storage": "10Gi"}},
                        "volumeMode": "Filesystem",
                        "dataSource": {"apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": "seed"},
                    },
                }
            ],
        },
        "template": pod,
    }


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
@pytest.mark.parametrize("enabled,opt_in", [(False, False), (False, True), (True, False), (True, True)])
def test_daemon_secret_reload_is_opt_in_and_preserves_reloader_updates(monkeypatch, kind, enabled, opt_in):
    """
    Emit restart controls on opted-in native controllers and retain externally triggered rollouts.
    """
    monkeypatch.setenv("POLYAD_ESO_RELOAD_ENABLED", str(enabled).lower())

    async def run():
        definition = {"controller": kind, "reloadOnSecretChange": opt_in, "template": template(True)}
        definition["template"]["spec"]["containers"][0]["envFrom"] = [{"secretRef": {"name": "service-credentials"}}]
        if kind == "StatefulSet":
            definition["statefulSet"] = {"serviceName": "server-peers"}
        graph = resource(
            "Graph",
            "application",
            {
                "mode": "persistent",
                "nodes": [{"name": "server", "kind": "Daemon", "ref": "server"}, {"name": "job", "kind": "Workload", "ref": "job"}],
            },
        )
        api = FakeAPI(graph, resource("Daemon", "server", definition), resource("Workload", "job", {"template": template()}))
        controller = Controller(api)
        key = "Graph", "test", "application"
        await controller.reconcile(key)
        child = api.children(kind)[0]
        expected = "true" if enabled and opt_in else None
        assert child["metadata"]["annotations"].get("reloader.stakater.com/search") == expected
        assert "reloader.stakater.com/search" not in child["spec"]["template"]["metadata"].get("annotations", {})
        assert "reloader.stakater.com/search" not in api.children("Job")[0]["metadata"]["annotations"]
        # Reloader changes the native Pod template; Polyad retains the owned controller.
        child["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "STAKATER_SECRET_HASH", "value": "changed"})
        api.calls.clear()
        await controller.reconcile(key)
        assert not any(call[0] in {"POST", "DELETE"} and call[1] == kind for call in api.calls)
        assert api.children(kind)[0] is child

    asyncio.run(run())


def test_daemon_rejects_nonboolean_secret_reload():
    """
    Validate the opt-in for definitions received outside Kubernetes schema validation.
    """
    with pytest.raises(ValueError, match="reloadOnSecretChange"):
        compile_daemon({"template": template(True), "reloadOnSecretChange": "false"}, {})


def test_composition_carries_stateful_storage_identity_and_audit():
    """
    Resolve a governing Service and preserve both shared and per-replica PVC configuration.
    """

    async def run():
        definition = stateful_spec()
        definition["statefulSet"]["serviceName"] = "${nodes.discovery.name}"
        definition["persistence"] = {"enabled": True, "storageClass": "durable", "claimName": "shared", "mountPath": "/shared"}
        original = copy.deepcopy(definition)
        request = request_value(
            {
                "requestId": "stored-service",
                "rootId": "root",
                "objects": [
                    {"id": "daemon", "kind": "Daemon", "spec": definition},
                    {"id": "reader", "kind": "Workload", "spec": {"template": template()}},
                    {
                        "id": "service",
                        "kind": "Resource",
                        "spec": {
                            "manifest": {
                                "apiVersion": "v1",
                                "kind": "Service",
                                "spec": {"clusterIP": "None", "selector": {"app": "database"}, "ports": [{"port": 8000}]},
                            }
                        },
                    },
                    {
                        "id": "root",
                        "kind": "Graph",
                        "spec": {
                            "mode": "persistent",
                            "nodes": [
                                {"id": "discovery", "refId": "service"},
                                {"id": "database", "refId": "daemon", "requires": [{"nodeId": "discovery", "condition": "ready"}]},
                                {"id": "consumer", "refId": "reader", "requires": [{"nodeId": "database", "condition": "ready"}]},
                            ],
                        },
                    },
                ],
            }
        )
        receipt = resource("Composition", request_name(request.requestId), receipt_spec(request))
        api = FakeAPI(receipt, resource("PersistentVolumeClaim", "shared", {"storageClassName": "durable"}))
        controller = Controller(api)
        key = "Composition", "test", receipt["metadata"]["name"]
        with pytest.raises(Pending):
            await controller.reconcile(key)
        await controller.reconcile(key)
        await settle(api)
        assert not api.children("Deployment") and not api.children("Job")
        (stateful,) = api.children("StatefulSet")
        spec = stateful["spec"]
        assert spec["serviceName"] == api.children("Service")[0]["metadata"]["name"]
        for name, value in original["statefulSet"].items():
            if name != "serviceName":
                assert spec[name] == value
        assert spec["replicas"] == 2
        pod = spec["template"]
        assert pod["spec"]["volumes"] == [
            *original["template"]["spec"]["volumes"],
            {"name": "polyad-persistence", "persistentVolumeClaim": {"claimName": "shared"}},
        ]
        assert pod["spec"]["containers"][0]["volumeMounts"] == [
            *original["template"]["spec"]["containers"][0]["volumeMounts"],
            {"name": "polyad-persistence", "mountPath": "/shared"},
        ]
        env = {entry["name"]: entry.get("value") for entry in pod["spec"]["containers"][0]["env"]}
        assert env["POLYAD_RESOURCE_KIND"] == "StatefulSet"
        assert env["POLYAD_REQUEST_ID"] == "stored-service"
        assert pod["metadata"]["annotations"][f"{asts.GROUP}/node-path"] == "root/database"
        stateful["status"] = {"observedGeneration": 1, "replicas": 2, "updatedReplicas": 1, "readyReplicas": 2, "availableReplicas": 2}
        await settle(api)
        assert len(api.children("Job")) == 1
        root = api.children("Graph")[0]
        assert root["status"]["metrics"]["resources"]["byKind"]["StatefulSet"] == 1
        audit = await CompositionStore(api, "test").lookup(request.requestId, True)
        assert len([item for item in audit["resources"] if item["kind"] == "StatefulSet"]) == 1
        assert definition == original
        root["spec"]["suspend"] = True
        await settle(api, 1)
        assert stateful["metadata"]["deletionTimestamp"]
        assert "deletionTimestamp" not in api.children("PersistentVolumeClaim")[0]["metadata"]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("options", "status", "ready"),
    [
        ({}, {}, True),
        ({}, {"observedGeneration": 1}, False),
        ({}, {"replicas": 4}, False),
        ({}, {"readyReplicas": 2}, False),
        ({}, {"updatedReplicas": 2}, False),
        ({"minReadySeconds": 10}, {"availableReplicas": 2}, False),
        ({"minReadySeconds": 10}, {"availableReplicas": 3}, True),
        ({"updateStrategy": {"type": "OnDelete"}}, {"updatedReplicas": 0}, True),
        ({"updateStrategy": {"rollingUpdate": {"partition": 1}}}, {"updatedReplicas": 2}, True),
        ({"updateStrategy": {"rollingUpdate": {"partition": 1}}}, {"updatedReplicas": 1}, False),
        ({"updateStrategy": {"rollingUpdate": {"partition": 1}}, "ordinals": {"start": 5}}, {"updatedReplicas": 2}, True),
        ({"updateStrategy": {"rollingUpdate": {"partition": 4}}}, {"updatedReplicas": 0}, True),
    ],
)
def test_stateful_readiness_observes_generation_and_rollout(options, status, ready):
    """
    Require current availability while respecting deliberate partitions and manual updates.
    """
    obj = resource("StatefulSet", "database", {"replicas": 3, **options})
    obj["metadata"]["generation"] = 2
    obj["status"] = {"observedGeneration": 2, "replicas": 3, "readyReplicas": 3, "updatedReplicas": 3, **status}
    assert observed(obj)["ready"] is ready
    assert not observed(obj)["completed"]
    obj["metadata"]["deletionTimestamp"] = "now"
    assert not observed(obj)["ready"]


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({}, "serviceName"),
        ({"serviceName": "Not.Valid"}, "serviceName"),
        ({"serviceName": "database", "replicas": 10}, "managed by Polyad"),
        ({"serviceName": "database", "podManagementPolicy": "Random"}, "podManagementPolicy"),
        ({"serviceName": "database", "ordinals": {"start": -1}}, "ordinals.start"),
        ({"serviceName": "database", "updateStrategy": {"type": "OnDelete", "rollingUpdate": {"partition": 1}}}, "OnDelete"),
        ({"serviceName": "database", "persistentVolumeClaimRetentionPolicy": {"whenDeleted": "Unknown"}}, "retention"),
    ],
)
def test_stateful_options_reject_invalid_identity_and_rollout(options, error):
    """
    Fail before native resource creation when stateful options are inconsistent.
    """
    with pytest.raises(ValueError, match=error):
        compile_daemon({"template": template(True), "controller": "StatefulSet", "statefulSet": options}, {})


def test_storage_validation_and_admission_pod_preserve_definition():
    """
    Materialize implicit volumes only for admission and reject ambiguous claims.
    """
    spec = stateful_spec()
    original = copy.deepcopy(spec)
    native = {"kind": "StatefulSet", "metadata": {"name": "database"}, "spec": asts.to_document(compile_daemon(spec, {}))}
    pod = execution_pod(native)
    assert pod["spec"]["volumes"][-1] == {"name": "data", "persistentVolumeClaim": {"claimName": "data-database-2"}}
    assert native["spec"]["template"] == original["template"]
    assert spec == original
    spec["statefulSet"]["volumeClaimTemplates"].append(copy.deepcopy(spec["statefulSet"]["volumeClaimTemplates"][0]))
    with pytest.raises(ValueError, match="unique"):
        compile_daemon(spec, {})
    spec = stateful_spec()
    spec["template"]["spec"]["volumes"].append({"name": "data", "emptyDir": {}})
    with pytest.raises(ValueError, match="shadow"):
        compile_daemon(spec, {})
    spec["controller"] = "Deployment"
    with pytest.raises(ValueError, match="require controller"):
        compile_daemon(spec, {})
    spec["controller"] = "Invalid"
    with pytest.raises(ValueError, match="Deployment, StatefulSet or DaemonSet"):
        compile_daemon(spec, {})


@pytest.mark.parametrize("claims", [True, False])
def test_capacity_accounts_for_stateful_replicas_or_rejects_per_ordinal_claims(monkeypatch, claims):
    """
    Never underforecast Pod demand or reuse one ordinal's PVC for every reservation.
    """
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", "true")

    async def run():
        api, controller, key = scenario("ProvisioningRequest", nodes=[{"name": "database", "kind": "Daemon", "ref": "database"}])
        api.objects[key]["spec"]["mode"] = "persistent"
        spec = stateful_spec()
        if not claims:
            spec["statefulSet"].pop("volumeClaimTemplates")
            spec["template"]["spec"]["containers"][0].pop("volumeMounts")
        spec["template"]["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "1", "memory": "1Gi"}}
        api.objects[("Daemon", "test", "database")] = resource("Daemon", "database", spec)
        if claims:
            with pytest.raises(ValueError, match="per-replica StatefulSet"):
                await passes(controller, key)
            assert not api.children("ProvisioningRequest") and not api.children("StatefulSet")
        else:
            await passes(controller, key, 3)
            (request,) = api.children("ProvisioningRequest")
            assert request["spec"]["podSets"][0]["count"] == 2
            request["status"] = {"conditions": [{"type": "Provisioned", "status": "True", "observedGeneration": 1}]}
            await passes(controller, key)
            assert len(api.children("StatefulSet")) == 1

    asyncio.run(run())


def test_daemon_schema_accepts_stateful_storage():
    """
    Keep native PVC options and controller selection expressible through CRD admission.
    """
    crd = yaml.safe_load(Path("charts/polyad/crds/daemons.yaml").read_text())
    schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    validate(stateful_spec(), schema)
    validate({"template": template(True)}, schema)
    assert schema["properties"]["statefulSet"]["properties"]["volumeClaimTemplates"]["items"]["properties"]["spec"][
        "x-kubernetes-preserve-unknown-fields"
    ]
