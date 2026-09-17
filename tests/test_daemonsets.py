"""
Verify node-based Daemon execution and private remote operator graph ownership.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from polyad.compiler.passes.daemon import compile_daemon
from polyad.operator.clusters.federation import Federation
from polyad.operator.reconciliation.controller import Controller, Pending, observed
from polyad_types.resources import to_document
from tests.test_operator import FakeAPI, resource, template
from tests.test_root_control_plane import ManagementAPI, manager


def test_daemonset_is_owned_by_graph_and_readiness_follows_nodes():
    """
    Compile one native DaemonSet and observe its rollout without inventing a replica field.
    """

    async def run():
        graph = resource(
            "Graph",
            "nodes",
            {
                "mode": "persistent",
                "nodes": [{"name": "workers", "kind": "Daemon", "ref": "worker"}],
            },
        )
        daemon = resource("Daemon", "worker", {"controller": "DaemonSet", "template": template(True)})
        api = FakeAPI(graph, daemon)
        await Controller(api).reconcile(("Graph", "test", "nodes"))
        native = api.children("DaemonSet")[0]
        assert "replicas" not in native["spec"]
        assert not observed(native)["ready"]
        native["status"] = {
            "observedGeneration": 1,
            "desiredNumberScheduled": 3,
            "updatedNumberScheduled": 3,
            "numberReady": 3,
            "numberAvailable": 3,
        }
        assert observed(native)["ready"]
        native["status"]["desiredNumberScheduled"] = 4
        assert not observed(native)["ready"]
        await Controller(api).reconcile(("Graph", "test", "nodes"))
        values = api.children("Graph")[0]["status"]["workloads"]["workers"]["values"]
        assert values["replicas"] == 4 and values["readyReplicas"] == 3

    asyncio.run(run())


def test_daemonset_rejects_replica_scaling_and_stateful_options():
    """
    Node eligibility cannot be misrepresented as a desired native Pod count.
    """
    spec = {"controller": "DaemonSet", "template": template(True)}
    assert "replicas" not in to_document(compile_daemon(spec, {"app": "test"}))
    for additional in ({"replicas": 2}, {"statefulSet": {}}, {"activation": {"mode": "Pulse"}}):
        with pytest.raises(ValueError, match="node eligibility"):
            compile_daemon({**spec, **additional}, {"app": "test"})


def test_reserved_root_polygraph_places_remote_graph_and_daemonset(monkeypatch):
    """
    Root bootstrap creates definitions; ordinary graph controllers exclusively create worker execution.
    """
    monkeypatch.setenv("POLYAD_CLUSTER_NAME", "management")
    monkeypatch.setenv("POLYAD_ROOT_DEPLOYMENT", "root")

    async def run():
        pool = resource("OperatorPool", "node-workers", {"cluster": "west", "controller": "DaemonSet", "replicas": 1})
        root = resource("Deployment", "root", {"template": template(True)})
        root["metadata"]["labels"] = {"polyad.astrivant.com/internal": "true"}
        local, remote = ManagementAPI(pool, root), ManagementAPI()
        pools = manager(local, remote)
        pools.root.federation.children = AsyncMock(return_value=[])
        pod = template(True)
        command = ["/usr/bin/tini", "--", "python", "-m", "polyad.operator.runtime"]
        pod["spec"]["containers"][0]["command"] = command
        await pools.graph_pool(pool, pod, "owner")
        assert not remote.children("DaemonSet")
        poly = local.children("PolyGraph")[0]
        assert poly["metadata"]["labels"]["polyad.astrivant.com/internal"] == "true"
        controller = Controller(local)
        federation = Federation(local)
        federation.name = "management"
        federation.target = lambda cluster: (remote, "test")
        controller.federation = federation
        key = "PolyGraph", "test", poly["metadata"]["name"]
        for _ in range(4):
            try:
                await controller.reconcile(key)
            except Pending:
                pass
        instance = next(item for item in remote.children("Graph") if not item["spec"].get("templateOnly"))
        for _ in range(3):
            try:
                await Controller(remote).reconcile(("Graph", "test", instance["metadata"]["name"]))
            except Pending:
                pass
        native = remote.children("DaemonSet")[0]
        assert native["metadata"]["ownerReferences"][0]["uid"] == instance["metadata"]["uid"]
        assert native["metadata"]["labels"]["polyad.astrivant.com/internal"] == "true"
        assert native["spec"]["template"]["spec"]["containers"][0]["command"] == command

    asyncio.run(run())
