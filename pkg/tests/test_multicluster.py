"""
Exercise remote ownership, local execution authority and optional read replicas.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from kubernetes.client.exceptions import ApiException

from polyad.api.observations.app import ObservationAPI, build_app, observe
from polyad.compiler.passes.network import policy_specs, traffic
from polyad.operator.clusters.federation import INVENTORY, PARENT, REMOTE, Federation
from polyad.operator.policies.rules import RuleViolation, check_rules
from polyad.operator.reconciliation.controller import FINALIZER, Controller, Pending
from polyad_types import GraphNode, MeshPeer, NetworkAccess, NetworkPeer, NetworkPort, TrafficRule
from polyad_types.graphs.topology import topology
from tests.test_network import scope
from tests.test_operator import FakeAPI, resource, template


def setup():
    """
    Place definitions in the destination while retaining parent intent in the source.
    """
    parent = resource("PolyGraph", "application", {"nodes": [{"name": "worker", "kind": "Graph", "ref": "workflow", "cluster": "west"}]})
    local = FakeAPI(parent)
    remote = FakeAPI(
        resource("Graph", "workflow", {"templateOnly": True, "nodes": [{"name": "run", "kind": "Workload", "ref": "job"}]}),
        resource("Workload", "job", {"template": template()}),
    )
    controller = Controller(local)
    federation = Federation(local)
    federation.name = "east"
    federation.target = lambda name: (remote, "test") if name == "west" else (_ for _ in ()).throw(ValueError("unknown cluster"))
    controller.federation = federation
    return local, remote, controller


async def create_remote(local, remote, controller):
    """
    Observe the persisted inventory before admitting remote intent.
    """
    with pytest.raises(Pending, match="inventory persisted"):
        await controller.reconcile(("PolyGraph", "test", "application"))
    assert not any(call[0] == "POST" for call in remote.calls)
    assert json.loads(local.children("PolyGraph")[0]["metadata"]["annotations"][INVENTORY])[0]["cluster"] == "west"
    await controller.reconcile(("PolyGraph", "test", "application"))
    return next(item for item in remote.children("Graph") if not item["spec"].get("templateOnly"))


def test_remote_graph_ownership_and_local_operator_execution():
    """
    The source manages graph intent; the destination alone creates workloads under its local rules.
    """

    async def run():
        local, remote, controller = setup()
        child = await create_remote(local, remote, controller)
        assert not child["metadata"].get("ownerReferences")
        assert json.loads(child["metadata"]["annotations"][PARENT])[:5] == ["east", "test", "PolyGraph", "application", "uid-application"]
        assert child["metadata"]["annotations"][REMOTE] == "west"
        assert not local.children("Job") and not remote.children("Job")
        worker = Controller(remote)
        key = "Graph", "test", child["metadata"]["name"]
        with pytest.raises(Pending, match="finalizer persisted"):
            await worker.reconcile(key)
        await worker.reconcile(key)
        assert len(remote.children("Job")) == 1
        assert not local.children("Job")

    asyncio.run(run())


def test_lost_remote_create_acknowledgement_is_idempotent():
    """
    A committed remote create remains discoverable after a timeout and does not duplicate execution.
    """

    async def run():
        local, remote, controller = setup()
        with pytest.raises(Pending):
            await controller.reconcile(("PolyGraph", "test", "application"))
        remote.fail_create_after_commit = True
        with pytest.raises(ApiException):
            await controller.reconcile(("PolyGraph", "test", "application"))
        await controller.reconcile(("PolyGraph", "test", "application"))
        assert len([child for child in remote.children("Graph") if not child["spec"].get("templateOnly")]) == 1
        assert len([call for call in remote.calls if call[0] == "POST"]) == 1

    asyncio.run(run())


def test_source_and_destination_rules_gate_their_own_mutations():
    """
    Parent constraints reject remote intent, while destination rules independently reject execution.
    """

    async def run():
        local, remote, controller = setup()
        rule = resource("GraphRule", "blocked", {"limits": {"nodes": 0}})
        local.objects[("GraphRule", "test", "blocked")] = rule
        with pytest.raises(RuleViolation):
            await controller.reconcile(("PolyGraph", "test", "application"))
        assert not remote.calls
        local.objects.pop(("GraphRule", "test", "blocked"))
        remote.objects[("GraphRule", "test", "blocked")] = rule
        child = await create_remote(local, remote, controller)
        child["metadata"]["finalizers"] = [FINALIZER]
        with pytest.raises(RuleViolation):
            await Controller(remote).reconcile(("Graph", "test", child["metadata"]["name"]))
        assert not remote.children("Job")

    asyncio.run(run())


def test_nested_remote_polygraph_creates_children_through_its_local_operator():
    """
    A remote PolyGraph becomes a normal composition under the destination's execution authority.
    """

    async def run():
        local, remote, controller = setup()
        parent = local.children("PolyGraph")[0]
        parent["spec"]["nodes"][0].update(kind="PolyGraph", ref="region")
        region = resource(
            "PolyGraph", "region", {"templateOnly": True, "nodes": [{"name": "pipeline", "kind": "Graph", "ref": "workflow"}]}
        )
        remote.objects[("PolyGraph", "test", "region")] = region
        with pytest.raises(Pending, match="inventory persisted"):
            await controller.reconcile(("PolyGraph", "test", "application"))
        await controller.reconcile(("PolyGraph", "test", "application"))
        instance = next(item for item in remote.children("PolyGraph") if not item["spec"].get("templateOnly"))
        instance["metadata"]["finalizers"] = [FINALIZER]
        await Controller(remote).reconcile(("PolyGraph", "test", instance["metadata"]["name"]))
        graph = next(item for item in remote.children("Graph") if not item["spec"].get("templateOnly"))
        assert graph["metadata"]["ownerReferences"][0]["uid"] == instance["metadata"]["uid"]
        assert REMOTE not in graph["metadata"]["annotations"]

    asyncio.run(run())


def test_removed_remote_node_drains_and_its_absent_inventory_entry_is_pruned():
    """
    Topology changes preserve deletion ownership until remote cleanup is acknowledged.
    """

    async def run():
        local, remote, controller = setup()
        child = await create_remote(local, remote, controller)
        parent = local.children("PolyGraph")[0]
        parent["spec"]["nodes"] = []
        parent["metadata"]["generation"] += 1
        with pytest.raises(Pending, match="draining"):
            await controller.reconcile(("PolyGraph", "test", "application"))
        assert child["metadata"]["deletionTimestamp"]
        assert json.loads(parent["metadata"]["annotations"][INVENTORY])
        remote.objects.pop(("Graph", "test", child["metadata"]["name"]))
        with pytest.raises(Pending, match="inventory persisted"):
            await controller.reconcile(("PolyGraph", "test", "application"))
        assert json.loads(parent["metadata"]["annotations"][INVENTORY]) == []
        await controller.reconcile(("PolyGraph", "test", "application"))
        assert parent["status"]["completed"] is True

    asyncio.run(run())


def test_remote_parent_finalizer_waits_for_observed_cleanup_and_refuses_foreign_children():
    """
    Neither delete acknowledgement nor a foreign same-name graph releases the parent's finalizer.
    """

    async def run():
        local, remote, controller = setup()
        child = await create_remote(local, remote, controller)
        parent = local.children("PolyGraph")[0]
        parent["metadata"]["deletionTimestamp"] = "now"
        with pytest.raises(Pending, match="owned resources"):
            await controller.reconcile(("PolyGraph", "test", "application"))
        assert FINALIZER in parent["metadata"]["finalizers"]
        assert child["metadata"]["deletionTimestamp"] == "now"
        child["metadata"]["annotations"][PARENT] = "foreign"
        with pytest.raises(ValueError, match="different ownership"):
            await controller.reconcile(("PolyGraph", "test", "application"))
        assert FINALIZER in parent["metadata"]["finalizers"]
        remote.objects.pop(("Graph", "test", child["metadata"]["name"]))
        await controller.reconcile(("PolyGraph", "test", "application"))
        assert FINALIZER not in parent["metadata"]["finalizers"]

    asyncio.run(run())


def test_unreachable_remote_cluster_does_not_release_ownership_or_report_ready():
    """
    API failures are unknown state, never evidence that remote resources disappeared.
    """

    async def run():
        local, remote, controller = setup()
        await create_remote(local, remote, controller)
        parent = local.children("PolyGraph")[0]
        parent.setdefault("status", {})["ready"] = True
        parent["metadata"]["deletionTimestamp"] = "now"

        async def unavailable(*args):
            raise ApiException(status=503)

        remote.get = unavailable
        with pytest.raises(ApiException):
            await controller.reconcile(("PolyGraph", "test", "application"))
        assert FINALIZER in parent["metadata"]["finalizers"]
        assert parent["status"]["ready"] is False

    asyncio.run(run())


def test_remote_status_heartbeat_must_be_current():
    """
    A matching generation with an expired heartbeat does not complete the parent.
    """

    async def run():
        local, remote, controller = setup()
        child = await create_remote(local, remote, controller)
        child["status"] = {
            "observedGeneration": 1,
            "ready": True,
            "completed": True,
            "metricsObservedAt": (datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
        }
        await controller.reconcile(("PolyGraph", "test", "application"))
        assert local.children("PolyGraph")[0]["status"]["ready"] is False
        child["status"]["metricsObservedAt"] = datetime.now(UTC).isoformat()
        await controller.reconcile(("PolyGraph", "test", "application"))
        assert local.children("PolyGraph")[0]["status"]["completed"] is True

    asyncio.run(run())


def test_graphs_remain_cluster_local_and_remote_rules_stay_at_the_destination():
    """
    Remote vertices are valid under PolyGraphs, including nesting, but cannot escape a Graph's cluster.
    """

    async def run():
        local, _, _ = setup()
        poly = local.children("PolyGraph")[0]
        assert topology(poly["spec"], "PolyGraph").nodes[0].cluster == "west"
        await check_rules(local, "test", "PolyGraph", poly["spec"])
        nested = resource("PolyGraph", "nested", poly["spec"])
        local.objects[("PolyGraph", "test", "nested")] = nested
        with pytest.raises(RuleViolation, match="one cluster"):
            await check_rules(local, "test", "Graph", {"nodes": [{"name": "nested", "kind": "PolyGraph", "ref": "nested"}]})
        with pytest.raises(ValueError):
            topology(poly["spec"], "Graph")
        with pytest.raises(ValueError, match="containing"):
            GraphNode(name="copies", kind="ReplicaGroup", ref="copies", cluster="west")

    asyncio.run(run())


@pytest.mark.parametrize("mode,gateway_port", [("Direct", 15443), ("Gateway", 15443), ("Gateway", 26443)])
def test_remote_transport_and_identity_constraints(mode, gateway_port):
    """
    Remote ingress enforces exact mTLS identity while transport policy uses real remote addresses.
    """
    from polyad_types.serialization import converter

    peer = converter.structure({"name": "west", "mode": mode, "cidrs": ["192.0.2.1/32"], "gatewayPort": gateway_port}, MeshPeer)
    ingress = TrafficRule(
        NetworkPeer(cluster="west"),
        ports=(NetworkPort(8080),),
        principals=("cluster.local/ns/workflows/sa/reader-west",),
        methods=("POST",),
    )
    egress_port = gateway_port if mode == "Gateway" else 8080
    egress = TrafficRule(NetworkPeer(cluster="west"), ports=(NetworkPort(egress_port),))
    access = NetworkAccess(mesh=True, allowWithin=False, allowDNS=False, ingress=(ingress,), egress=(egress,))
    policies = policy_specs({"node": "receiver"}, [scope(access)], mesh_peers={"west": peer})
    source = policies["AuthorizationPolicy"]["rules"][0]["from"][0]["source"]
    assert source == {"principals": list(ingress.principals)}
    assert policies["NetworkPolicy"]["egress"][0]["to"] == [{"ipBlock": {"cidr": "192.0.2.1/32"}}]
    assert policies["NetworkPolicy"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": egress_port}]
    assert policies["NetworkPolicy"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8080}]
    if mode == "Gateway":
        wrong_port = TrafficRule(NetworkPeer(cluster="west"), ports=(NetworkPort(8080),))
        with pytest.raises(ValueError, match=f"TCP {gateway_port}"):
            policy_specs({}, [scope(NetworkAccess(mesh=True, egress=(wrong_port,)))], mesh_peers={"west": peer})
    transport = policies["NetworkPolicy"]["ingress"][0]["from"][0]
    assert ("podSelector" in transport) is (mode == "Gateway")
    local_parent = NetworkAccess(mesh=True, allowWithin=True, allowDNS=False)
    assert traffic([scope(local_parent), scope(access)], "ingress") == []
    with pytest.raises(ValueError, match="not registered"):
        policy_specs({}, [scope(access)])
    with pytest.raises(ValueError, match="selectors"):
        NetworkPeer(cluster="west", namespace="workflows")
    with pytest.raises(ValueError, match="source principals"):
        NetworkAccess(mesh=True, ingress=(egress,))


@pytest.mark.parametrize("port", [0, 65536, True, 15443.5])
def test_invalid_remote_gateway_ports(port):
    """
    Reject unusable tunnel destinations independently of Helm validation.
    """
    with pytest.raises(ValueError, match="gateway ports"):
        MeshPeer("west", "Gateway", ("192.0.2.1/32",), gatewayPort=port)


def test_observer_reads_never_authorize_or_execute_mutations():
    """
    Read replicas recompute topology, expose observation identity and reject mutation methods.
    """
    api = FakeAPI(resource("Graph", "pipeline", {"nodes": []}))
    result = asyncio.run(observe(api, "west", "test", "Graph", "pipeline"))
    assert result["cluster"] == "west" and result["uid"] == "uid-pipeline"
    assert result["metrics"]["topology"]["nodeCount"] == 0
    assert result["statusCurrent"] is False
    assert not api.calls
    app = build_app(lambda kind, name: result if name == "pipeline" else None, "reader-token")
    client = app.test_client()
    headers = {"Authorization": "Bearer reader-token"}
    assert client.get("/v1/observations/Graph/pipeline").status_code == 401
    response = client.get("/v1/observations/Graph/pipeline", headers=headers)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert client.post("/v1/observations/Graph/pipeline", headers=headers).status_code == 405
    assert client.get("/v1/observations/Graph/absent", headers=headers).status_code == 404
    from openapi_spec_validator import validate

    validate(client.get("/openapi.json", headers=headers).json)
    # No constructor or kubeconfig is needed: the write guard executes before transport.
    readonly = object.__new__(ObservationAPI)
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        with pytest.raises(ValueError, match="cannot mutate"):
            asyncio.run(readonly.request(method, "Graph", "test", "pipeline"))


def test_observer_rejects_graph_replacement_during_snapshot():
    """
    A deleted and recreated graph cannot share the first incarnation's observation.
    """
    from polyad.api.http.errors import Unavailable

    api = FakeAPI(resource("Graph", "pipeline", {"nodes": []}))
    original = api.get
    calls = 0

    async def changing(*args):
        nonlocal calls
        calls += 1
        value = await original(*args)
        if calls > 1:
            value = copy.deepcopy(value)
            value["metadata"]["uid"] = "replacement"
        return value

    api.get = changing
    with pytest.raises(Unavailable):
        asyncio.run(observe(api, "west", "test", "Graph", "pipeline"))


@pytest.mark.parametrize("invalid", ["exec", "tokenFile", "insecure", "http"])
def test_remote_credentials_reject_exec_files_and_unverified_transport(monkeypatch, invalid):
    """
    Projected configuration cannot execute programs, read arbitrary host files or disable TLS verification.
    """
    from pathlib import Path

    document = {
        "current-context": "west",
        "users": [{"name": "manager", "user": {"token": "test"}}],
        "clusters": [{"name": "west", "cluster": {"server": "https://west.example.com"}}],
    }
    if invalid in {"exec", "tokenFile"}:
        document["users"][0]["user"][invalid] = {"command": "should-never-run"} if invalid == "exec" else "/private/file"
    elif invalid == "insecure":
        document["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    else:
        document["clusters"][0]["cluster"]["server"] = "http://west.example.com"
    monkeypatch.setattr(Path, "read_bytes", lambda _: json.dumps(document).encode())
    federation = Federation(FakeAPI())
    federation.clusters = {"west": {"name": "west", "namespace": "test", "kubeconfigSecret": "west-access"}}
    with pytest.raises(ValueError, match="kubeconfig"):
        federation.target("west")


def test_remote_registration_is_opt_in_and_does_not_accept_local_identity(monkeypatch):
    """
    Disabled or malformed registrations never fall back to the local API.
    """
    with pytest.raises(ValueError, match="not registered"):
        Federation(FakeAPI()).target("west")
    monkeypatch.setenv("POLYAD_CLUSTER_NAME", "west")
    monkeypatch.setenv("POLYAD_FEDERATION_CLUSTERS", json.dumps([{"name": "west", "namespace": "test", "kubeconfigSecret": "access"}]))
    with pytest.raises(ValueError, match="distinct"):
        Federation(FakeAPI())


def test_remote_client_uses_isolated_credentials_retains_the_write_fence_and_rotates(monkeypatch):
    """
    Remote credentials cannot replace the local client, and projected Secret updates replace the remote transport.
    """
    from pathlib import Path

    from kubernetes.client import Configuration

    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "west",
        "contexts": [{"name": "west", "context": {"cluster": "west", "user": "manager"}}],
        "users": [{"name": "manager", "user": {"token": "first"}}],
        "clusters": [{"name": "west", "cluster": {"server": "https://west.example.com"}}],
    }
    monkeypatch.setattr(Path, "read_bytes", lambda _: json.dumps(document).encode())
    local_host = Configuration.get_default_copy().host
    local = FakeAPI()

    async def guard():
        return None

    local.before_write = guard
    federation = Federation(local)
    federation.clusters = {"west": {"name": "west", "namespace": "workflows", "kubeconfigSecret": "west-access"}}
    try:
        first, namespace = federation.target("west")
        assert namespace == "workflows" and first.before_write is guard
        assert first.client.configuration.host == "https://west.example.com"
        assert first.client.configuration.verify_ssl is True
        assert Configuration.get_default_copy().host == local_host
        assert federation.target("west")[0] is first
        document["users"][0]["user"]["token"] = "rotated"
        second, _ = federation.target("west")
        assert second is not first
        assert second.client.configuration.api_key["authorization"] == "Bearer rotated"
    finally:
        federation.close()
