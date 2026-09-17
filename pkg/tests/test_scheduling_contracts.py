"""
Exercise storage, inherited placement and delay admission across graph instances.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from polyad.compiler.passes.storage import configure_storage
from polyad.graph import DelayGate
from polyad.operator.reconciliation.controller import Controller
from polyad.operator.reconciliation.placement import merge_placement, place_pod
from polyad_types.resources import GROUP
from tests.test_composition import settle
from tests.test_operator import FakeAPI, resource, template


def test_placement_defaults_can_be_overridden_but_enforcement_is_sticky():
    """
    Allow explicit defaults to change while preserving enforced selectors and tolerations.
    """
    required = {"nodeSelector": {"pool": "cpu"}, "tolerations": [{"key": "dedicated", "operator": "Exists"}]}
    own = {"nodeSelector": {"pool": "gpu"}}
    assert merge_placement({**required, "enforce": False}, own) == own
    with pytest.raises(ValueError, match="conflicting placement"):
        merge_placement(required, {**own, "enforce": False})
    inherited = merge_placement(required, {"enforce": False, "nodeSelector": {"zone": "west"}})
    pod = {"nodeSelector": {"pool": "cpu"}, "affinity": {"podAntiAffinity": {"preferred": []}}}
    place_pod(pod, inherited)
    assert pod["nodeSelector"] == {"pool": "cpu", "zone": "west"}
    assert pod["tolerations"] == required["tolerations"]
    assert "podAntiAffinity" in pod["affinity"]
    with pytest.raises(ValueError, match="nodeName"):
        place_pod({"nodeName": "bypass"}, inherited)
    empty = {}
    place_pod(empty, {**required, "enforce": False})
    assert empty["nodeSelector"] == {"pool": "cpu"}


@pytest.mark.parametrize("enforce", [True, False])
def test_workload_placement_override_reaches_native_pod(enforce):
    """
    Reject conflicting explicit workload placement only when the graph enforces it.
    """

    async def scenario():
        api = FakeAPI(
            resource(
                "Graph",
                "root",
                {
                    "placement": {"enforce": enforce, "nodeSelector": {"pool": "cpu"}},
                    "nodes": [{"name": "worker", "kind": "Workload", "ref": "worker"}],
                },
            ),
            resource("Workload", "worker", {"template": template(), "placement": {"nodeSelector": {"pool": "gpu"}}}),
        )
        if enforce:
            with pytest.raises(ValueError, match="conflicting placement"):
                await settle(api)
            assert not api.children("Job")
        else:
            await settle(api)
            assert api.children("Job")[0]["spec"]["template"]["spec"]["nodeSelector"] == {"pool": "gpu"}

    asyncio.run(scenario())


def test_persistence_uses_named_class_claim_and_preserves_external_storage():
    """
    Check class identity before creating a Job and leave external PVCs outside graph cleanup.
    """

    async def scenario():
        api = FakeAPI(
            resource("Graph", "root", {"nodes": [{"name": "worker", "kind": "Workload", "ref": "worker"}]}),
            resource(
                "Workload",
                "worker",
                {
                    "template": template(),
                    "persistence": {"enabled": True, "storageClass": "durable", "claimName": "data", "mountPath": "/data"},
                },
            ),
            resource("PersistentVolumeClaim", "data", {"storageClassName": "wrong"}),
        )
        with pytest.raises(ValueError, match="does not match"):
            await settle(api)
        assert not api.children("Job")
        claim = api.objects[("PersistentVolumeClaim", "test", "data")]
        claim["spec"]["storageClassName"] = "durable"
        # Pending claims are allowed: WaitForFirstConsumer needs a pod before binding.
        await settle(api)
        pod = api.children("Job")[0]["spec"]["template"]["spec"]
        assert pod["volumes"] == [{"name": "polyad-persistence", "persistentVolumeClaim": {"claimName": "data"}}]
        assert pod["containers"][0]["volumeMounts"] == [{"name": "polyad-persistence", "mountPath": "/data"}]
        definition = api.objects[("Workload", "test", "worker")]
        definition["spec"]["persistence"]["storageClass"] = "changed"
        with pytest.raises(ValueError, match="does not match"):
            await Controller(api).reconcile(("Graph", "test", "root"))
        definition["spec"]["persistence"]["storageClass"] = "durable"
        root = api.objects[("Graph", "test", "root")]
        root["spec"]["suspend"] = True
        await Controller(api).reconcile(("Graph", "test", "root"))
        assert "deletionTimestamp" not in claim["metadata"]

    asyncio.run(scenario())


@pytest.mark.parametrize("persistence", [{"enabled": True}, {"enabled": True, "claimName": "data"}])
def test_persistence_requires_storage_class(persistence):
    """
    Reject implicit default StorageClasses when persistence is enabled.
    """
    with pytest.raises(ValueError, match="requires storageClass"):
        configure_storage({"template": template(), "persistence": persistence})


@pytest.mark.parametrize(
    "configuration",
    [
        {"persistence": {"enabled": True, "storageClass": "durable", "claimName": "data"}},
        {"persistence": {"storageClass": "durable"}},
        {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]},
        {"volumes": [{"name": "data", "ephemeral": {"volumeClaimTemplate": {"spec": {"storageClassName": "disk"}}}}]},
    ],
)
def test_workload_storage_configuration_is_preserved(configuration):
    """
    Preserve user-selected claims and native volume templates on ordinary workloads.
    """
    spec = {"template": template()}
    if "volumes" in configuration:
        spec["template"]["spec"].update(configuration)
    else:
        spec.update(configuration)
    persistence = configure_storage(spec)
    if "volumes" in configuration:
        assert spec["template"]["spec"]["volumes"] == configuration["volumes"]
    elif persistence.enabled:
        assert spec["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "data"
    else:
        assert persistence.storageClass == "durable"


def test_spot_placement_leaves_storage_policy_to_users():
    """
    Allow user-chosen persistent storage on workloads beneath spot-placed graph boundaries.
    """

    async def scenario():
        api = FakeAPI(
            resource(
                "Graph",
                "root",
                {"placement": {"nodeSelector": {"capacity": "spot"}}, "nodes": [{"name": "group", "kind": "PolyGraph", "ref": "group"}]},
            ),
            resource("PolyGraph", "group", {"templateOnly": True, "nodes": [{"name": "loop", "kind": "Graph", "ref": "loop"}]}),
            resource(
                "Graph",
                "loop",
                {"templateOnly": True, "nodes": [{"name": "worker", "kind": "Workload", "ref": "worker"}]},
            ),
            resource(
                "Workload",
                "worker",
                {"template": template(), "persistence": {"enabled": True, "storageClass": "durable", "claimName": "data"}},
            ),
        )
        api.objects[("PersistentVolumeClaim", "test", "data")] = resource("PersistentVolumeClaim", "data", {"storageClassName": "durable"})
        await settle(api)
        pod = api.children("Job")[0]["spec"]["template"]["spec"]
        assert pod["nodeSelector"] == {"capacity": "spot"}
        assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "data"

    asyncio.run(scenario())


def test_delay_is_persisted_and_recovered_without_blocking_other_nodes():
    """
    Start a timer after dependencies complete and retain it across replica replacement.
    """

    async def scenario():
        root_key = "Graph", "test", "root"
        api = FakeAPI(
            resource(
                "Graph",
                "root",
                {
                    "nodes": [
                        {"name": "first", "kind": "Workload", "ref": "worker"},
                        {"name": "next", "kind": "Workload", "ref": "worker", "gate": "cooldown", "requires": [{"node": "first"}]},
                        {"name": "independent", "kind": "Workload", "ref": "worker"},
                    ]
                },
            ),
            resource("Workload", "worker", {"template": template()}),
            resource("Gate", "cooldown", {"delaySeconds": 10}),
        )
        start = datetime(2026, 9, 15, tzinfo=UTC)
        with patch("polyad.operator.reconciliation.controller.datetime", wraps=datetime) as clock:
            clock.now.return_value = start
            await settle(api)
            assert len(api.children("Job")) == 2
            assert not api.objects[root_key]["status"]["delays"]
            first = next(o for o in api.children("Job") if o["metadata"]["labels"][f"{GROUP}/node"] == "first")
            first["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
            await Controller(api).reconcile(root_key)
            deadline = api.objects[root_key]["status"]["delays"]["next"]["notBefore"]
            assert deadline == (start + timedelta(seconds=10)).isoformat()
            clock.now.return_value = start + timedelta(seconds=9)
            await Controller(api).reconcile(root_key)
            assert len(api.children("Job")) == 2
            assert api.objects[root_key]["status"]["delays"]["next"]["notBefore"] == deadline
            # Recreating a dependency resets the timer even with the same graph generation.
            first["metadata"]["uid"] = "replacement-first"
            await Controller(api).reconcile(root_key)
            assert api.objects[root_key]["status"]["delays"]["next"]["notBefore"] == (start + timedelta(seconds=19)).isoformat()
            clock.now.return_value = start + timedelta(seconds=10)
            await Controller(api).reconcile(root_key)
            assert len(api.children("Job")) == 2
            clock.now.return_value = start + timedelta(seconds=19)
            await Controller(api).reconcile(root_key)
            assert len(api.children("Job")) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("seconds", [-1, float("inf"), float("nan"), True, 315360001])
def test_delay_rejects_invalid_durations(seconds):
    """
    Reject invalid deadlines before scheduling or serializing them.
    """
    with pytest.raises(ValueError, match="delay seconds"):
        DelayGate(seconds)
