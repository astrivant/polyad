"""
Exercise live operator membership, bootstrap safety and private hierarchy observations.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from unittest.mock import AsyncMock

import pytest
from deepdiff import DeepDiff

from polyad.events.visibility import INTERNAL, public_observation
from polyad.exceptions.reconciliation import Pending
from polyad.operator.clusters.federation import Federation
from polyad.operator.clusters.pools import REGISTERED
from polyad.operator.clusters.reserved import DEPLOYMENT
from polyad.operator.coordination.leases import Coordinator, root_shard
from polyad.operator.reconciliation.controller import Controller
from polyad_types.resources import GROUP
from tests.test_coordination import LeaseAPI
from tests.test_operator import resource, template
from tests.test_root_control_plane import ManagementAPI, manager


def operator_deployment(name):
    """
    Provide a ready native operator whose lifecycle belongs to Helm or a pool.
    """
    deployment = resource("Deployment", name, {"replicas": 2, "template": template(True)})
    deployment["metadata"]["labels"] = {INTERNAL: "true"}
    deployment["status"] = {"observedGeneration": 1, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}
    return deployment


def test_new_operator_groups_join_one_root_polygraph_without_replacing_the_root(monkeypatch, caplog):
    """
    Add Deployment and DaemonSet groups and roll their live observations into one root model.
    """
    monkeypatch.setenv("POLYAD_ROOT_DEPLOYMENT", "root")
    monkeypatch.setenv("POLYAD_SELF_GRAPH", "operator-tree")
    monkeypatch.setenv("POLYAD_CLUSTER_NAME", "management")

    async def run():
        root = operator_deployment("root")
        local, west, east = ManagementAPI(root), ManagementAPI(), ManagementAPI()
        pools = manager(local, west)
        pools.root.resolve = lambda cluster: ({"west": west, "east": east}[cluster], "test")
        federation = Federation(local)
        federation.name = "management"
        federation.target = pools.root.resolve
        pools.root.federation = federation
        controller = Controller(local)
        controller.federation = federation
        first = await pools.topology()
        assert [node["name"] for node in first["spec"]["nodes"]] == ["root"]

        async def settle():
            for _ in range(12):
                for api in (local, west, east):
                    selected = controller if api is local else Controller(api)
                    for key, obj in list(api.objects.items()):
                        if key[0] in {"Graph", "PolyGraph"} and not obj["spec"].get("templateOnly"):
                            try:
                                await selected.reconcile(key)
                            except Pending:
                                pass

        await settle()
        root_graph = next(obj for obj in local.children("Graph") if not obj["spec"].get("templateOnly"))
        root_uid = root_graph["metadata"]["uid"]
        assert root_graph["status"]["ready"]
        for cluster, kind in (("west", "Deployment"), ("east", "DaemonSet")):
            pool = resource("OperatorPool", cluster, {"cluster": cluster, "controller": kind, "replicas": 1})
            local.objects["OperatorPool", "test", cluster] = pool
            if kind == "Deployment":
                native = operator_deployment(f"polyad-worker-{pool['metadata']['uid'][:12]}")
                west.objects["Deployment", "test", native["metadata"]["name"]] = native
            await pools.graph_pool(copy.deepcopy(pool), template(True), f"owner-{cluster}")
            await settle()

        poly = local.children("PolyGraph")[0]
        assert len(local.children("PolyGraph")) == 1
        assert poly["metadata"]["uid"] == first["metadata"]["uid"]
        assert len(poly["spec"]["nodes"]) == 3
        assert {node.get("cluster", "management") for node in poly["spec"]["nodes"]} == {"management", "west", "east"}
        assert len(poly["spec"]["connections"]) == 4
        assert next(obj for obj in local.children("Graph") if not obj["spec"].get("templateOnly"))["metadata"]["uid"] == root_uid
        assert len(local.children("Deployment")) == len(west.children("Deployment")) == len(east.children("DaemonSet")) == 1
        assert poly["status"]["metrics"]["rollup"]["leafNodes"] == 3
        for api in (local, west, east):
            graph = next(obj for obj in api.children("Graph") if not obj["spec"].get("templateOnly"))
            assert not await public_observation(api, graph)
        assert not await public_observation(local, poly)
        assert not DeepDiff(root, local.children("Deployment")[0])
        before = len([record for record in caplog.records if getattr(record, "event_name", "") == "polyad.operator_topology.membership"])
        await pools.topology()
        assert (
            len([record for record in caplog.records if getattr(record, "event_name", "") == "polyad.operator_topology.membership"])
            == before
        )

    with caplog.at_level(logging.INFO, logger="polyad"):
        asyncio.run(run())
    members = [record for record in caplog.records if getattr(record, "event_name", "") == "polyad.operator_topology.membership"]
    assert len(members) == 3
    assert {record.polyad_attributes["polyad.target.cluster"] for record in members} == {"management", "west", "east"}


def test_root_observation_refreshes_readiness_and_never_mutates_its_deployment():
    """
    Rollout changes affect graph state while suspension and deletion leave bootstrap running.
    """

    async def run():
        root = operator_deployment("root")
        graph = resource("Graph", "root-group", {"mode": "persistent", "nodes": [{"name": "root", "kind": "Daemon", "ref": "root"}]})
        graph["metadata"].update(labels={INTERNAL: "true"}, annotations={DEPLOYMENT: "root"})
        definition = resource("Daemon", "root", {"template": template(True)})
        api = ManagementAPI(root, graph, definition)
        controller = Controller(api)
        key = "Graph", "test", "root-group"
        await controller.reconcile(key)
        assert api.objects[key]["status"]["ready"]
        assert api.objects[key]["status"]["metrics"]["resources"]["byKind"]["Deployment"] == 1
        api.objects["Deployment", "test", "root"]["metadata"]["generation"] = 2
        await controller.reconcile(key)
        assert not api.objects[key]["status"]["ready"]
        assert api.objects[key]["status"]["metrics"]["execution"]["readyNodes"] == 0
        api.objects[key]["spec"]["suspend"] = True
        await controller.reconcile(key)
        assert api.objects[key]["status"]["phase"] == "Suspended"
        api.objects[key]["metadata"]["deletionTimestamp"] = "now"
        await controller.reconcile(key)
        assert not any(kind == "Deployment" for _, kind, _ in api.calls)
        assert not api.objects["Deployment", "test", "root"]["metadata"].get("ownerReferences")

    asyncio.run(run())


def test_component_recovery_and_scaling_stay_inside_the_root_operator_graph(monkeypatch):
    """
    Reconcile the Helm component pipeline inside its operator group without adopting bootstrap.
    """
    from tests.test_chart import render

    monkeypatch.setenv("POLYAD_ROOT_DEPLOYMENT", "test-polyad")
    monkeypatch.setenv("POLYAD_SELF_GRAPH", "test-atlas")
    monkeypatch.setenv("POLYAD_COMPONENT_GRAPH", "test-control-plane")
    monkeypatch.setenv("POLYAD_CLUSTER_NAME", "management")
    rendered = render(
        "architecture.mode=Distributed",
        "api.enabled=true",
        "architecture.expandedNodes=9",
        "keda.observation.enabled=false",
        values_files=("root-values.yaml",),
    )
    deployment = next(obj for obj in rendered if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    environment = {entry["name"]: entry.get("value") for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    monkeypatch.setenv("POLYAD_LOCAL_SERVICES", environment["POLYAD_LOCAL_SERVICES"])
    definitions = []
    for obj in rendered:
        if obj["apiVersion"] == f"{GROUP}/v1alpha1" or obj["kind"] == "Service":
            definition = resource(obj["kind"], obj["metadata"]["name"], obj["spec"])
            definition["metadata"].update(obj["metadata"])
            definitions.append(definition)

    async def run():
        root = operator_deployment("test-polyad")
        api = ManagementAPI(root, *definitions)
        pools = manager(api, ManagementAPI())
        controller = Controller(api)
        boundary = await pools.topology()
        assert [node["name"] for node in boundary["spec"]["nodes"]] == ["root"]

        async def settle():
            for _ in range(12):
                for key, obj in list(api.objects.items()):
                    if key[0] in {"Graph", "PolyGraph", "ReplicaGroup"} and not obj["spec"].get("templateOnly"):
                        try:
                            await controller.reconcile(key)
                        except Pending:
                            pass
                for deployment in api.children("Deployment"):
                    count = deployment["spec"].get("replicas", 1)
                    deployment["status"] = {
                        "observedGeneration": deployment["metadata"]["generation"],
                        "replicas": count,
                        "updatedReplicas": count,
                        "readyReplicas": count,
                        "availableReplicas": count,
                    }

        await settle()
        root_group = (await api.owned("test", boundary["metadata"]["uid"]))[0]
        members = {obj["metadata"]["labels"][f"{GROUP}/node"]: obj for obj in await api.owned("test", root_group["metadata"]["uid"])}
        assert set(members) == {"bootstrap", "components", "endpoints"}
        assert members["bootstrap"]["metadata"]["annotations"][DEPLOYMENT] == "test-polyad"
        assert DEPLOYMENT not in root_group["metadata"]["annotations"]
        assert root_group["spec"]["connections"] == [
            {"source": "bootstrap", "target": "components"},
            {"source": "components", "target": "bootstrap"},
            {"source": "bootstrap", "target": "endpoints"},
            {"source": "endpoints", "target": "bootstrap"},
        ]
        assert members["components"]["status"]["structuralRules"][0]["measurements"]["cheeger"] == 1
        poly = api.objects["PolyGraph", "test", "test-atlas"]
        assert poly["status"]["ready"]
        assert poly["status"]["metrics"]["rollup"]["leafNodes"] == 9
        assert len(api.children("Deployment")) == 7
        for obj in (poly, root_group, *members.values()):
            assert not await public_observation(api, obj)

        executor = next(
            obj
            for obj in api.children("Deployment")
            if obj["spec"]["template"].get("metadata", {}).get("labels", {}).get(f"{GROUP}/component") == "executor"
        )
        lost = "Deployment", "test", executor["metadata"]["name"]
        del api.objects[lost]
        await settle()
        assert lost in api.objects
        assert len(api.children("Deployment")) == 7
        assert api.objects["PolyGraph", "test", "test-atlas"]["status"]["ready"]

        source = api.objects["ReplicaGroup", "test", "test-executor"]
        source["spec"]["replicas"] = 3
        source["metadata"]["generation"] += 1
        instance = next(obj for obj in api.children("ReplicaGroup") if obj["spec"].get("replicaSource", {}).get("name") == "test-executor")
        with pytest.raises(ValueError, match="expandedNodes"):
            await controller.reconcile(("ReplicaGroup", "test", instance["metadata"]["name"]))
        assert len(api.children("Deployment")) == 7
        assert not DeepDiff(root, api.objects["Deployment", "test", "test-polyad"])
        assert not any(kind == "Deployment" and name == "test-polyad" for _, kind, name in api.calls)

    asyncio.run(run())


def test_pool_removal_unlinks_only_its_graph_and_preserves_the_reserved_root(monkeypatch):
    """
    A deleting pool leaves the shared topology in place while its remote branch drains.
    """
    monkeypatch.setenv("POLYAD_ROOT_DEPLOYMENT", "root")

    async def run():
        local, remote = ManagementAPI(operator_deployment("root")), ManagementAPI()
        pools = manager(local, remote)
        for name in ("west", "east"):
            pool = resource("OperatorPool", name, {"cluster": name, "replicas": 1})
            pool["metadata"]["annotations"] = {REGISTERED: pool["metadata"]["uid"]}
            local.objects["OperatorPool", "test", name] = pool
        before = await pools.topology()
        branch = resource("Graph", "west-instance")
        branch["metadata"]["labels"] = {f"{GROUP}/node": "pool-uid-west"}
        pools.root.federation.children = AsyncMock(return_value=[branch])
        local.objects["OperatorPool", "test", "west"]["metadata"]["deletionTimestamp"] = "now"
        with pytest.raises(Pending, match="unlinked operator Graph"):
            await pools.pool(await local.get("OperatorPool", "test", "west"))
        after = local.children("PolyGraph")[0]
        assert after["metadata"]["uid"] == before["metadata"]["uid"]
        assert [node["name"] for node in after["spec"]["nodes"]] == ["root", "pool-uid-east"]
        assert len(after["spec"]["connections"]) == 2
        assert not any(method == "DELETE" for method, _, _ in local.calls)
        assert not remote.calls

        # A transiently unreachable peer cannot disappear from the model.
        pools.root.resolve = lambda cluster: (_ for _ in ()).throw(ConnectionError("offline"))
        unchanged = await pools.topology()
        assert not DeepDiff(after["spec"], unchanged["spec"])

    asyncio.run(run())


@pytest.mark.parametrize("component", ["dense", "bootstrap"])
def test_reserved_polygraph_shard_stays_with_root_planners(monkeypatch, component):
    """
    Remote executors cannot own the family required to rebuild their operator graphs.
    """
    monkeypatch.setenv("POLYAD_SELF_GRAPH", "operators")
    monkeypatch.setenv("POLYAD_SELF_GRAPH_KIND", "PolyGraph")

    async def run():
        api = LeaseAPI()
        monkeypatch.setenv("POLYAD_COMPONENT", component)
        root = Coordinator(api, "test", "root")
        await root.tick()
        monkeypatch.setenv("POLYAD_COMPONENT", "executor")
        worker = Coordinator(api, "test", "remote", planner=False)
        await worker.tick()
        await root.tick()
        assignments = json.loads(api.objects["Lease", "test", "polyad-leader"]["metadata"]["annotations"][f"{GROUP}/assignments"])
        assert assignments[str(root_shard("PolyGraph", "test", "operators"))] == "root"
        assert "remote" in assignments.values()

    asyncio.run(run())


def test_root_mode_defaults_match_pool_topology_without_helm_environment(monkeypatch):
    """
    Reserve the same family for bootstrap recovery when a root is configured outside Helm.
    """
    monkeypatch.setenv("POLYAD_ROOT_ENABLED", "true")
    monkeypatch.setenv("POLYAD_ROOT_DEPLOYMENT", "root")
    monkeypatch.delenv("POLYAD_SELF_GRAPH", raising=False)
    monkeypatch.delenv("POLYAD_SELF_GRAPH_KIND", raising=False)
    coordinator = Coordinator(LeaseAPI(), "test", "root")
    assert coordinator.self_graph_kind == "PolyGraph"
    assert coordinator.self_graph == manager(ManagementAPI(), ManagementAPI()).topology_name == "root-atlas"


def test_pool_status_refreshes_registration_but_never_acknowledges_newer_intent():
    """
    Metadata-only bootstrap updates permit status, while concurrent scale edits invalidate it.
    """

    async def run():
        pool = resource("OperatorPool", "west", {"cluster": "west", "replicas": 2})
        local = ManagementAPI(pool)
        pools = manager(local, ManagementAPI())
        live = local.objects["OperatorPool", "test", "west"]
        live["metadata"].update(resourceVersion="2", annotations={REGISTERED: live["metadata"]["uid"]})
        await pools.status(pool, phase="Pending", replicas=0)
        assert local.children("OperatorPool")[0]["status"]["phase"] == "Pending"
        live["metadata"]["generation"] += 1
        live["spec"]["replicas"] = 3
        await pools.status(pool, phase="Ready", replicas=2)
        assert local.children("OperatorPool")[0]["status"]["phase"] == "Pending"
        assert local.children("OperatorPool")[0]["status"]["observedGeneration"] == 1

    asyncio.run(run())
