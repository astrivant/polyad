"""
Exercise inherited discovery modes, exact-service negotiation and replay-aware client hooks.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from attrs import evolve

from polyad.api.connections.store import ConnectionSettings, ConnectionStore
from polyad.events.access import require_scope, scope_allows
from polyad.events.discovery import Directory
from polyad.events.store import EventStore
from polyad.events.visibility import public_observation
from polyad.exceptions.api import Conflict, Forbidden, Unavailable
from polyad.exceptions.reconciliation import Pending
from polyad.graph.service_connections import grants
from polyad.graph.temporary import entries
from polyad.operator.clusters.federation import INVENTORY, PARENT, REMOTE, Federation
from polyad.operator.policies.connections import reconcile_connection
from polyad.operator.reconciliation.controller import Controller
from polyad_sdk import Client
from polyad_sdk.events.filters import connection_pending, event_type, field, graph, phase
from polyad_sdk.exceptions.events import StreamInterrupted
from polyad_types import APIKey, ConnectionResponse, Event, GraphAccess, ServiceConnectionRequest, ServiceEndpoint
from polyad_types.api.auth import KeyDirection
from polyad_types.api.discovery import AccessMode, AtlasAccess, ServiceAccess
from polyad_types.networking.access import NetworkPort
from polyad_types.resources import GROUP
from polyad_types.serialization import converter
from tests.test_operator import resource, template
from tests.test_temporary_connections import ConnectionAPI, graph_fixture, participant


def identity(name, cluster="west"):
    """
    Construct a canonical graph identity.
    """
    return {"name": name, "namespace": "test", "kind": "Graph", "uid": "uid-" + name, "cluster": cluster}


@pytest.mark.parametrize(
    "mode,allowed", [("Disabled", []), ("SameGraph", [0]), ("GraphTree", [0, 1]), ("Cluster", [0, 1, 2]), ("Atlas", [0, 1, 2, 3])]
)
def test_access_modes(mode, allowed):
    """
    Distinguish all scopes without granting cross-cluster tree access implicitly.
    """
    home = [identity("a"), identity("parent")]
    paths = [home, [identity("b"), identity("parent")], [identity("other")], [identity("far", "east"), identity("parent")]]
    assert [i for i, path in enumerate(paths) if scope_allows(AccessMode(mode), home, path)] == allowed


def test_child_cannot_widen_or_forward_to_parent(monkeypatch):
    """
    Intersect parent, child and serving operator ceilings.
    """
    policy = AtlasAccess(
        AccessMode.ATLAS,
        AccessMode.ATLAS,
        {
            "region": ServiceAccess(AccessMode.GRAPH_TREE, AccessMode.CLUSTER),
            "west": ServiceAccess(AccessMode.ATLAS, AccessMode.ATLAS, "region"),
        },
    )
    assert policy.effective("west", "discovery") == AccessMode.GRAPH_TREE
    assert policy.effective("west", "connections") == AccessMode.CLUSTER
    monkeypatch.setenv("POLYAD_CLUSTER_NAME", "west")
    with pytest.raises(Forbidden, match="GraphTree"):
        require_scope("discovery", [identity("home", "root")], [identity("peer", "east")], policy=policy)
    with pytest.raises(ValueError, match="cycle"):
        AtlasAccess(clusters={"a": ServiceAccess(parent="b"), "b": ServiceAccess(parent="a")})
    with pytest.raises(ValueError, match="not configured"):
        AtlasAccess(clusters={"a": ServiceAccess(parent="missing")})


def test_filters_and_hook_checkpoint_failures():
    """
    Checkpoint only successful dispatch and skip retained successful callbacks on replay.
    """
    event = Event(
        "1-0",
        "topology",
        {"graph": identity("processor"), "nodes": [{"name": "worker-1"}], "status": {"phase": "Running"}, "null": None, "count": 1},
    )
    match = event_type("topology") & graph(name="processor") & phase("Running") & field("nodes.*.name", regex=r"^worker-\d+$")
    assert match(event)
    assert not field("missing")(event) and field("null")(event)
    assert field("missing", exists=False)(event) and not field("count", equals=True)(event)
    assert (~event_type("connection") | field("absent"))(event)
    calls = []
    subscription = Client("https://root", "key").subscribe(cursor="0-0")
    subscription.on(match, lambda value: calls.append(value.id))
    fail = [True]

    def second(value):
        if fail.pop() if fail else False:
            raise RuntimeError("application handler failed")
        calls.append("second")

    subscription.on(match, second)
    with pytest.raises(RuntimeError):
        subscription.dispatch(event)
    assert subscription.cursor == "0-0" and calls == ["1-0"]
    subscription.dispatch(event)
    assert subscription.cursor == "1-0" and calls == ["1-0", "second"]
    subscription.dispatch(event)
    assert calls == ["1-0", "second"]
    assert subscription.request_id(event, "join") == subscription.request_id(event, "join")
    with pytest.raises(StreamInterrupted):
        subscription.dispatch(Event("", "reset", {}))
    assert subscription.cursor == "1-0"


def setup_atlas(monkeypatch):
    """
    Build two isolated workload clusters under one root application PolyGraph.
    """
    monkeypatch.setenv("POLYAD_CLUSTER_NAME", "management")
    monkeypatch.setenv("POLYAD_ROOT_ENABLED", "true")
    monkeypatch.setenv("POLYAD_EVENTS_ENABLED", "true")
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")
    monkeypatch.setenv("POLYAD_MESH_ENABLED", "true")
    monkeypatch.setenv(
        "POLYAD_SERVICE_ACCESS",
        json.dumps({"discovery": "Atlas", "connections": "Atlas", "trustDomains": {"west": "west.test", "east": "east.test"}}),
    )
    monkeypatch.setenv(
        "POLYAD_FEDERATION_CLUSTERS",
        json.dumps([{"name": name, "namespace": "test", "kubeconfigSecret": name + "-access"} for name in ("west", "east")]),
    )
    monkeypatch.setenv(
        "POLYAD_ROOT_MESH_PEERS",
        json.dumps([{"name": name, "mode": "Gateway", "cidrs": [f"192.0.2.{i}/32"]} for i, name in enumerate(("west", "east"), start=1)]),
    )
    root = resource(
        "PolyGraph",
        "application",
        {"nodes": [{"name": name, "kind": "Graph", "ref": "leaf", "cluster": name} for name in ("west", "east")]},
    )
    leaf, definitions = graph_fixture()
    leaf["metadata"].update(name="leaf", uid="uid-leaf")
    leaf["spec"]["templateOnly"] = True
    root_api = ConnectionAPI(root, leaf, *definitions)
    apis, graphs, callers = {}, {}, {}
    journal = []
    for name in ("west", "east"):
        graph_obj, definitions = graph_fixture()
        graph_obj["metadata"].update(
            name=name,
            uid="uid-" + name,
            labels={f"{GROUP}/node": name},
            annotations={
                REMOTE: name,
                PARENT: json.dumps(
                    ["management", "test", "PolyGraph", "application", root["metadata"]["uid"], name], separators=(",", ":")
                ),
            },
        )
        graph_obj["spec"]["network"]["mesh"] = True
        api = ConnectionAPI(graph_obj, *definitions)
        callers[name] = evolve(participant(api, graph_obj, "a"), cluster=name)
        deployment = resource("Deployment", name + "-worker", {"template": template(daemon=True), "replicas": 1})
        deployment["apiVersion"] = "apps/v1"
        deployment["metadata"].update(
            labels={f"{GROUP}/node": "a"},
            ownerReferences=[
                {
                    "apiVersion": graph_obj["apiVersion"],
                    "kind": "Graph",
                    "name": name,
                    "uid": graph_obj["metadata"]["uid"],
                    "controller": True,
                }
            ],
        )
        api.objects["Deployment", "test", deployment["metadata"]["name"]] = deployment
        apis[name], graphs[name] = api, graph_obj
        journal.append({"cluster": name, "namespace": "test", "kind": "Graph", "name": name, "node": name})
    root_api.objects["PolyGraph", "test", "application"]["metadata"]["annotations"] = {INVENTORY: json.dumps(journal)}
    federation = Federation(root_api)
    federation.resolver = lambda name: (apis[name], "test")
    store = ConnectionStore(root_api, ConnectionSettings("test"))
    store.__dict__["federation"] = federation
    controller = Controller(root_api)
    controller.federation = federation
    request = ServiceConnectionRequest(
        "join", *(ServiceEndpoint(name, "test", "Graph", name, "uid-" + name, "a") for name in ("west", "east")), 300, (NetworkPort(8080),)
    )
    return store, controller, request, apis, graphs, callers


async def settle_receipt(controller, name):
    """
    Retry until both clusters acknowledge their policy mutations.
    """
    for _ in range(12):
        receipt = await controller.api.get("TemporaryConnection", "test", name)
        try:
            await reconcile_connection(controller, receipt)
            return await controller.api.get("TemporaryConnection", "test", name)
        except Pending:
            pass
    pytest.fail("cross-cluster receipt did not settle")


def test_cross_cluster_consent_policies_and_expiry(monkeypatch):
    """
    Require consent in both home clusters and remove policies on the original deadline.
    """
    store, controller, request, apis, graphs, callers = setup_atlas(monkeypatch)

    async def run():
        receipt = await store.connect_services(request, callers["west"])
        assert receipt["consent"] == {"west": "Approve"}
        pending = await settle_receipt(controller, receipt["name"])
        assert pending["status"]["phase"] == "Pending"
        assert not grants(apis["west"].objects["Graph", "test", "west"])
        await store.respond("test", receipt["name"], ConnectionResponse(receipt["uid"], "Approve"), callers["east"])
        active = await settle_receipt(controller, receipt["name"])
        assert active["status"]["phase"] == "Active", active.get("status")
        root = controller.api.objects["PolyGraph", "test", "application"]
        assert entries(root)[receipt["uid"]]["ports"] == []
        west = grants(apis["west"].objects["Graph", "test", "west"])[receipt["uid"]]
        east = grants(apis["east"].objects["Graph", "test", "east"])[receipt["uid"]]
        assert west["egress"][0]["ports"] == [{"port": 15443, "protocol": "TCP"}]
        assert east["ingress"][0]["ports"] == [{"port": 8080, "protocol": "TCP"}]
        assert east["ingress"][0]["principals"] == ["west.test/ns/test/sa/participant-west-a"]
        assert all(review["resourceAttributes"]["name"] == name for name, api in apis.items() for review in api.reviews)
        stored = controller.api.objects["TemporaryConnection", "test", receipt["name"]]
        stored["metadata"]["creationTimestamp"] = (datetime.now(UTC) - timedelta(seconds=301)).isoformat()
        expired = await settle_receipt(controller, receipt["name"])
        assert expired["status"]["phase"] == "Expired"
        assert not entries(controller.api.objects["PolyGraph", "test", "application"])
        for name, api in apis.items():
            assert not grants(api.objects["Graph", "test", name], active=False)
            for (kind, _, _), obj in api.objects.items():
                if kind == "AuthorizationPolicy":
                    assert "west.test/ns" not in json.dumps(obj)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["mode", "capability", "ownership", "identity", "common-owner"])
def test_children_reject_unfulfillable_proposals_without_receipts(monkeypatch, failure):
    """
    Reject unsupported mode, ownership and transport before writing intent.
    """
    store, controller, request, apis, graphs, callers = setup_atlas(monkeypatch)

    async def run():
        if failure == "mode":
            monkeypatch.setenv(
                "POLYAD_SERVICE_ACCESS", json.dumps({"connections": "Atlas", "clusters": {"east": {"connections": "Cluster"}}})
            )
            expected = Forbidden
        elif failure == "capability":
            monkeypatch.setenv("POLYAD_ROOT_MESH_PEERS", "[]")
            expected = Unavailable
        elif failure == "ownership":
            controller.api.objects["PolyGraph", "test", "application"]["metadata"]["annotations"][INVENTORY] = "[]"
            expected = Conflict
        elif failure == "identity":
            callers["west"] = evolve(callers["east"], cluster="west")
            expected = Forbidden
        else:
            store.api = apis["west"]
            store.resolve = lambda name: (controller.api, "test") if name == "management" else (apis[name], "test")
            expected = Unavailable
        with pytest.raises(expected):
            await store.connect_services(request, callers["west"])
        assert not any(kind == "TemporaryConnection" for kind, _, _ in controller.api.objects)
        assert not any(grants(api.objects["Graph", "test", name]) for name, api in apis.items())

    asyncio.run(run())


def test_discovery_permissions_live_uids_cursors_and_proposal_projection(monkeypatch):
    """
    Fence live discovery and deliver proposals to exact participant graph streams.
    """
    store, controller, request, apis, graphs, callers = setup_atlas(monkeypatch)
    streams = {name: SimpleNamespace(cursor=AsyncMock(return_value="11-0")) for name in ("management", "west", "east")}
    directory = Directory(controller.api, "test", store.federation, streams)
    key = APIKey(
        "caller",
        KeyDirection.INBOUND,
        "key",
        endpoints=("discovery", "events"),
        home=GraphAccess("west", "test", cluster="west"),
        graphs=(GraphAccess("application", "test", kind="PolyGraph", cluster="management"),),
    )

    async def run():
        target = GraphAccess("east", "test", cluster="east", uid="uid-east")
        result = await directory.discover(key, target)
        assert result["cursors"] == {"east": "11-0"} and len(result["services"]) == 2
        assert "template" not in json.dumps(result) and "serviceAccount" not in json.dumps(result)
        branches = await directory.discover(key, key.graphs[0])
        assert {item["cluster"] for item in branches["children"]} == {"west", "east"}
        with pytest.raises(Forbidden):
            await directory.discover(evolve(key, graphs=()), target)
        with pytest.raises(KeyError):
            await directory.discover(key, evolve(target, uid="old-uid"))
        streams.pop("east")
        with pytest.raises(Unavailable):
            await directory.discover(key, target)
        receipt = await store.connect_services(request, callers["west"])
        stored = await controller.api.get("TemporaryConnection", "test", receipt["name"])
        events = EventStore("redis://localhost", "test", visible=lambda obj: public_observation(apis["east"], obj), cluster="east")
        events.cache.client = AsyncMock()
        try:
            await events.publish_connection(stored, graphs["east"], "target")
            payload = json.loads(events.cache.client.eval.await_args.args[-2])
            assert payload["graph"]["name"] == "east"
            assert "system:serviceaccount" not in json.dumps(payload)
            assert connection_pending("a")(Event("12-0", "connection", payload))
            payload["connection"]["consent"]["east"] = "Approve"
            assert not connection_pending("a")(Event("12-0", "connection", payload))
        finally:
            await events.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["mode", "removed", "rule"])
def test_active_connections_revoke_when_child_state_cannot_fulfill_contract(monkeypatch, change):
    """
    Narrowed modes, removed endpoints and new restrictive rules revoke both grants.
    """
    store, controller, request, apis, _, callers = setup_atlas(monkeypatch)

    async def run():
        receipt = await store.connect_services(request, callers["west"])
        await store.respond("test", receipt["name"], ConnectionResponse(receipt["uid"], "Approve"), callers["east"])
        assert (await settle_receipt(controller, receipt["name"]))["status"]["phase"] == "Active"
        if change == "mode":
            policy = json.loads(os.environ["POLYAD_SERVICE_ACCESS"])
            policy["clusters"] = {"east": {"connections": "SameGraph"}}
            monkeypatch.setenv("POLYAD_SERVICE_ACCESS", json.dumps(policy))
        elif change == "removed":
            apis["east"].objects["Graph", "test", "east"]["spec"]["nodes"] = []
        else:
            apis["east"].objects["GraphRule", "test", "deny"] = resource(
                "GraphRule",
                "deny",
                {
                    "enforcement": "Namespace",
                    "network": {"allowWithin": False, "allowDNS": False, "mesh": True},
                },
            )
        terminal = await settle_receipt(controller, receipt["name"])
        assert terminal["status"]["phase"] == "Rejected", terminal.get("status")
        for name, api in apis.items():
            assert not grants(api.objects["Graph", "test", name], active=False)

    asyncio.run(run())


def test_approval_rechecks_mode_and_reject_remains_available(monkeypatch):
    """
    Do not consume positive consent after the child narrows scope; preserve explicit refusal.
    """
    store, controller, request, apis, _, callers = setup_atlas(monkeypatch)

    async def run():
        receipt = await store.connect_services(request, callers["west"])
        monkeypatch.setenv("POLYAD_SERVICE_ACCESS", '{"connections":"SameGraph"}')
        with pytest.raises(Forbidden):
            await store.respond("test", receipt["name"], ConnectionResponse(receipt["uid"], "Approve"), callers["east"])
        result = await store.respond("test", receipt["name"], ConnectionResponse(receipt["uid"], "Reject"), callers["east"])
        assert result["consent"]["east"] == "Reject"

    asyncio.run(run())


def test_unknown_discovery_mode_and_missing_home_reject_stream_intake(monkeypatch):
    """
    Reject before creating a subscription or reading event data.
    """
    store, controller, _, _, _, _ = setup_atlas(monkeypatch)
    directory = Directory(controller.api, "test", store.federation, {"management": SimpleNamespace(), "west": SimpleNamespace()})
    key = APIKey("service", KeyDirection.INBOUND, "key", endpoints=("events",))

    async def run():
        with pytest.raises(Forbidden, match="home"):
            await directory.authorize_stream(key, "west")
        with pytest.raises(Unavailable):
            await directory.authorize_stream(key, "not-registered")
        monkeypatch.setenv("POLYAD_SERVICE_ACCESS", '{"discovery":"Disabled"}')
        with pytest.raises(Forbidden, match="disabled"):
            await directory.authorize_stream(None, None)

    asyncio.run(run())


def test_service_client_uses_issuer_header_and_rotating_tokens(monkeypatch):
    """
    Select the issuer explicitly and re-read projected tokens without implicit mutation retries.
    """
    import io
    from unittest.mock import Mock

    store, _, request, _, _, _ = setup_atlas(monkeypatch)
    tokens = iter(["first", "second"])
    client = Client("https://root", None, identity_cluster="west", token_provider=lambda: next(tokens))
    client._opener = Mock()
    client._opener.open.side_effect = [io.BytesIO(b'{"status":"Pending"}'), io.BytesIO(b'{"status":"Pending"}')]
    client.connect_services(request)
    client.connect_services(request)
    calls = client._opener.open.call_args_list
    assert [call.args[0].get_header("Authorization") for call in calls] == ["Bearer first", "Bearer second"]
    assert all(call.args[0].get_header("X-polyad-cluster") == "west" for call in calls)
    assert all(call.args[0].selector == "/v1/connections/atlas" for call in calls)
    assert json.loads(calls[0].args[0].data)["requestId"] == "join"


@pytest.mark.parametrize("child,parent,allowed", [("Atlas", "Cluster", False), ("SameGraph", "Atlas", True), ("Atlas", "Atlas", True)])
def test_worker_policy_cannot_bypass_changed_parent(monkeypatch, child, parent, allowed):
    """
    Check the live root ceiling even when a Helm worker still has an older environment.
    """
    from polyad.events.access import parent_allows_worker

    monkeypatch.setenv("POLYAD_ROOT_WORKER", "true")
    monkeypatch.setenv("POLYAD_ROOT_DEPLOYMENT", "operator")
    monkeypatch.setenv("POLYAD_SERVICE_ACCESS", json.dumps({"discovery": child, "connections": child}))
    root = resource(
        "Deployment",
        "operator",
        {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "operator",
                            "env": [{"name": "POLYAD_SERVICE_ACCESS", "value": json.dumps({"discovery": parent, "connections": parent})}],
                        }
                    ]
                }
            }
        },
    )
    api = ConnectionAPI(root)
    assert asyncio.run(parent_allows_worker(api, "test")) is allowed
    api.objects.clear()
    assert not asyncio.run(parent_allows_worker(api, "test"))


@pytest.mark.parametrize("profile", [None, "values-ha.reference.yaml", "values-components.reference.yaml", "values-worker.reference.yaml"])
def test_access_configuration_reaches_every_operator_container(profile):
    """
    Preserve typed access ceilings through dense, split and manual worker deployments.
    """
    from tests.test_chart import CHART, render

    objects = render(
        "operator.serviceAccess.discovery=Atlas",
        "operator.serviceAccess.connections=Cluster",
        "operator.serviceAccess.clusters.west.discovery=SameGraph",
        *(("federation.clusters[0].namespace=test",) if profile == "values-worker.reference.yaml" else ()),
        values_files=(CHART / "references" / profile,) if profile else (),
    )
    policies = []
    for obj in objects:
        if obj["kind"] not in {"Deployment", "Daemon"} or "template" not in obj["spec"]:
            continue
        for container in obj["spec"]["template"]["spec"]["containers"]:
            if container["name"] == "operator":
                policies.extend(json.loads(item["value"]) for item in container["env"] if item["name"] == "POLYAD_SERVICE_ACCESS")
    assert policies
    for policy in policies:
        typed = converter.structure(policy, AtlasAccess)
        assert typed.effective("west", "discovery") == AccessMode.SAME_GRAPH
        assert typed.connections == AccessMode.CLUSTER


def test_destination_discovery_mode_denies_atlas_read_and_stream(monkeypatch):
    """
    A wide caller grant cannot make a child expose data outside its own mode.
    """
    store, controller, _, _, _, _ = setup_atlas(monkeypatch)
    policy = json.loads(os.environ["POLYAD_SERVICE_ACCESS"])
    policy["clusters"] = {"east": {"discovery": "Cluster"}}
    monkeypatch.setenv("POLYAD_SERVICE_ACCESS", json.dumps(policy))
    directory = Directory(controller.api, "test", store.federation, {name: SimpleNamespace() for name in ("management", "west", "east")})
    key = APIKey(
        "service",
        KeyDirection.INBOUND,
        "key",
        endpoints=("discovery", "events"),
        home=GraphAccess("west", "test", cluster="west"),
        graphs=(GraphAccess("east", "test", cluster="east"),),
    )

    async def run():
        with pytest.raises(Forbidden, match="Cluster"):
            await directory.discover(key, key.graphs[0])
        with pytest.raises(Forbidden, match="Cluster"):
            await directory.authorize_stream(key, "east")

    asyncio.run(run())


def test_discovery_client_bounds_breadth_and_deduplicates_cycles():
    """
    Bound queued graph identities as well as reads while safely skipping denied branches.
    """
    from unittest.mock import Mock

    a, b = identity("a"), identity("b")
    client = Client("https://root", "key")
    client.discover = Mock(
        side_effect=[
            {"roots": [a, a], "nextOffset": None},
            {"services": [{"name": "a"}], "children": [a, b]},
            {"services": [{"name": "b"}], "children": [a]},
        ]
    )
    assert list(client.services(max_graphs=2)) == [{"name": "a"}, {"name": "b"}]
    client.discover = Mock(return_value={"roots": [a, b], "nextOffset": None})
    with pytest.raises(ValueError, match="max_graphs"):
        list(client.services(max_graphs=1))
    assert client.discover.call_count == 1


@pytest.mark.parametrize("name", ["../secrets", "name/../../configmaps", "name?watch=1", "name%2fstatus", "name#fragment"])
def test_discovery_graph_names_cannot_change_kubernetes_request_paths(name):
    """
    Validate public graph addresses before resolving a privileged Kubernetes transport.
    """
    with pytest.raises(ValueError, match="resource name"):
        GraphAccess(name, "test")


@pytest.mark.parametrize(
    "mode,allowed", [("Disabled", False), ("SameGraph", False), ("GraphTree", False), ("Cluster", True), ("Atlas", True)]
)
def test_namespace_credentials_cannot_bypass_home_relative_modes(monkeypatch, mode, allowed):
    """
    A credential without a home cannot fulfill a home-relative discovery contract.
    """
    monkeypatch.setenv("POLYAD_SERVICE_ACCESS", json.dumps({"discovery": mode}))
    api = ConnectionAPI()
    directory = Directory(api, "test", Federation(api), {})
    if allowed:
        asyncio.run(directory.authorize_stream(None, None))
    else:
        with pytest.raises(Forbidden):
            asyncio.run(directory.authorize_stream(None, None))


def test_portless_service_edges_do_not_grant_unrestricted_transport(monkeypatch):
    """
    Preserve structural-only semantics instead of interpreting an empty port list as every port.
    """
    from polyad.operator.policies.service_connections import policies

    store, controller, request, _, _, callers = setup_atlas(monkeypatch)

    async def run():
        receipt = await store.connect_services(request, callers["west"])
        stored = await controller.api.get("TemporaryConnection", "test", receipt["name"])
        stored["spec"]["ports"] = []
        stored["spec"]["peers"]["target"]["cluster"] = "west"
        grants = policies(stored)
        assert all(not grant["ingress"] and not grant["egress"] for grant in grants.values())

    asyncio.run(run())
