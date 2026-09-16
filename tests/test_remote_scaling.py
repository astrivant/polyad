"""
Keep remote scaling subordinate to destination approval and fresh local admission.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from kubernetes.client.exceptions import ApiException

from polyad.metrics.inventory import inventory
from polyad.operator.controller import Pending
from polyad.operator.remote_scaling import INTENT, approved_intent, remote_revision
from polyad.operator.replication import effective_spec
from polyad.operator.rule_state import check_live_rules
from polyad_types.replication import replica_topology
from tests.test_operator import resource, template
from tests.test_replication import group, turn
from tests.test_root_control_plane import ManagementAPI, manager, scale_request


def test_local_edit_wins_and_remote_request_cannot_rebase():
    """
    A local spec revision revokes an existing remote intent, including subsequent KEDA updates.
    """

    async def scenario():
        target = group(1)
        intent = scale_request(target, 3)
        root, remote = ManagementAPI(intent), ManagementAPI(target)
        pools = manager(root, remote)
        await pools.scale(intent)
        live = remote.objects[("ReplicaGroup", "test", "copies")]
        assert (await effective_spec(remote, live))[0]["replicas"] == 3
        assert live["spec"]["replicas"] == 1
        live["spec"]["replicas"] = 2
        live["metadata"]["generation"] += 1
        assert (await effective_spec(remote, live))[0]["replicas"] == 2
        intent["spec"]["replicas"] = 4
        with pytest.raises(ValueError, match="local ReplicaGroup edits"):
            await pools.scale(intent)
        assert live["spec"]["replicas"] == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("identity", ["root", "namespace", "name", "uid"])
def test_competing_requests_are_distinguished_by_complete_identity(identity):
    """
    Only the locally selected request wins; an unapproved duplicate cannot deny it service.
    """

    async def scenario():
        target = group(1)
        approved = scale_request(target, 2)
        competitor = copy.deepcopy(approved)
        competitor["metadata"].update(name="competitor", uid="competing-request")
        root, remote = ManagementAPI(approved, competitor), ManagementAPI(target)
        pools = manager(root, remote)
        await pools.scale(approved)
        accepted = copy.deepcopy(remote.objects[("ReplicaGroup", "test", "copies")])
        candidate = copy.deepcopy(approved)
        if identity == "root":
            pools.root.federation.name = "other-root"
        else:
            candidate["metadata"][identity] = "other"
        with pytest.raises(ValueError, match="has not approved"):
            await pools.scale(candidate)
        assert remote.objects[("ReplicaGroup", "test", "copies")] == accepted

    asyncio.run(scenario())


def test_new_intent_requires_new_local_admission_and_metric_observation():
    """
    Metadata-only intent changes cannot reuse the prior request's successful status.
    """

    async def scenario():
        target = group(1)
        intent = scale_request(target, 2)
        root = ManagementAPI(intent)
        remote = ManagementAPI(target, resource("Daemon", "worker", {"template": template(True)}))
        pools = manager(root, remote)
        await pools.scale(intent)
        await turn(remote)
        await turn(remote)
        live = remote.objects[("ReplicaGroup", "test", "copies")]
        assert live["status"]["remoteScaleRevision"] == remote_revision(live)
        assert live["status"]["observedRemoteScaleIntent"] == live["metadata"]["annotations"][INTENT]
        assert live["status"]["metrics"]["topology"]["nodeCount"] == 2
        assert live["spec"]["replicas"] == 1
        await pools.scale(await root.get("RemoteScale", "test", "consumers"))
        assert root.children("RemoteScale")[0]["status"]["phase"] == "Ready"
        old = copy.deepcopy(live)
        request = root.objects[("RemoteScale", "test", "consumers")]
        request["spec"]["replicas"] = 3
        request["metadata"]["generation"] += 1
        await pools.scale(copy.deepcopy(request))
        assert live["metadata"]["generation"] == old["metadata"]["generation"]
        assert live["status"]["observedRemoteScaleIntent"] != live["metadata"]["annotations"][INTENT]
        assert not next(record for record in inventory(list(remote.objects.values()))["objects"] if record["kind"] == "ReplicaGroup")[
            "scaling"
        ]["current"]
        await pools.scale(await root.get("RemoteScale", "test", "consumers"))
        assert root.children("RemoteScale")[0]["status"]["phase"] == "Pending"
        old["spec"] = replica_topology((await effective_spec(remote, old))[0])
        with pytest.raises(Pending, match="remote scale intent changed"):
            await check_live_rules(remote, old)
        await turn(remote)
        assert len(remote.children("Deployment")) == 3
        assert live["status"]["metrics"]["topology"]["nodeCount"] == 3
        assert live["status"]["observedRemoteScaleIntent"] == live["metadata"]["annotations"][INTENT]

    asyncio.run(scenario())


def test_local_edit_racing_with_remote_submission_is_fenced():
    """
    The remote writer cannot submit against a destination changed after its initial read.
    """

    class RacingAPI(ManagementAPI):
        async def request(self, method, kind, namespace, name="", body=None, **kwargs):
            if method == "PATCH" and INTENT in body.get("metadata", {}).get("annotations", {}):
                live = self.objects[(kind, namespace, name)]
                live["metadata"].update(generation=2, resourceVersion="2")
                live["spec"]["replicas"] = 4
            return await super().request(method, kind, namespace, name, body, **kwargs)

    async def scenario():
        target = group(1)
        intent = scale_request(target, 2)
        root, remote = ManagementAPI(intent), RacingAPI(target)
        with pytest.raises(ApiException) as error:
            await manager(root, remote).scale(intent)
        assert error.value.status == 409
        live = await remote.get("ReplicaGroup", "test", "copies")
        assert not approved_intent(live)
        assert (await effective_spec(remote, live))[0]["replicas"] == 4

    asyncio.run(scenario())


def test_remote_intent_follows_reusable_groups_without_overriding_local_instances():
    """
    Approved reusable intent propagates through inheritance and remains locally bounded.
    """

    async def scenario():
        source = group(1, templateOnly=True)
        intent = scale_request(source, 3)
        child = group(1, replicaSource={"name": "copies", "uid": source["metadata"]["uid"]})
        child["metadata"].update(name="instance", uid="instance-uid")
        root, remote = ManagementAPI(intent), ManagementAPI(source, child)
        await manager(root, remote).scale(intent)
        assert (await effective_spec(remote, child))[0]["replicas"] == 3
        child["spec"]["inheritReplicas"] = False
        assert (await effective_spec(remote, child))[0]["replicas"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["{", "[]", "null", '{"replicas": true}'])
def test_malformed_remote_intent_retains_local_control(value):
    """
    Untrusted or incomplete annotations cannot inject a desired count.
    """
    target = group(1)
    scale_request(target, 2)
    target["metadata"]["annotations"] = {INTENT: value}
    assert approved_intent(target) is None


def test_revoking_approval_invalidates_an_intent_even_without_generation_update():
    """
    Explicit owner mismatch is independently sufficient to reject a stale intent.
    """
    target = group(1)
    intent = scale_request(target, 2)
    target["metadata"]["annotations"] = {
        INTENT: json.dumps(
            {"owner": target["spec"]["remoteScaling"], "targetUid": intent["spec"]["target"]["uid"], "targetGeneration": 1, "replicas": 2}
        )
    }
    assert approved_intent(target)
    del target["spec"]["remoteScaling"]
    assert not approved_intent(target)


def test_reserved_ancestor_cannot_be_scaled_even_with_an_explicit_grant():
    """
    The guard follows ownership instead of checking only the target's labels.
    """

    async def scenario():
        parent = resource("Graph", "operator")
        parent["metadata"]["labels"] = {"polyad.astrivant.com/internal": "true"}
        target = group(1)
        target["metadata"]["ownerReferences"] = [
            {"apiVersion": parent["apiVersion"], "kind": "Graph", "name": "operator", "uid": parent["metadata"]["uid"], "controller": True}
        ]
        intent = scale_request(target, 2)
        root, remote = ManagementAPI(intent), ManagementAPI(target, parent)
        with pytest.raises(ValueError, match="reserved operator graphs"):
            await manager(root, remote).scale(intent)
        assert not remote.calls

    asyncio.run(scenario())


def test_reusable_remote_scale_still_checks_ancestor_rules():
    """
    Each generated instance admits remote intent against fresh containing graph limits.
    """
    from tests.test_replication import policy_family, start_family

    async def scenario():
        initial = policy_family(bound=8)
        source = initial.objects[("ReplicaGroup", "test", "copies")]
        intent = scale_request(source, 2)
        root, remote = ManagementAPI(intent), ManagementAPI(*initial.objects.values())
        (instance,) = await start_family(remote)
        await manager(root, remote).scale(intent)
        await turn(remote, instance["metadata"]["name"])
        assert instance["status"]["scaleCurrent"]
        assert instance["status"]["desiredReplicas"] == 2
        assert instance["status"]["sourceRemoteScaleRevision"]
        await turn(remote)
        assert remote.objects[("ReplicaGroup", "test", "copies")]["status"]["scaleCurrent"]
        request = root.objects[("RemoteScale", "test", "consumers")]
        request["spec"]["replicas"] = 20
        request["metadata"]["generation"] += 1
        await manager(root, remote).scale(copy.deepcopy(request))
        before = len(remote.children("Deployment"))
        with pytest.raises(ValueError):
            await turn(remote, instance["metadata"]["name"])
        assert len(remote.children("Deployment")) == before
        assert not instance["status"]["scaleCurrent"]

    asyncio.run(scenario())
