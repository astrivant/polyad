"""
Keep Helm worker lifecycle separate from explicit root attachment and scaling grants.
"""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from unittest.mock import AsyncMock

import pytest
from deepdiff import DeepDiff
from kubernetes.client.exceptions import ApiException

from polyad.events.visibility import INTERNAL, public_observation
from polyad.exceptions.coordination import NotOwner
from polyad.exceptions.reconciliation import Pending
from polyad.operator.clusters.pools import ATTACHMENT, FINALIZER, REGISTERED, SCALING
from polyad.operator.clusters.reserved import DEPLOYMENT
from polyad.operator.coordination.leases import DURATION, Coordinator, active_shard
from tests.test_chart import render
from tests.test_coordination import LeaseAPI
from tests.test_operator import resource
from tests.test_reserved_topology import operator_deployment
from tests.test_root_control_plane import ManagementAPI, manager


def test_worker_chart_connects_to_root_without_local_authority_or_servers():
    """
    Root and hosting namespaces, cluster identities and credentials remain distinct.
    """
    objects = render("tracing.logs.enabled=true", values_files=("worker-values.yaml",))
    assert {obj["kind"] for obj in objects} == {"CustomResourceDefinition", "ServiceAccount", "Deployment"}
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment")
    assert "replicas" not in deployment["spec"]
    assert deployment["metadata"]["annotations"][SCALING] == "Root"
    assert json.loads(deployment["metadata"]["annotations"][ATTACHMENT]) == [
        "management",
        "control",
        "root-polyad",
        "root-atlas",
        "west-workers",
    ]
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    operator = pod["containers"][0]
    assert operator["args"][0] == "--namespace=control"
    assert operator["command"] == ["/usr/bin/tini", "--", "python", "-m", "polyad.operator.runtime"]
    env = {item["name"]: item for item in operator["env"]}
    for key, value in {
        "POLYAD_ROOT_WORKER": "true",
        "POLYAD_ROOT_ENABLED": "true",
        "POLYAD_COMPONENT": "executor",
        "POLYAD_CLUSTER_NAME": "management",
        "POLYAD_POD_CLUSTER": "west",
        "POLYAD_WORKER_POOL": "west-workers",
        "POLYAD_WORKER_DEPLOYMENT": "test-polyad",
        "POLYAD_WORKER_CLUSTER": "west",
        "POLYAD_ROOT_DEPLOYMENT": "root-polyad",
        "POLYAD_SELF_GRAPH": "root-atlas",
        "POLYAD_SELF_GRAPH_KIND": "PolyGraph",
        "KUBECONFIG": "/var/run/polyad/root/config",
        "POLYAD_API_ENABLED": "false",
        "POLYAD_METRICS_ENABLED": "false",
    }.items():
        assert env[key]["value"] == value
    assert operator["ports"] == [{"name": "health", "containerPort": 8080}]
    assert {volume.get("secret", {}).get("secretName") for volume in pod["volumes"]} >= {"root-access", "west-access", "root-cache"}


def test_local_worker_scaling_uses_helm_and_optional_hpa():
    """
    Local authority permits the ordinary HA replica and CPU/memory autoscaling settings.
    """
    objects = render("worker.scalingAuthority=Local", "ha=true", "operator.replicaCount=3", values_files=("worker-values.yaml",))
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment")
    assert deployment["spec"]["replicas"] == 3
    assert deployment["metadata"]["annotations"][SCALING] == "Local"
    objects = render(
        "worker.scalingAuthority=Local",
        "ha=true",
        "operator.autoscaling.enabled=true",
        "operator.autoscaling.targetMemoryUtilizationPercentage=80",
        values_files=("worker-values.yaml",),
    )
    assert "replicas" not in next(obj for obj in objects if obj["kind"] == "Deployment")["spec"]
    hpa = next(obj for obj in objects if obj["kind"] == "HorizontalPodAutoscaler")
    assert hpa["spec"]["scaleTargetRef"] == {"apiVersion": "apps/v1", "kind": "Deployment", "name": "test-polyad"}
    assert {metric["resource"]["name"] for metric in hpa["spec"]["metrics"]} == {"cpu", "memory"}


@pytest.mark.parametrize(
    "setting",
    [
        "worker.rootNamespace=",
        "worker.rootClusterName=west",
        "federation.clusters[0].namespace=wrong",
        "api.enabled=true",
        "metrics.enabled=true",
        "observer.enabled=true",
        "dragonfly.enabled=true",
        "rootControlPlane.enabled=true",
        "architecture.mode=Distributed",
        "operator.replicaCount=2",
        "operator.autoscaling.enabled=true",
        "rootControlPlane.kubeconfigSecret=",
        "worker.scalingAuthority=Unknown",
    ],
)
def test_invalid_worker_combinations_fail_before_install(setting):
    """
    Fail configuration mistakes before creating a worker with competing local authority.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(setting, values_files=("worker-values.yaml",))


@pytest.mark.parametrize("authority", ["Root", "Local"])
def test_root_chart_registers_an_existing_worker_without_deploying_it(authority):
    """
    Store attachment intent at the root while the downstream release owns its Deployment.
    """
    objects = render(
        f"rootControlPlane.pools[0].scalingAuthority={authority}", values_files=("root-values.yaml", "attached-pool-values.yaml")
    )
    pool = next(obj for obj in objects if obj["kind"] == "OperatorPool")
    assert pool["spec"]["existingDeployment"] == "west-polyad"
    assert pool["spec"]["scalingAuthority"] == authority
    assert not any(obj["kind"] == "Deployment" and obj["metadata"]["name"] == "west-polyad" for obj in objects)


@pytest.mark.parametrize(
    "setting",
    [
        "rootControlPlane.pools[0].controller=DaemonSet",
        "rootControlPlane.pools[0].resources.requests.cpu=100m",
        "rootControlPlane.pools[0].nodeSelector.pool=execution",
        "rootControlPlane.pools[0].scalingAuthority=Unknown",
    ],
)
def test_attachment_schema_rejects_competing_configuration(setting):
    """
    An attachment cannot overwrite the administrator's controller or Pod configuration.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(setting, values_files=("root-values.yaml", "attached-pool-values.yaml"))


def test_local_scaling_requires_a_helm_attachment():
    """
    Root-provisioned pools cannot lose their only replica owner.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render("rootControlPlane.pools[0].scalingAuthority=Local", values_files=("root-values.yaml", "pool-values.yaml"))


def installed_pool(monkeypatch, authority="Root"):
    """
    Provide a Helm-owned worker and matching root attachment in isolated fake clusters.
    """
    monkeypatch.setenv("POLYAD_ROOT_DEPLOYMENT", "root")
    monkeypatch.setenv("POLYAD_SELF_GRAPH", "operators")
    pool = resource(
        "OperatorPool",
        "west-workers",
        {"cluster": "west", "replicas": 3, "existingDeployment": "west-polyad", "scalingAuthority": authority},
    )
    pool["metadata"].update(finalizers=[FINALIZER], annotations={"polyad.astrivant.com/worker-namespace": "test"})
    worker = operator_deployment("west-polyad")
    worker["metadata"]["annotations"] = {
        ATTACHMENT: json.dumps(["management", "test", "root", "operators", "west-workers"]),
        SCALING: authority,
        "meta.helm.sh/release-name": "west",
        "meta.helm.sh/release-namespace": "test",
    }
    worker["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "Helm"
    local, remote = ManagementAPI(pool, operator_deployment("root")), ManagementAPI(worker)
    return manager(local, remote), local, remote, pool, worker


@pytest.mark.parametrize("authority", ["Root", "Local"])
def test_attachment_links_graph_and_preserves_helm_ownership(monkeypatch, authority):
    """
    Both modes join the hierarchy; only an explicit Root grant changes native replicas.
    """
    pools, local, remote, pool, before = installed_pool(monkeypatch, authority)

    async def run():
        await pools.pool(pool)
        poly = local.children("PolyGraph")[0]
        assert [node["name"] for node in poly["spec"]["nodes"]] == ["root", "pool-uid-west-wor"]
        graph = remote.children("Graph")[0]
        assert graph["metadata"]["annotations"][DEPLOYMENT] == "west-polyad"
        current = remote.children("Deployment")[0]
        assert current["spec"]["replicas"] == (3 if authority == "Root" else 2)
        assert not DeepDiff(
            before,
            current,
            exclude_paths={"root['spec']['replicas']", "root['metadata']['generation']", "root['metadata']['resourceVersion']"},
        )
        assert not any(method == "POST" and kind == "Deployment" for method, kind, _ in remote.calls)
        assert not remote.children("Secret") and not remote.children("CustomResourceDefinition")
        if authority == "Local":
            assert not any(kind == "Deployment" for _, kind, _ in remote.calls)
            assert local.children("OperatorPool")[0]["status"]["phase"] == "Ready"
        assert not await public_observation(remote, current)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["wrong-root", "wrong-pool", "local-revocation", "not-internal", "other-controller"])
def test_attachment_grant_conflicts_never_adopt_or_scale(monkeypatch, change):
    """
    Require the exact root, pool and local scaling grant before registering or mutating.
    """
    pools, local, remote, pool, _ = installed_pool(monkeypatch)
    worker = remote.objects["Deployment", "test", "west-polyad"]
    if change in {"wrong-root", "wrong-pool"}:
        value = json.loads(worker["metadata"]["annotations"][ATTACHMENT])
        value[0 if change == "wrong-root" else -1] = "other"
        worker["metadata"]["annotations"][ATTACHMENT] = json.dumps(value)
    elif change == "local-revocation":
        worker["metadata"]["annotations"][SCALING] = "Local"
    elif change == "not-internal":
        worker["metadata"]["labels"][INTERNAL] = "false"
    else:
        worker["metadata"]["ownerReferences"] = [{"controller": True, "uid": "other"}]
    with pytest.raises(ValueError, match="attachment or scaling authority"):
        asyncio.run(pools.pool(pool))
    assert not remote.calls
    assert REGISTERED not in local.children("OperatorPool")[0]["metadata"]["annotations"]


def test_concurrent_local_revocation_blocks_root_scale(monkeypatch):
    """
    A grant edit between planning and dispatch invalidates the native resourceVersion.
    """
    pools, _, remote, pool, _ = installed_pool(monkeypatch)
    original = pools.graph_pool

    async def revoke(*args):
        boundary = await original(*args)
        worker = remote.objects["Deployment", "test", "west-polyad"]
        worker["metadata"]["annotations"][SCALING] = "Local"
        worker["metadata"]["resourceVersion"] = "2"
        return boundary

    pools.graph_pool = revoke
    with pytest.raises(ApiException) as error:
        asyncio.run(pools.pool(pool))
    assert error.value.status == 409
    assert remote.children("Deployment")[0]["spec"]["replicas"] == 2


def test_detachment_never_deletes_helm_resources(monkeypatch):
    """
    Root cleanup removes its observation definitions while retaining the administrator's Deployment.
    """
    pools, local, remote, pool, _ = installed_pool(monkeypatch, "Local")

    async def run():
        await pools.pool(pool)
        live = local.objects["OperatorPool", "test", "west-workers"]
        current = copy.deepcopy(remote.children("Deployment")[0])
        live["metadata"]["deletionTimestamp"] = "now"
        pools.root.federation.children = AsyncMock(return_value=[])
        for _ in range(4):
            try:
                await pools.pool(await local.get("OperatorPool", "test", "west-workers"))
            except Pending:
                pass

            # Kubernetes garbage collection acknowledges deletion separately.
            for key, obj in list(remote.objects.items()):
                if obj["metadata"].get("deletionTimestamp"):
                    del remote.objects[key]
        assert not DeepDiff(current, remote.children("Deployment")[0])
        assert len(local.children("PolyGraph")[0]["spec"]["nodes"]) == 1
        assert not remote.children("Graph") and not remote.children("Daemon")
        assert not any(method == "DELETE" and kind == "Deployment" for method, kind, _ in remote.calls)

    asyncio.run(run())


def test_helm_worker_waits_for_registration_and_stops_when_detached(monkeypatch):
    """
    Registration gates both shard membership and fresh write authority without planner failover.
    """
    now = [100.0]
    monkeypatch.setattr("polyad.operator.coordination.leases.time.monotonic", lambda: now[0])

    async def run():
        api = LeaseAPI()
        root = Coordinator(api, "test", "root")
        await root.tick()
        monkeypatch.setenv("POLYAD_WORKER_POOL", "west-workers")
        monkeypatch.setenv("POLYAD_WORKER_DEPLOYMENT", "west-polyad")
        monkeypatch.setenv("POLYAD_WORKER_CLUSTER", "west")
        worker = Coordinator(api, "test", "worker", planner=False)
        await worker.tick()
        assert not worker.owned and not worker.attachment_ready
        assert not any(obj["metadata"]["name"] == "polyad-member-worker" for obj in api.children("Lease"))
        pool = resource("OperatorPool", "west-workers", {"cluster": "west", "existingDeployment": "west-polyad"})
        pool["metadata"]["annotations"] = {REGISTERED: pool["metadata"]["uid"]}
        api.objects["OperatorPool", "test", "west-workers"] = pool
        await worker.tick()
        await root.tick()
        await worker.tick()

        # A joining worker must wait for the previous owner's lease to expire.
        now[0] += DURATION + 1
        await root.tick()
        await worker.tick()
        assert worker.attachment_ready and worker.owned and not worker.leader
        token = active_shard.set(next(iter(worker.owned)))
        try:
            await worker.guard()
            pool["metadata"]["deletionTimestamp"] = "now"
            with pytest.raises(NotOwner, match="attachment"):
                await worker.guard()
        finally:
            active_shard.reset(token)
        await worker.tick()
        assert not worker.owned and not worker.attachment_ready

    asyncio.run(run())


def test_missing_installed_worker_stays_pending_without_recreation(monkeypatch):
    """
    An administrator can uninstall a worker without the root recreating its Deployment.
    """
    pools, local, remote, pool, _ = installed_pool(monkeypatch)
    del remote.objects["Deployment", "test", "west-polyad"]
    with pytest.raises(Pending, match="administrator-installed"):
        asyncio.run(pools.pool(pool))
    assert not remote.calls
    assert REGISTERED not in local.children("OperatorPool")[0]["metadata"]["annotations"]


@pytest.mark.parametrize("boundary", ["root", "remote"])
def test_attached_scale_requires_fresh_rules_at_both_boundaries(monkeypatch, boundary):
    """
    A current rule rejection at either graph boundary prevents native replica writes.
    """
    pools, local, remote, pool, _ = installed_pool(monkeypatch)
    checks = []

    async def check(api, obj):
        checks.append((api, obj["kind"]))
        if api is (local if boundary == "root" else remote):
            raise ValueError("live graph constraint blocks scale")

    monkeypatch.setattr("polyad.operator.clusters.pools.check_live_rules", check)
    with pytest.raises(ValueError, match="live graph constraint"):
        asyncio.run(pools.pool(pool))
    assert checks == ([(local, "PolyGraph")] if boundary == "root" else [(local, "PolyGraph"), (remote, "Graph")])
    assert remote.children("Deployment")[0]["spec"]["replicas"] == 2
    assert not any(kind == "Deployment" for _, kind, _ in remote.calls)
