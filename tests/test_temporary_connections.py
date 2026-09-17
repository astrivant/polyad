"""
Exercise authenticated intake and durable connection admission, expiry and retry fencing.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from attrs import evolve
from openapi_spec_validator import validate

from polyad.api.connections.app import build_app
from polyad.api.connections.store import AUDIENCE, FINALIZER, Caller, ConnectionSettings, ConnectionStore
from polyad.api.errors import Conflict, Forbidden, Unauthorized
from polyad.compiler.passes.network import NetworkScope, traffic
from polyad.events.topology import topology_snapshot
from polyad.graph import NetworkAccess
from polyad.graph.temporary import ANNOTATION, CLEANUP, deadline, entries, overlay
from polyad.operator.policies.network import context, ensure_policies
from polyad.operator.policies.rule_state import check_live_rules
from polyad.operator.policies.rules import RuleViolation
from polyad.operator.reconciliation.controller import Controller, Pending
from polyad_client import APIError, Client
from polyad_types import resources as asts
from polyad_types.codec import converter
from polyad_types.requests import ConnectionRequest
from polyad_types.topology import topology
from tests.test_client import Adapter
from tests.test_operator import FakeAPI, resource, template

CALLER = Caller("system:serviceaccount:test:worker", "caller-uid", "test", ("system:serviceaccounts",))


class ConnectionAPI(FakeAPI):
    """
    Supply Kubernetes authentication reviews, server timestamps and JSON merge patches.
    """

    def __init__(self, *objects):
        """
        Initialize independent authentication and authorization results.
        """
        super().__init__(*objects)
        self.caller = CALLER
        self.authenticated = self.allowed = True
        self.audiences = [AUDIENCE]
        self.reviews = []

    async def request(self, method, kind, namespace, name="", body=None, **kwargs):
        """
        Retain review attributes and emulate server-side receipt metadata.
        """
        if kind == "TokenReview":
            assert body["spec"]["audiences"] == [AUDIENCE]
            return {
                "status": {
                    "authenticated": self.authenticated,
                    "audiences": self.audiences,
                    "user": {"username": self.caller.username, "uid": self.caller.uid, "groups": list(self.caller.groups)},
                }
            }
        if kind == "SubjectAccessReview":
            self.reviews.append(copy.deepcopy(body["spec"]))
            return {"status": {"allowed": self.allowed}}
        body = asts.encode_body(body)
        if method == "POST":
            body["metadata"]["creationTimestamp"] = datetime.now(UTC).isoformat()
        if method == "PATCH" and "annotations" in body.get("metadata", {}):
            annotations = dict(self.objects[(kind, namespace, name)]["metadata"].get("annotations", {}))
            annotations.update(body["metadata"]["annotations"])
            body["metadata"]["annotations"] = {key: value for key, value in annotations.items() if value is not None}
        return await super().request(method, kind, namespace, name, body, **kwargs)


def graph_fixture(kind="Graph"):
    """
    Construct real boundary intent and reusable workload definitions.
    """
    graph = resource(
        "Graph",
        "root",
        {
            "mode": "persistent",
            "nodes": [{"name": name, "kind": "Daemon", "ref": "server"} for name in ("a", "b")],
            "network": {"allowWithin": False, "allowDNS": False},
        },
    )
    definitions = [resource("Daemon", "server", {"template": template(daemon=True)})]
    if kind == "PolyGraph":
        graph["kind"] = kind
        definitions.append(resource("Graph", "leaf", {"templateOnly": True, **copy.deepcopy(graph["spec"])}))
        graph["spec"]["nodes"] = [{"name": name, "kind": "Graph", "ref": "leaf"} for name in ("a", "b")]
    elif kind == "ReplicaGroup":
        graph["kind"] = kind
        graph["spec"] = {
            "replicas": 2,
            "template": {"kind": "Daemon", "ref": "server"},
            "network": {"allowWithin": False, "allowDNS": False},
        }
    return graph, definitions


def request_for(graph, **changes):
    """
    Request a real transport edge against an exact graph incarnation.
    """
    names = ("replica-0", "replica-1") if graph["kind"] == "ReplicaGroup" else ("a", "b")
    return converter.structure(
        {
            "requestId": "edge-one",
            "namespace": graph["metadata"]["namespace"],
            "kind": graph["kind"],
            "graph": graph["metadata"]["name"],
            "graphUid": graph["metadata"]["uid"],
            "source": names[0],
            "target": names[1],
            "ttlSeconds": 300,
            "ports": [{"port": 8080}],
            **changes,
        },
        ConnectionRequest,
    )


async def settle(controller, key):
    """
    Observe asynchronous policy writes before expecting terminal reconciliation.
    """
    for _ in range(12):
        try:
            await controller.reconcile(key)
            return
        except Pending:
            pass
    pytest.fail(f"reconciliation did not settle: {key}")


@pytest.mark.parametrize(
    "scope,configured,caller,target,allowed",
    [
        ("Cluster", "", "caller", "target", True),
        ("OperatorNamespace", "", "test", "test", True),
        ("OperatorNamespace", "", "other", "test", False),
        ("OperatorNamespace", "", "test", "other", False),
        ("Namespace", "chosen", "chosen", "chosen", True),
        ("Namespace", "chosen", "test", "chosen", False),
        ("Namespace", "chosen", "chosen", "test", False),
    ],
)
def test_scope_limits_verified_callers_and_targets(scope, configured, caller, target, allowed):
    """
    Restrict both sides independently and require graph-specific Kubernetes authorization.
    """

    async def run():
        api = ConnectionAPI()
        api.caller = evolve(CALLER, namespace=caller, username=f"system:serviceaccount:{caller}:worker")
        store = ConnectionStore(api, ConnectionSettings("test", scope, configured))

        async def invoke():
            identity = await store.authenticate("projected-token")
            await store.authorize(identity, target, "Graph", "root")

        if allowed:
            await invoke()
            assert api.reviews[-1]["resourceAttributes"] == {
                "namespace": target,
                "group": asts.GROUP,
                "resource": "graphs",
                "name": "root",
                "verb": "connect",
            }
        else:
            with pytest.raises(Forbidden):
                await invoke()
            assert not api.reviews

    asyncio.run(run())


@pytest.mark.parametrize("change", ["invalid", "audience", "human", "uid", "denied"])
def test_authentication_and_authorization_fail_closed(change):
    """
    Reject invalid tokens, wrong audiences, non-service-account identities and denied grants.
    """

    async def run():
        api = ConnectionAPI()
        if change == "invalid":
            api.authenticated = False
        elif change == "audience":
            api.audiences = ["kubernetes"]
        elif change == "human":
            api.caller = evolve(CALLER, username="admin")
        elif change == "uid":
            api.caller = evolve(CALLER, uid="")
        else:
            api.allowed = False
        store = ConnectionStore(api, ConnectionSettings("test"))
        with pytest.raises(Forbidden if change == "denied" else Unauthorized):
            caller = await store.authenticate("token")
            await store.authorize(caller, "test", "Graph", "root")
        assert not api.objects

    asyncio.run(run())


def test_intake_idempotency_ownership_ttl_and_graph_incarnation():
    """
    Replays retain their original deadline and cannot retarget an existing request.
    """

    async def run():
        graph, definitions = graph_fixture()
        api = ConnectionAPI(graph, *definitions)
        store = ConnectionStore(api, ConnectionSettings("test", max_ttl=600))
        request = request_for(graph)
        receipt = await store.submit(request, CALLER)
        assert receipt == await store.submit(request, CALLER)
        assert len(api.children("TemporaryConnection")) == 1
        with pytest.raises(Conflict):
            await store.submit(evolve(request, ttlSeconds=301), CALLER)
        with pytest.raises(ValueError, match="maximum"):
            await store.submit(evolve(request, requestId="too-long", ttlSeconds=601), CALLER)
        with pytest.raises(Conflict, match="incarnation"):
            await store.submit(evolve(request, requestId="stale", graphUid="old"), CALLER)
        with pytest.raises(ValueError, match="endpoints"):
            await store.submit(evolve(request, requestId="missing", target="absent"), CALLER)
        assert await store.lookup("test", request.requestId, evolve(CALLER, uid="new-service-account")) is None
        assert (await store.revoke("test", request.requestId, CALLER))["revokeRequested"]
        assert (await store.lookup("test", request.requestId, CALLER))["expiresAt"] == receipt["expiresAt"]
        assert not api.children("NetworkPolicy")

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["Graph", "PolyGraph", "ReplicaGroup"])
def test_admission_changes_neighbors_at_each_boundary(kind, monkeypatch):
    """
    Include accepted edges in effective topology without rewriting reusable specifications.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        graph, definitions = graph_fixture(kind)
        api = ConnectionAPI(graph, *definitions)
        before = await topology_snapshot(api, graph)
        receipt = await ConnectionStore(api, ConnectionSettings("test")).submit(request_for(graph), CALLER)
        controller = Controller(api)
        await settle(controller, ("TemporaryConnection", "test", receipt["name"]))
        stored = await api.get(kind, "test", "root")
        assert stored["spec"] == graph["spec"]
        snapshot = await topology_snapshot(api, stored)
        assert snapshot["revision"] != before["revision"]
        assert len(snapshot["connections"]) == 1
        assert api.children("TemporaryConnection")[0]["status"]["phase"] == "Active"
        assert len(entries(stored)) == 1

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["Graph", "PolyGraph", "ReplicaGroup"])
@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_graph_reconciliation_revokes_removed_connection_endpoints(kind, endpoint, monkeypatch):
    """
    Revoke removed logical nodes and scaled-in copies before the graph admits more work.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        graph, definitions = graph_fixture(kind)
        api = ConnectionAPI(graph, *definitions)
        controller = Controller(api)
        request = request_for(graph)
        receipt = await ConnectionStore(api, ConnectionSettings("test")).submit(request, CALLER)
        key = ("TemporaryConnection", "test", receipt["name"])
        await settle(controller, key)
        before = await topology_snapshot(api, await api.get(kind, "test", "root"))
        changed = await api.get(kind, "test", "root")
        if kind == "ReplicaGroup":
            changed["spec"]["replicas"] = 0 if endpoint == "source" else 1
        else:
            changed["spec"]["nodes"] = [node for node in changed["spec"]["nodes"] if node["name"] != getattr(request, endpoint)]
        await api.request("PUT", kind, "test", "root", changed)
        # Cleanup is independent of the listener's current admission setting.
        monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "false")
        await settle(controller, (kind, "test", "root"))
        current = await api.get(kind, "test", "root")
        assert entries(current) == {}
        assert CLEANUP not in current["metadata"]["annotations"]
        assert api.objects[key]["status"]["phase"] == "Revoked"
        assert "endpoint removed" in api.objects[key]["status"]["message"]
        after = await topology_snapshot(api, current)
        assert after["revision"] != before["revision"]
        assert after["connections"] == []
        current["spec"] = graph["spec"]
        await api.request("PUT", kind, "test", "root", current)
        await settle(Controller(api), key)
        assert entries(await api.get(kind, "test", "root")) == {}
        assert api.objects[key]["status"]["phase"] == "Revoked"

    asyncio.run(run())


def test_missing_or_unready_workload_keeps_logical_connection(monkeypatch):
    """
    Missing runtime objects and Pod readiness do not remove declared graph neighbors.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        graph, definitions = graph_fixture()
        api = ConnectionAPI(graph, *definitions)
        receipt = await ConnectionStore(api, ConnectionSettings("test")).submit(request_for(graph), CALLER)
        key = ("TemporaryConnection", "test", receipt["name"])
        controller = Controller(api)
        await settle(controller, key)
        assert not api.children("Deployment")
        await settle(controller, ("Graph", "test", "root"))
        child = api.children("Deployment")[0]
        api.objects.pop(("Deployment", "test", child["metadata"]["name"]))
        await settle(controller, ("Graph", "test", "root"))
        assert len(api.children("Deployment")) == 2
        assert api.objects[key]["status"]["phase"] == "Active"
        assert len(entries(await api.get("Graph", "test", "root"))) == 1

    asyncio.run(run())


def test_cleanup_uses_inherited_replica_count_and_waits_for_missing_source(monkeypatch):
    """
    Resolve shared replica intent without treating an unavailable source as scale-to-zero.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        graph, definitions = graph_fixture("ReplicaGroup")
        source = resource("ReplicaGroup", "pool", {**graph["spec"], "templateOnly": True})
        graph["spec"]["replicaSource"] = {"name": "pool", "uid": source["metadata"]["uid"]}
        api = ConnectionAPI(graph, source, *definitions)
        receipt = await ConnectionStore(api, ConnectionSettings("test")).submit(request_for(graph), CALLER)
        key = ("TemporaryConnection", "test", receipt["name"])
        controller = Controller(api)
        await settle(controller, key)
        api.objects.pop(("ReplicaGroup", "test", "pool"))
        with pytest.raises(Pending, match="source incarnation is unavailable"):
            await controller.reconcile(("ReplicaGroup", "test", "root"))
        assert len(entries(await api.get("ReplicaGroup", "test", "root"))) == 1
        assert api.objects[key]["status"]["phase"] == "Active"
        source["spec"]["replicas"] = 1
        api.objects[("ReplicaGroup", "test", "pool")] = source
        await settle(controller, ("ReplicaGroup", "test", "root"))
        assert api.objects[key]["status"]["phase"] == "Revoked"
        assert entries(await api.get("ReplicaGroup", "test", "root")) == {}

    asyncio.run(run())


def test_orphan_cleanup_preserves_other_receipts(monkeypatch):
    """
    Removing an orphaned grant cannot remove an overlapping grant with a live receipt.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        graph, definitions = graph_fixture()
        api = ConnectionAPI(graph, *definitions)
        store, controller = ConnectionStore(api, ConnectionSettings("test")), Controller(api)
        receipts = [await store.submit(request_for(graph, requestId=name), CALLER) for name in ("orphaned", "live")]
        for receipt in receipts:
            await settle(controller, ("TemporaryConnection", "test", receipt["name"]))
        before = await topology_snapshot(api, await api.get("Graph", "test", "root"))
        api.objects.pop(("TemporaryConnection", "test", receipts[0]["name"]))
        await settle(controller, ("Graph", "test", "root"))
        current = await api.get("Graph", "test", "root")
        assert set(entries(current)) == {receipts[1]["uid"]}
        after = await topology_snapshot(api, current)
        assert after["connections"] == before["connections"]
        assert api.children("TemporaryConnection")[0]["status"]["phase"] == "Active"

    asyncio.run(run())


@pytest.mark.parametrize("orphaned", [False, True])
def test_dead_connection_cleanup_survives_policy_failure_and_endpoint_return(orphaned, monkeypatch):
    """
    Retry policy removal after restart without reviving dead grants or losing static edges.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        graph, definitions = graph_fixture()
        graph["spec"]["connections"] = [{"source": "b", "target": "a", "ports": [{"port": 9090}]}]
        api = ConnectionAPI(graph, *definitions)
        controller = Controller(api)
        await settle(controller, ("Graph", "test", "root"))
        receipt = await ConnectionStore(api, ConnectionSettings("test")).submit(request_for(graph), CALLER)
        key = ("TemporaryConnection", "test", receipt["name"])
        await settle(controller, key)
        if orphaned:
            api.objects.pop(key)
        else:
            current = await api.get("Graph", "test", "root")
            current["spec"]["nodes"] = current["spec"]["nodes"][:1]
            current["spec"]["connections"] = []
            await api.request("PUT", "Graph", "test", "root", current)
        original = api.request

        async def failing(method, kind, *args, **kwargs):
            if method == "PUT" and kind == "NetworkPolicy":
                raise OSError("policy API unavailable")
            return await original(method, kind, *args, **kwargs)

        api.request = failing
        with pytest.raises(OSError):
            await controller.reconcile(("Graph", "test", "root"))
        current = await api.get("Graph", "test", "root")
        assert entries(current) == {}
        assert current["metadata"]["annotations"][CLEANUP] == "true"
        if not orphaned:
            assert api.objects[key]["status"]["phase"] == "Active"
            assert api.objects[key]["metadata"]["annotations"][f"{asts.GROUP}/revoke-requested"] == "true"
        current["spec"] = graph["spec"]
        api.request = original
        await api.request("PUT", "Graph", "test", "root", current)
        # Normal graph admission now fails, but cleanup must still finish.
        api.objects[("GraphRule", "test", "tight")] = resource("GraphRule", "tight", {"relation": "connections", "cheeger": {"minimum": 2}})
        restarted = Controller(api)
        for _ in range(12):
            try:
                await restarted.reconcile(("Graph", "test", "root"))
            except Pending:
                continue
            except RuleViolation:
                break
        else:
            pytest.fail("cleanup did not finish before graph rule validation")
        if not orphaned:
            await settle(restarted, key)
            assert api.objects[key]["status"]["phase"] == "Revoked"
        current = await api.get("Graph", "test", "root")
        assert entries(current) == {}
        assert CLEANUP not in current["metadata"]["annotations"]
        assert current["spec"]["connections"] == graph["spec"]["connections"]
        policy = next(p for p in api.children("NetworkPolicy") if p["metadata"]["labels"][f"{asts.GROUP}/node"] == "net-a")
        assert policy["spec"]["egress"] == []
        assert policy["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 9090}]

    asyncio.run(run())


def test_expiry_rebuilds_policies_after_restart_even_when_rules_block(monkeypatch):
    """
    Remove expiring grants despite minimum Cheeger rules while preserving static transport.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        graph, definitions = graph_fixture()
        graph["spec"]["connections"] = [{"source": "b", "target": "a", "ports": [{"port": 9090}]}]
        api = ConnectionAPI(graph, *definitions)
        controller = Controller(api)
        await settle(controller, ("Graph", "test", "root"))
        store = ConnectionStore(api, ConnectionSettings("test"))
        receipt = await store.submit(request_for(graph), CALLER)
        key = ("TemporaryConnection", "test", receipt["name"])
        await settle(controller, key)
        _, scopes = await context(api, await api.get("Graph", "test", "root"), "a")
        assert traffic(scopes, "egress")[0]["ports"] == [("TCP", 8080)]
        # Simulate restart past the immutable server timestamp deadline.
        stored = api.objects[key]
        stored["metadata"]["creationTimestamp"] = (datetime.now(UTC) - timedelta(seconds=301)).isoformat()
        target = api.objects[("Graph", "test", "root")]
        grants = entries(target)
        grants[stored["metadata"]["uid"]]["expiresAt"] = deadline(stored).isoformat()
        target["metadata"]["annotations"][ANNOTATION] = json.dumps(grants)
        # The static reverse edge still has h=1; demand h=2 so normal work is blocked.
        api.objects[("GraphRule", "test", "tight")] = resource("GraphRule", "tight", {"relation": "connections", "cheeger": {"minimum": 2}})
        with pytest.raises(RuleViolation):
            await check_live_rules(api, await api.get("Graph", "test", "root"))
        monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "false")
        await settle(Controller(api), key)
        assert api.objects[key]["status"]["phase"] == "Expired"
        target = await api.get("Graph", "test", "root")
        assert entries(target) == {}
        _, scopes = await context(api, target, "a")
        assert traffic(scopes, "egress") == []
        assert traffic(scopes, "ingress")[0]["ports"] == [("TCP", 9090)]
        policy = next(p for p in api.children("NetworkPolicy") if p["metadata"]["labels"][f"{asts.GROUP}/node"] == "net-a")
        assert policy["spec"]["egress"] == []
        assert policy["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 9090}]

    asyncio.run(run())


def test_overlapping_grants_revoke_independently_and_release_finalizers(monkeypatch):
    """
    One revocation cannot delete a live duplicate or the static edge it overlaps.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        graph, definitions = graph_fixture()
        graph["spec"]["connections"] = [{"source": "a", "target": "b", "ports": [{"port": 8080}]}]
        api = ConnectionAPI(graph, *definitions)
        controller, store = Controller(api), ConnectionStore(api, ConnectionSettings("test"))
        receipts = [await store.submit(request_for(graph, requestId=name), CALLER) for name in ("one", "two")]
        for receipt in receipts:
            await settle(controller, ("TemporaryConnection", "test", receipt["name"]))
        first = ("TemporaryConnection", "test", receipts[0]["name"])
        await store.revoke("test", "one", CALLER)
        await settle(controller, first)
        target = await api.get("Graph", "test", "root")
        assert len(entries(target)) == 1
        assert len(topology(overlay(target, target["spec"])).connections) == 1
        assert api.objects[first]["status"]["phase"] == "Revoked"
        await api.delete(api.objects[first])
        await settle(controller, first)
        assert FINALIZER not in api.objects[first]["metadata"]["finalizers"]
        await store.revoke("test", "two", CALLER)
        await settle(controller, ("TemporaryConnection", "test", receipts[1]["name"]))
        target = await api.get("Graph", "test", "root")
        assert entries(target) == {}
        assert len(topology(overlay(target, target["spec"])).connections) == 1

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [True, False])
def test_admission_rejects_rules_or_disabled_feature_without_grant(enabled, monkeypatch):
    """
    Admit no edge when the namespace feature is off or a fresh connection rule rejects it.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", str(enabled).lower())

    async def run():
        graph, definitions = graph_fixture()
        rule = resource("GraphRule", "edges", {"relation": "connections", "limits": {"edges": 0}})
        api = ConnectionAPI(graph, *definitions, rule)
        receipt = await ConnectionStore(api, ConnectionSettings("test")).submit(request_for(graph), CALLER)
        await settle(Controller(api), ("TemporaryConnection", "test", receipt["name"]))
        assert api.children("TemporaryConnection")[0]["status"]["phase"] == "Rejected"
        assert entries(await api.get("Graph", "test", "root")) == {}

    asyncio.run(run())


def test_policy_write_rejects_expired_compilation():
    """
    A policy compiled before expiry cannot be dispatched after its grant deadline.
    """

    async def run():
        graph, _ = graph_fixture()
        api = ConnectionAPI(graph)
        scopes = [NetworkScope("test", "Graph", "root", "a", NetworkAccess(), expires_at=datetime.now(UTC) - timedelta(seconds=1))]
        with pytest.raises(Pending, match="expired"):
            await ensure_policies(Controller(api), graph, {"a": scopes})
        assert not api.children("NetworkPolicy")

    asyncio.run(run())


def test_http_schema_strict_input_and_standalone_client():
    """
    Expose authenticated POST, GET and DELETE with strict scalar types and valid OpenAPI.
    """
    graph, _ = graph_fixture()
    request = converter.unstructure(request_for(graph))
    calls = []

    def authenticate(token):
        if token != "projected":
            raise Unauthorized("invalid token")
        return CALLER

    app = build_app(
        authenticate,
        lambda value, caller: {"requestId": value.requestId},
        lambda ns, key, caller: {"requestId": key},
        lambda ns, key, caller: calls.append((ns, key)) or {"revokeRequested": True},
    )
    client = Client("http://connections:8093", "projected")
    adapter = Adapter(app)
    client._opener = adapter
    validate(client.openapi())
    assert client.connect(request) == {"requestId": "edge-one"}
    assert client.connection("test", "edge-one") == {"requestId": "edge-one"}
    assert client.disconnect("test", "edge-one") == {"revokeRequested": True}
    assert calls == [("test", "edge-one")]
    http = app.test_client()
    headers = {"Authorization": "Bearer projected"}
    assert http.post("/v1/connections", json=request).status_code == 401
    for changes in (
        {"ttlSeconds": True},
        {"ttlSeconds": 1.5},
        {"ttlSeconds": "30"},
        {"bidirectional": "false"},
        {"ports": [{"port": True}]},
        {"unknown": True},
        {"target": "a"},
        {"ports": [{"port": 0}]},
    ):
        assert http.post("/v1/connections", json={**request, **changes}, headers=headers).status_code == 400
    assert http.post("/v1/connections", data="x" * 65537, content_type="application/json", headers=headers).status_code == 413


def test_ancestor_rule_blocks_descendant_admission(monkeypatch):
    """
    A child's temporary grant must satisfy referenced Subtree rules on its actual parent.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        leaf, definitions = graph_fixture()
        leaf["metadata"]["name"] = "leaf"
        leaf["spec"]["templateOnly"] = True
        root = resource(
            "PolyGraph",
            "parent",
            {"mode": "persistent", "rules": ["no-edges"], "nodes": [{"name": "child", "kind": "Graph", "ref": "leaf"}]},
        )
        rule = resource(
            "GraphRule", "no-edges", {"enforcement": "Referenced", "scope": "Subtree", "relation": "connections", "limits": {"edges": 0}}
        )
        api = ConnectionAPI(root, leaf, rule, *definitions)
        controller = Controller(api)
        desired = controller.child(root, "child", "Graph", {**leaf["spec"], "templateOnly": False})
        child = await api.request("POST", "Graph", "test", body=desired)
        receipt = await ConnectionStore(api, ConnectionSettings("test")).submit(request_for(child), CALLER)
        await settle(controller, ("TemporaryConnection", "test", receipt["name"]))
        stored = api.children("TemporaryConnection")[0]
        assert stored["status"]["phase"] == "Rejected"
        assert "no-edges" in stored["status"]["message"]
        assert entries(await api.get("Graph", "test", child["metadata"]["name"])) == {}

    asyncio.run(run())


def test_partial_policy_failure_is_retried_before_revocation_finishes(monkeypatch):
    """
    Keep receipt cleanup pending after annotation removal if policy enforcement fails.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        graph, definitions = graph_fixture()
        api = ConnectionAPI(graph, *definitions)
        controller = Controller(api)
        await settle(controller, ("Graph", "test", "root"))
        store = ConnectionStore(api, ConnectionSettings("test"))
        receipt = await store.submit(request_for(graph), CALLER)
        key = ("TemporaryConnection", "test", receipt["name"])
        await settle(controller, key)
        await store.revoke("test", "edge-one", CALLER)
        original = api.request

        async def failing(method, kind, *args, **kwargs):
            if method == "PUT" and kind == "NetworkPolicy":
                raise OSError("policy API unavailable")
            return await original(method, kind, *args, **kwargs)

        api.request = failing
        with pytest.raises(OSError):
            await controller.reconcile(key)
        assert entries(await api.get("Graph", "test", "root")) == {}
        assert api.objects[key]["status"]["phase"] == "Active"
        assert FINALIZER in api.objects[key]["metadata"]["finalizers"]
        api.request = original
        await settle(Controller(api), key)
        assert api.objects[key]["status"]["phase"] == "Revoked"
        monkeypatch.setenv("POLYAD_CONNECTIONS_RETENTION", "0")
        await settle(controller, key)
        assert api.objects[key]["metadata"].get("deletionTimestamp")
        await settle(controller, key)
        assert FINALIZER not in api.objects[key]["metadata"]["finalizers"]

    asyncio.run(run())


def test_receipt_and_target_share_the_same_family_shard():
    """
    Persisting a receipt does not bypass root-family coordination for graph mutations.
    """
    from polyad.operator.coordination.leases import Coordinator

    async def run():
        graph, definitions = graph_fixture()
        api = ConnectionAPI(graph, *definitions)
        receipt = await ConnectionStore(api, ConnectionSettings("test")).submit(request_for(graph), CALLER)
        coordinator = Coordinator(api, "test")
        assert await coordinator.shard_for(("Graph", "test", "root")) == await coordinator.shard_for(
            ("TemporaryConnection", "test", receipt["name"])
        )

    asyncio.run(run())


@pytest.mark.parametrize("domain", ["composition", "connections"])
def test_api_server_serves_real_http_and_closes_workers(monkeypatch, domain):
    """
    Run the bounded Waitress bridge through real sockets and join its transport on shutdown.
    """
    from types import SimpleNamespace

    from polyad.api.server import APIServer
    from tests.test_composition_api import document

    monkeypatch.setenv("POLYAD_API_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("POLYAD_CACHE_URL", "redis://127.0.0.1:6379/0")

    async def run():
        graph, definitions = graph_fixture()
        api = ConnectionAPI(graph, *definitions)
        closed = []
        api.client = SimpleNamespace(close=lambda: closed.append(True))
        server = APIServer(api)
        if domain == "connections":
            server.connections(ConnectionSettings("test"))
        else:
            server.composition("test", "projected")
        server.start(host="127.0.0.1", ports={domain: 0})
        client = Client(f"http://127.0.0.1:{server.server.effective_port}", "projected")
        try:
            if domain == "connections":
                request = request_for(graph)
                receipt = await asyncio.to_thread(client.connect, request)
                observed = await asyncio.to_thread(client.connection, "test", "edge-one")
                assert observed["expiresAt"] == receipt["expiresAt"]
                assert (await asyncio.to_thread(client.disconnect, "test", "edge-one"))["revokeRequested"]
                api.allowed = False
                with pytest.raises(APIError) as error:
                    await asyncio.to_thread(client.connect, request)
                assert error.value.status == 403
                api.authenticated = False
                with pytest.raises(APIError) as error:
                    await asyncio.to_thread(client.connect, request)
                assert error.value.status == 401
            else:
                receipt = await asyncio.to_thread(client.compose, document())
                assert (await asyncio.to_thread(client.composition, receipt["requestId"]))["uid"] == receipt["uid"]
        finally:
            await server.close()
        assert not server.thread.is_alive() and not server.pending and closed == [True]

    asyncio.run(run())
