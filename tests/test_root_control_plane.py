"""
Exercise central authority, worker bootstrap and remote scale admission boundaries.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from polyad.metrics.builder import MetricsAPIBuilder
from polyad.metrics.store import MetricsStore
from polyad.operator.controller import Controller, Pending
from polyad.operator.coordination import DURATION, WRITE_BUDGET, Coordinator, NotOwner, active_shard, root_shard
from polyad.operator.pools import FINALIZER, OWNER, PoolManager
from polyad.operator.root import RootControlPlane
from polyad_types.resources import encode_body
from tests.test_coordination import LeaseAPI
from tests.test_metrics_api import snapshot
from tests.test_operator import FakeAPI, resource, template
from tests.test_replication import group, turn


class ManagementAPI(FakeAPI):
    """
    Add real merge-patch spec behavior to the existing versioned fake.
    """

    async def request(self, method, kind, namespace, name="", body=None, **kwargs):
        """
        Model default-preserving updates, including generation changes and secret data.
        """
        body = encode_body(body)
        result = await super().request(method, kind, namespace, name, body, **kwargs)
        if method == "PATCH" and "status" not in body:
            obj = self.objects[(kind, namespace, name)]
            for key in ("spec", "data"):
                if key in body:
                    obj.setdefault(key, {}).update(copy.deepcopy(body[key]))
                    if key == "spec":
                        obj["metadata"]["generation"] += 1
            result = copy.deepcopy(obj)
        return result


def manager(root_api, remote):
    """
    Build a root manager with one administrator-registered destination.
    """
    root = SimpleNamespace(
        controller=SimpleNamespace(api=root_api),
        coordinator=SimpleNamespace(namespace="test"),
        federation=SimpleNamespace(name="management"),
        resolve=lambda name: (remote, "test"),
    )
    return PoolManager(root)


def scale_request(target, replicas):
    """
    Pin a remote ReplicaGroup incarnation in a root-local scale request.
    """
    intent = resource(
        "RemoteScale",
        "consumers",
        {
            "cluster": "west",
            "replicas": replicas,
            "target": {
                "name": target["metadata"]["name"],
                "uid": target["metadata"]["uid"],
                "generation": target["metadata"]["generation"],
            },
        },
    )
    target["spec"]["remoteScaling"] = {"root": "management", **{key: intent["metadata"][key] for key in ("namespace", "name", "uid")}}
    return intent


def test_remote_workers_never_elect_a_planner_and_stop_on_root_heartbeat_loss(monkeypatch):
    """
    A surviving remote worker cannot replace root authority or keep mutating indefinitely.
    """
    now = [100.0]
    monkeypatch.setattr("polyad.operator.coordination.time.monotonic", lambda: now[0])

    async def scenario():
        api = LeaseAPI()
        root = Coordinator(api, "test", "root")
        worker = Coordinator(api, "test", "worker", planner=False)
        await worker.tick()
        assert not worker.leader and not worker.owned
        assert await api.get("Lease", "test", "polyad-leader") is None
        await root.tick()
        await worker.tick()
        assert worker.owned and not worker.leader
        shard = next(iter(worker.owned))
        token = active_shard.set(shard)
        try:
            await worker.guard()
            now[0] += DURATION - WRITE_BUDGET + 1
            with pytest.raises(NotOwner, match="root planner heartbeat"):
                await worker.guard()
            now[0] += WRITE_BUDGET
            await worker.tick()
            assert not worker.owned
        finally:
            active_shard.reset(token)
        assert (await api.get("Lease", "test", "polyad-leader"))["spec"]["holderIdentity"] == "root"

    asyncio.run(scenario())


def test_cluster_routing_keeps_root_leases_and_serializes_colliding_families():
    """
    Remote parent lookups use the target API while all writes share root-held ownership.
    """

    async def scenario():
        root_api = LeaseAPI()
        worker = Coordinator(root_api, "test", "root")
        remote = FakeAPI(resource("Graph", "workflow"))
        key = "Graph", "test", "workflow"
        shard = await worker.shard_for(key, api=remote, cluster="west")
        assert shard == root_shard("Graph", "west/test", "workflow")
        assert await worker.claim(f"polyad-shard-{shard}")
        worker.owned.add(shard)
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        async def first():
            async with worker.duty(key, api=remote, cluster="west"):
                order.append("first")
                entered.set()
                await release.wait()

        async def second():
            await entered.wait()
            async with worker.duty(key, api=remote, cluster="west"):
                order.append("second")

        a, b = asyncio.create_task(first()), asyncio.create_task(second())
        await entered.wait()
        await asyncio.sleep(0)
        assert order == ["first"]
        release.set()
        await asyncio.gather(a, b)
        assert order == ["first", "second"]
        assert not remote.calls

    asyncio.run(scenario())


def test_root_cache_loss_fences_every_remote_mutation():
    """
    Lease health alone cannot authorize writes when root storage is unreachable.
    """

    async def scenario():
        plane = object.__new__(RootControlPlane)
        plane.shared = SimpleNamespace(ping=AsyncMock(side_effect=ConnectionError("offline")))
        plane.coordinator = SimpleNamespace(guard=AsyncMock())
        with pytest.raises(ConnectionError):
            await plane.guard()
        plane.coordinator.guard.assert_not_awaited()

    asyncio.run(scenario())


def test_root_scale_reuses_live_graph_rules_and_retains_existing_execution():
    """
    Root KEDA requests reach ReplicaGroup admission without directly scaling native controllers.
    """

    async def scenario():
        target = group(1)
        intent = scale_request(target, 3)
        remote = ManagementAPI(target, resource("Daemon", "worker", {"template": template(True)}))
        await turn(remote)
        original = remote.children("Deployment")[0]["metadata"]["uid"]
        root_api = ManagementAPI(intent)
        pools = manager(root_api, remote)
        remote.objects[("GraphRule", "test", "limit")] = resource("GraphRule", "limit", {"limits": {"nodes": 2}})
        await pools.scale(intent)
        status = root_api.children("RemoteScale")[0]["status"]
        assert status["phase"] == "Pending" and status["replicas"] == 1
        assert status["labelSelector"] == "polyad.astrivant.com/root-scale=uid-consumers"
        assert remote.objects[("ReplicaGroup", "test", "copies")]["spec"]["replicas"] == 1
        with pytest.raises(ValueError):
            await Controller(remote).reconcile(("ReplicaGroup", "test", "copies"))
        assert [item["metadata"]["uid"] for item in remote.children("Deployment")] == [original]
        assert not remote.children("Deployment")[0]["metadata"].get("deletionTimestamp")
        remote.objects[("GraphRule", "test", "limit")]["spec"]["limits"]["nodes"] = 3
        await turn(remote)
        assert len(remote.children("Deployment")) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["replaced", "bounds", "inherited", "duplicate", "opt-out", "local-edit", "reserved", "local-cluster"])
def test_remote_scale_rejects_ambiguous_or_invalid_targets(failure):
    """
    Root requests cannot accidentally scale a replacement, violate limits or compete for a target.
    """

    async def scenario():
        target = group(1)
        intent = scale_request(target, 2)
        if failure == "replaced":
            target["metadata"]["uid"] = "replacement"
        elif failure == "bounds":
            target["spec"]["maxReplicas"] = 1
        elif failure == "inherited":
            target["spec"]["replicaSource"] = {"name": "source", "uid": "source-uid"}
        elif failure == "opt-out":
            del target["spec"]["remoteScaling"]
        elif failure == "local-edit":
            target["metadata"]["generation"] += 1
        elif failure == "reserved":
            target["metadata"]["labels"] = {"polyad.astrivant.com/internal": "true"}
        elif failure == "local-cluster":
            intent["spec"]["cluster"] = "management"
        root_api, remote = ManagementAPI(intent), ManagementAPI(target)
        if failure == "duplicate":
            other = copy.deepcopy(intent)
            other["metadata"].update(name="duplicate", uid="another")
            root_api.objects[("RemoteScale", "test", "duplicate")] = other
            intent = other
        with pytest.raises(ValueError):
            await manager(root_api, remote).scale(intent)
        assert not remote.calls

    asyncio.run(scenario())


def test_pool_install_upgrade_secret_rotation_and_scale_zero(monkeypatch, tmp_path):
    """
    Root installation stays owned, rolls credential revisions and only scales worker capacity.
    """
    monkeypatch.setenv("POLYAD_ROOT_DEPLOYMENT", "root")
    monkeypatch.setenv("POLYAD_OPERATOR_IMAGE", "polyad:v1")
    monkeypatch.setenv("POLYAD_CRD_DIRECTORY", str(tmp_path))
    (tmp_path / "graphs.yaml").write_text(
        json.dumps(
            {
                "apiVersion": "apiextensions.k8s.io/v1",
                "kind": "CustomResourceDefinition",
                "metadata": {"name": "graphs.polyad.astrivant.com"},
                "spec": {"scope": "Namespaced"},
            }
        )
    )

    async def scenario():
        pool = resource("OperatorPool", "west", {"cluster": "west", "replicas": 2})
        root_deployment = resource(
            "Deployment",
            "root",
            {
                "template": {
                    "metadata": {"labels": {"app": "root"}},
                    "spec": {
                        "serviceAccountName": "root",
                        "containers": [
                            {
                                "name": "operator",
                                "image": "polyad:v1",
                                "command": ["/usr/bin/tini", "--", "python", "-m", "polyad.operator.runtime"],
                                "env": [
                                    {"name": "POLYAD_ROOT_ENABLED", "value": "true"},
                                    {"name": "POLYAD_CACHE_URL", "valueFrom": {"secretKeyRef": {"name": "access", "key": "url"}}},
                                    {"name": "POLYAD_AUTH_CONFIG_FILE", "value": "/var/run/polyad/authentication/config.json"},
                                    {"name": "POLYAD_TRACING_ENABLED", "value": "true"},
                                    {"name": "POLYAD_LOGS_ENABLED", "value": "true"},
                                    {"name": "POLYAD_POD_CLUSTER", "value": "management"},
                                    {
                                        "name": "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
                                        "valueFrom": {"secretKeyRef": {"name": "tracing", "key": "headers"}},
                                    },
                                    {
                                        "name": "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
                                        "valueFrom": {"secretKeyRef": {"name": "tracing", "key": "headers"}},
                                    },
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "root", "secret": {"secretName": "access"}},
                            {"name": "authentication", "projected": {"sources": []}},
                        ],
                    },
                }
            },
        )
        secret = resource("Secret", "access")
        secret["data"] = {"config": "e30=", "url": "cmVkaXM6Ly9jYWNoZQ=="}
        tracing_secret = resource("Secret", "tracing")
        tracing_secret["data"] = {"headers": "YXV0aG9yaXphdGlvbj10b2tlbg=="}
        root_api, remote = ManagementAPI(pool, root_deployment, secret, tracing_secret), ManagementAPI()
        pools = manager(root_api, remote)
        with pytest.raises(Pending, match="ownership recorded"):
            await pools.pool(pool)
        assert not remote.calls
        pool = await root_api.get("OperatorPool", "test", "west")
        assert FINALIZER in pool["metadata"]["finalizers"]
        await pools.pool(pool)
        assert root_api.children("OperatorPool")[0]["status"]["labelSelector"] == "polyad.astrivant.com/root-scale=uid-west"
        deployed = remote.children("Deployment")[0]
        assert deployed["spec"]["replicas"] == 2
        pod = deployed["spec"]["template"]
        assert pod["spec"]["automountServiceAccountToken"] is False
        assert pod["spec"]["containers"][0]["command"] == ["/usr/bin/tini", "--", "python", "-m", "polyad.operator.runtime"]
        assert "serviceAccountName" not in pod["spec"]
        assert pod["metadata"]["labels"] != root_deployment["spec"]["template"]["metadata"]["labels"]
        env = {item["name"]: item for item in pod["spec"]["containers"][0]["env"]}
        assert env["POLYAD_ROOT_WORKER"]["value"] == "true"
        assert env["KUBECONFIG"]["value"] == "/var/run/polyad/root/config"
        assert env["POLYAD_API_ENABLED"]["value"] == "false"
        assert env["POLYAD_CACHE_URL"]["valueFrom"]["secretKeyRef"]["name"] == remote.children("Secret")[0]["metadata"]["name"]
        assert "POLYAD_AUTH_CONFIG_FILE" not in env
        assert env["POLYAD_TRACING_ENABLED"]["value"] == "true"
        assert env["POLYAD_LOGS_ENABLED"]["value"] == "true"
        assert env["POLYAD_POD_CLUSTER"]["value"] == "west"
        trace_secret_name = env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"]["valueFrom"]["secretKeyRef"]["name"]
        assert trace_secret_name != "tracing"
        assert env["OTEL_EXPORTER_OTLP_LOGS_HEADERS"]["valueFrom"]["secretKeyRef"]["name"] == trace_secret_name
        hierarchy = root_api.children("PolyGraph")[0]
        assert [node["name"] for node in hierarchy["spec"]["nodes"]] == ["root", "pool-uid-west"]
        assert hierarchy["spec"]["nodes"][1]["cluster"] == "west"
        assert len(root_api.children("Deployment")) == len(remote.children("Deployment")) == 1
        assert (
            next(obj for obj in remote.children("Secret") if obj["metadata"]["name"] == trace_secret_name)["data"] == tracing_secret["data"]
        )
        assert all(volume["name"] != "authentication" for volume in pod["spec"].get("volumes", []))
        old_revision = pod["metadata"]["annotations"].copy()
        root_api.objects[("Secret", "test", "tracing")]["metadata"]["resourceVersion"] = "2"
        root_api.objects[("Deployment", "test", "root")]["spec"]["template"]["spec"]["containers"][0]["image"] = "polyad:v2"
        pool = await root_api.get("OperatorPool", "test", "west")
        pool["spec"]["replicas"] = 0
        before = len(remote.calls)
        with pytest.raises(Pending, match="desired image"):
            await pools.pool(pool)
        assert len(remote.calls) == before
        monkeypatch.setenv("POLYAD_OPERATOR_IMAGE", "polyad:v2")
        rule = resource("GraphRule", "block-operators", {"enforcement": "Namespace", "limits": {"nodes": 0}})
        remote.objects["GraphRule", "test", "block-operators"] = rule
        with pytest.raises(ValueError, match="nodes"):
            await pools.pool(pool)
        assert remote.children("Deployment")[0]["spec"]["replicas"] == 2
        del remote.objects["GraphRule", "test", "block-operators"]
        await pools.pool(pool)
        updated = remote.children("Deployment")[0]
        assert updated["spec"]["replicas"] == 0
        assert updated["spec"]["template"]["metadata"]["annotations"] != old_revision
        assert updated["spec"]["template"]["spec"]["containers"][0]["image"] == "polyad:v2"
        assert not any(call[0] == "DELETE" for call in remote.calls)
        assert len(remote.children("CustomResourceDefinition")) == 1

    asyncio.run(scenario())


def test_pool_refuses_to_adopt_unrelated_resources():
    """
    A stable address alone never grants permission to replace another root's worker.
    """

    async def scenario():
        deployment = resource("Deployment", "existing", {"replicas": 3})
        remote = ManagementAPI(deployment)
        with pytest.raises(ValueError, match="unmanaged"):
            await manager(ManagementAPI(), remote).apply(remote, copy.deepcopy(deployment), "new-root")
        assert not remote.calls
        assert OWNER not in remote.objects[("Deployment", "test", "existing")]["metadata"].get("annotations", {})

    asyncio.run(scenario())


def test_root_metrics_select_cluster_and_fail_closed_on_missing_samples():
    """
    Identical remote names stay isolated, and expired data cannot trigger scale to zero.
    """
    target = group(2)
    target["status"] = {
        "observedGeneration": 1,
        "scaleCurrent": True,
        "scaleObservedAt": datetime.now(UTC).isoformat(),
        "replicas": 2,
        "desiredReplicas": 2,
        "readyReplicas": 2,
        "totalReplicas": 2,
        "instanceCount": 1,
    }
    data = snapshot()
    data["clusters"] = {"west": snapshot([target]), "east": {"namespace": "test", "inventory": {"fresh": False}}}
    store = MetricsStore()
    store.publish(data, graph_labels=True)
    client = MetricsAPIBuilder().with_store(store).build().test_client()
    path = "/v1/workloads/ReplicaGroup/copies/replicas"
    assert client.get(path).status_code == 404
    response = client.get(path + "?cluster=west")
    assert response.status_code == 200 and response.json["value"] == 2 and response.json["cluster"] == "west"
    assert client.get(path + "?cluster=east").status_code == 503
    assert client.get(path + "?cluster=unregistered").status_code == 404
    metrics = client.get("/metrics").text
    assert "polyad_cluster_workload_signal{" in metrics and 'cluster="west"' in metrics


def test_pool_namespace_cannot_move_before_remote_cleanup():
    """
    A registry edit cannot strand live workers by redirecting a pool's cleanup address.
    """

    async def scenario():
        pool = resource("OperatorPool", "west", {"cluster": "west", "replicas": 1})
        pool["metadata"]["annotations"] = {"polyad.astrivant.com/worker-namespace": "original"}
        remote = ManagementAPI()
        with pytest.raises(ValueError, match="retain the pool"):
            await manager(ManagementAPI(pool), remote).pool(pool)
        assert not remote.calls

    asyncio.run(scenario())


def test_pool_deletion_waits_for_its_workers_and_preserves_workloads():
    """
    Cleanup observes deletion before dropping the finalizer and never deletes application Pods.
    """

    async def scenario():
        pool = resource("OperatorPool", "west", {"cluster": "west", "replicas": 1})
        pool["metadata"].update(deletionTimestamp="now", finalizers=[FINALIZER])
        pool["metadata"]["annotations"] = {"polyad.astrivant.com/worker-namespace": "test"}
        deployment = resource("Deployment", "worker", {"replicas": 1})
        deployment["metadata"].update(
            labels={"polyad.astrivant.com/operator-pool": pool["metadata"]["uid"]},
            annotations={OWNER: json.dumps(["management", "test", pool["metadata"]["uid"]], separators=(",", ":"))},
        )
        app = resource("Deployment", "application", {"replicas": 3})
        root_api, remote = ManagementAPI(pool), ManagementAPI(deployment, app)
        pools = manager(root_api, remote)
        with pytest.raises(Pending, match="remote worker cleanup"):
            await pools.pool(pool)
        assert remote.calls == [("DELETE", "Deployment", "worker")]
        assert FINALIZER in root_api.objects[("OperatorPool", "test", "west")]["metadata"]["finalizers"]
        del remote.objects[("Deployment", "test", "worker")]
        await pools.pool(pool)
        assert not root_api.objects[("OperatorPool", "test", "west")]["metadata"]["finalizers"]
        assert not remote.objects[("Deployment", "test", "application")]["metadata"].get("deletionTimestamp")

    asyncio.run(scenario())


def test_remote_worker_consumes_root_queue_and_freezes_execution_when_root_is_lost(monkeypatch):
    """
    Exercise the remote worker with actual controllers and leased root authority.
    """
    monkeypatch.setenv("POLYAD_CLUSTER_NAME", "management")
    monkeypatch.setenv("POLYAD_ROOT_ENABLED", "true")
    monkeypatch.setenv("POLYAD_FEDERATION_CLUSTERS", "[]")

    class MemoryQueue:
        def __init__(self, url, namespace, consumer):
            self.namespace = namespace
            self.messages = []
            self.backlog_sample = (0, {})
            self.ping = AsyncMock()
            self.client = SimpleNamespace(set=AsyncMock(), get=AsyncMock(return_value=None))

        async def publish(self, shard, key):
            if (shard, key) not in self.messages:
                self.messages.append((shard, key))

        async def take(self, shard):
            return next(((str(index), key) for index, (slot, key) in enumerate(self.messages) if slot == shard), None)

        async def acknowledge(self, shard, message_id):
            self.messages.pop(int(message_id))

        async def sample_backlog(self, shards):
            pass

        def backlog(self, shards):
            return {"fresh": True, "sampleAgeSeconds": 0}

    class MemoryEvents:
        def __init__(self, *args, **kwargs):
            self.publish = AsyncMock()

    class GuardedAPI(ManagementAPI):
        async def request(self, method, *args, **kwargs):
            if method not in {"GET", "HEAD"}:
                await self.before_write()
            return await super().request(method, *args, **kwargs)

    monkeypatch.setattr("polyad.operator.root.SharedQueue", MemoryQueue)
    monkeypatch.setattr("polyad.operator.root.EventStore", MemoryEvents)

    async def scenario():
        coordinator = Coordinator(LeaseAPI(), "test", "root")
        remote = GuardedAPI(group(1), resource("Daemon", "worker", {"template": template(True)}))
        controller = Controller(ManagementAPI())
        controller.federation = SimpleNamespace(
            name="management", clusters={"west": {"namespace": "test"}}, target=lambda _: (remote, "test")
        )
        root_queue = MemoryQueue("unused", "test", "root")
        plane = RootControlPlane(coordinator, controller, root_queue)
        remote.before_write = plane.guard
        worker = plane.workers["west"]
        await worker.scan()
        shard = await worker.shard_for(("ReplicaGroup", "test", "copies"))
        await coordinator.claim(f"polyad-shard-{shard}")
        coordinator.owned.add(shard)
        await worker.consume()
        assert len(remote.children("Deployment")) == 1
        worker.events.publish.assert_awaited()
        assert not worker.shared.messages
        assert not remote.children("Lease")
        assert root_queue.client.set.await_args.args[0] == "polyad:test:cluster-observation:west"
        remote.objects[("ReplicaGroup", "test", "copies")]["spec"]["replicas"] = 2
        await worker.scan()
        root_queue.ping.side_effect = ConnectionError("root disconnected")
        await worker.consume()
        assert len(remote.children("Deployment")) == 1
        assert worker.shared.messages  # Failed mutation is still recoverable.
        root_queue.ping.side_effect = None
        await worker.consume()
        assert len(remote.children("Deployment")) == 2
        assert not worker.shared.messages

    asyncio.run(scenario())


def test_root_upgrade_reassigns_old_workers_before_upgrading_their_pools(monkeypatch):
    """
    Old images cannot retain the management shards needed to replace themselves.
    """
    monkeypatch.setenv("POLYAD_ROOT_ENABLED", "true")
    monkeypatch.setenv("POLYAD_OPERATOR_IMAGE", "polyad:v2")

    async def scenario():
        api = LeaseAPI()
        root = Coordinator(api, "test", "root-v2")
        old = Coordinator(api, "test", "worker-v1", planner=False)
        old.image = "polyad:v1"
        await old.tick()
        await root.tick()
        plan = json.loads((await api.get("Lease", "test", "polyad-leader"))["metadata"]["annotations"]["polyad.astrivant.com/assignments"])
        assert set(plan.values()) == {"root-v2"}
        assert root.owned
        await old.tick()
        assert not old.owned

    asyncio.run(scenario())
