"""
Verify graph isolation, explicit inheritance and ordered network admission.
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from polyad.compiler.passes.network import NetworkScope, configure_pod, policy_specs, scope_label, traffic
from polyad.exceptions.policies import RuleViolation
from polyad.exceptions.reconciliation import Pending
from polyad.graph import NetworkAccess, NetworkPeer, NetworkPort, TrafficRule
from polyad.operator.policies.network import context
from polyad.operator.reconciliation.controller import Controller
from polyad_types import resources as asts
from tests.test_operator import FakeAPI, resource, template


def scope(access, name="root", branch="server"):
    """
    Build a resolved graph scope for a direct workload.
    """
    return NetworkScope("test", "Graph", name, branch, access)


def test_ancestor_constraints_are_intersected_not_unioned():
    """
    A child can narrow ports but cannot authorize peers denied by its ancestor.
    """
    peer = NetworkPeer(namespace="clients", podLabels={"app": "reader"})
    parent = NetworkAccess(allowWithin=False, ingress=(TrafficRule(peer, ports=(NetworkPort(443),)),))
    child = NetworkAccess(
        allowWithin=False,
        ingress=(TrafficRule(peer, ports=(NetworkPort(80), NetworkPort(443))), TrafficRule(NetworkPeer(namespace="other"))),
    )
    effective = traffic([scope(parent), scope(child, "child")], "ingress")
    assert len(effective) == 1
    assert effective[0]["namespace"] == "clients"
    assert effective[0]["ports"] == [("TCP", 443)]
    policy = policy_specs({"owner": "one"}, [scope(parent), scope(child)])["NetworkPolicy"]
    match = policy["ingress"][0]["from"][0]
    assert match == {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "clients"}},
        "podSelector": {"matchLabels": {"app": "reader"}},
    }


def test_http_identity_authorization_and_control_plane_access():
    """
    Compile exact principal, method and path checks plus mTLS and narrow Istiod egress.
    """
    access = NetworkAccess(
        mesh=True,
        allowWithin=False,
        ingress=(
            TrafficRule(
                NetworkPeer(namespace="clients"),
                ports=(NetworkPort(8080),),
                methods=("GET",),
                paths=("/status",),
                principals=("cluster.local/ns/clients/sa/reader",),
            ),
        ),
    )
    specs = policy_specs({"node": "server"}, [scope(access)], mesh_namespace="control")
    assert specs["PeerAuthentication"]["mtls"] == {"mode": "STRICT"}
    rule = specs["AuthorizationPolicy"]["rules"][0]
    assert rule["from"][0]["source"] == {"namespaces": ["clients"], "principals": ["cluster.local/ns/clients/sa/reader"]}
    assert rule["to"][0]["operation"] == {"methods": ["GET"], "paths": ["/status"], "ports": ["8080"]}
    assert specs["NetworkPolicy"]["egress"][-1]["ports"] == [{"protocol": "TCP", "port": 15012}]
    with pytest.raises(ValueError, match="destination"):
        NetworkAccess(mesh=True, egress=access.ingress)
    with pytest.raises(ValueError, match="require"):
        NetworkAccess(ingress=access.ingress)


def test_connection_grants_select_graph_subtrees():
    """
    Transport edges authorize the matching descendant graph identity in each direction.
    """
    access = NetworkAccess(allowWithin=False, allowDNS=False)
    resolved = NetworkScope("test", "PolyGraph", "pipeline", "client", access, (("client", "server", (NetworkPort(8080),)),))
    egress = traffic([resolved], "egress")
    assert egress[0]["labels"] == {scope_label("test", "PolyGraph", "pipeline", "server"): "true"}
    assert traffic([resolved], "ingress") == []


@pytest.mark.parametrize("port", [50051, 5672, 5671, 6379, 9000])
def test_workload_tcp_protocols_compile_without_http_restrictions(port):
    """
    gRPC, brokers and raw sockets retain exact port and identity gates without invented HTTP filters.
    """
    access = NetworkAccess(
        mesh=True,
        allowWithin=False,
        ingress=(
            TrafficRule(NetworkPeer(namespace="clients"), ports=(NetworkPort(port),), principals=("cluster.local/ns/clients/sa/worker",)),
        ),
    )
    specs = policy_specs({"node": "server"}, [scope(access)])
    assert specs["PeerAuthentication"]["mtls"] == {"mode": "STRICT"}
    rule = specs["AuthorizationPolicy"]["rules"][0]
    assert rule["to"][0]["operation"] == {"ports": [str(port)]}
    assert rule["from"][0]["source"]["principals"] == ["cluster.local/ns/clients/sa/worker"]
    assert specs["NetworkPolicy"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": port}]


@pytest.mark.parametrize("boundary", [False, True])
def test_owner_chain_resolves_scope_and_labels(boundary):
    """
    Boundary scope stops at direct nodes while subtree scope survives real owner traversal.
    """

    async def run():
        parent = resource(
            "PolyGraph",
            "root",
            {
                "nodes": [{"name": "nested", "kind": "Graph", "ref": "definition"}],
                "network": {"scope": "Boundary" if boundary else "Subtree", "allowWithin": False},
            },
        )
        controller = Controller(FakeAPI(parent))
        child = asts.to_document(
            controller.child(parent, "nested", "Graph", {"nodes": [{"name": "work", "kind": "Workload", "ref": "job"}]})
        )
        child["metadata"].update(uid="child-uid", resourceVersion="1")
        labels, scopes = await context(controller.api, child, "work")
        assert labels[scope_label("test", "PolyGraph", "root", "nested")] == "true"
        assert len(scopes) == (0 if boundary else 1)
        parent_key = ("PolyGraph", "test", "root")
        controller.api.objects[parent_key]["metadata"]["uid"] = "replaced"
        with pytest.raises(Pending, match="owner"):
            await context(controller.api, child, "work")

    asyncio.run(run())


def test_policy_acknowledgement_precedes_admission_and_updates_do_not_delete():
    """
    Observe guards before pod creation and retain them until work cleanup is observed.
    """

    async def run():
        graph = resource(
            "Graph", "root", {"nodes": [{"name": "work", "kind": "Workload", "ref": "job"}], "network": {"allowWithin": False}}
        )
        api = FakeAPI(graph, resource("Workload", "job", {"template": template()}))
        controller = Controller(api)
        with pytest.raises(Pending, match="guards persisted"):
            await controller.reconcile(("Graph", "test", "root"))
        assert api.children("NetworkPolicy") and not api.children("Job")
        await controller.reconcile(("Graph", "test", "root"))
        assert len(api.children("Job")) == 1
        stored = api.objects[("Graph", "test", "root")]
        stored["spec"]["network"]["allowDNS"] = False
        with pytest.raises(Pending, match="guards persisted"):
            await controller.reconcile(("Graph", "test", "root"))
        assert any(method == "PUT" and kind == "NetworkPolicy" for method, kind, _ in api.calls)
        assert not any(method == "DELETE" for method, _, _ in api.calls)
        await controller.drain(stored)
        assert api.children("Job")[0]["metadata"].get("deletionTimestamp")
        assert not api.children("NetworkPolicy")[0]["metadata"].get("deletionTimestamp")
        api.objects = {key: value for key, value in api.objects.items() if key[0] != "Job"}
        await controller.drain(stored)
        assert api.children("NetworkPolicy")[0]["metadata"].get("deletionTimestamp")

    asyncio.run(run())


def test_mesh_workload_admission_requires_actual_injection(monkeypatch):
    """
    Declaring mesh mode cannot admit an uninjected pod when the webhook is absent.
    """
    monkeypatch.setenv("POLYAD_MESH_ENABLED", "true")

    async def run():
        graph = resource("Graph", "root", {"nodes": [{"name": "work", "kind": "Workload", "ref": "job"}], "network": {"mesh": True}})
        api = FakeAPI(graph, resource("Workload", "job", {"template": template()}))
        controller = Controller(api)
        with pytest.raises(Pending):
            await controller.reconcile(("Graph", "test", "root"))
        with pytest.raises(ValueError, match="injection"):
            await controller.reconcile(("Graph", "test", "root"))
        assert not api.children("Job")

    asyncio.run(run())


@pytest.mark.parametrize("override", [{"hostNetwork": True}, {"containers": [{"securityContext": {"privileged": True}}]}])
def test_network_bypass_is_rejected(override):
    """
    Isolated workloads cannot request host networking or privileged containers.
    """
    pod = copy.deepcopy(template())
    pod["spec"].update(override)
    with pytest.raises(ValueError):
        configure_pod(pod, {}, isolated=True, mesh=False)


@pytest.mark.parametrize("scope_name", ["Boundary", "Subtree"])
def test_referenced_structural_rules_obey_their_scope(scope_name):
    """
    Boundary-scoped references do not silently constrain reusable descendants.
    """
    from polyad.operator.policies.rules import check_rules

    async def run():
        rule = resource("GraphRule", "one-node", {"enforcement": "Referenced", "scope": scope_name, "limits": {"nodes": 1}})
        child = resource(
            "Graph",
            "child",
            {
                "templateOnly": True,
                "nodes": [{"name": "a", "kind": "Workload", "ref": "job"}, {"name": "b", "kind": "Workload", "ref": "job"}],
            },
        )
        root = {"rules": ["one-node"], "nodes": [{"name": "child", "kind": "Graph", "ref": "child"}]}
        api = FakeAPI(rule, child)
        if scope_name == "Subtree":
            with pytest.raises(RuleViolation):
                await check_rules(api, "test", "Graph", root)
        else:
            assert (await check_rules(api, "test", "Graph", root))[0]["allowed"]

    asyncio.run(run())


def test_namespace_network_rule_cannot_be_opted_out_of():
    """
    A graph's broad local allowance is intersected with mandatory namespace denial.
    """

    async def run():
        rule = resource("GraphRule", "isolated", {"network": {"allowWithin": False}})
        graph = resource(
            "Graph", "root", {"nodes": [{"name": "a", "kind": "Workload", "ref": "job"}], "network": {"ingress": [{"peer": {}}]}}
        )
        _, scopes = await context(FakeAPI(rule), graph, "a")
        assert traffic(scopes, "ingress") == []

    asyncio.run(run())


def test_policy_names_cannot_impersonate_workload_nodes():
    """
    Native policies sharing a Kubernetes name never count as admitted graph nodes.
    """

    async def run():
        graph = resource(
            "Graph",
            "root",
            {
                "network": {},
                "nodes": [{"name": "work", "kind": "Workload", "ref": "job"}, {"name": "net-work", "kind": "Workload", "ref": "job"}],
            },
        )
        api = FakeAPI(graph, resource("Workload", "job", {"template": template()}))
        controller = Controller(api)
        with pytest.raises(Pending):
            await controller.reconcile(("Graph", "test", "root"))
        stored = api.objects[("Graph", "test", "root")]
        assert stored["status"]["metrics"]["execution"]["observedNodes"] == 0
        await controller.reconcile(("Graph", "test", "root"))
        assert len(api.children("Job")) == 2

    asyncio.run(run())
