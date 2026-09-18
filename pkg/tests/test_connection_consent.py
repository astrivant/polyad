"""
Verify service consent, identity fences, pending expiry and negotiation cooldowns.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from attrs import evolve
from kubernetes.client.exceptions import ApiException

from polyad.api.connections.app import build_app
from polyad.api.connections.consent import CONSENTS, confirmed, endpoint
from polyad.api.connections.store import ConnectionSettings, ConnectionStore
from polyad.api.events.builder import EventAPIBuilder
from polyad.api.http.errors import Conflict, Forbidden
from polyad.events.store import EventStore
from polyad.events.visibility import public_observation
from polyad.graph.temporary import entries
from polyad.operator.coordination.pulses import PulseDeferred, PulsePolicy
from polyad.operator.coordination.shared_queue import SharedQueue
from polyad.operator.reconciliation.controller import Controller
from polyad_sdk import APIError, Client
from polyad_types import ConnectionResponse
from polyad_types.resources import GROUP
from tests.test_authentication import access, header, registry
from tests.test_client import Adapter
from tests.test_operator import resource
from tests.test_temporary_connections import CALLER, ConnectionAPI, graph_fixture, participant, request_for, settle


@pytest.mark.parametrize("kind", ["Graph", "PolyGraph", "ReplicaGroup"])
def test_requester_consents_once_and_counterpart_must_approve(monkeypatch, kind):
    """
    Grant no connection until the other endpoint consents with its own permission.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        graph, definitions = graph_fixture(kind)
        api = ConnectionAPI(graph, *definitions)
        request = request_for(graph)
        source, target = (participant(api, graph, node) for node in (request.source, request.target))
        store = ConnectionStore(api, ConnectionSettings("test"))
        receipt = await store.submit(request, source)
        assert receipt["consent"] == {request.source: "Approve"}
        key = ("TemporaryConnection", "test", receipt["name"])
        controller = Controller(api)
        await settle(controller, key)
        assert api.objects[key]["status"]["awaitingApproval"] == [request.target]
        assert not entries(api.objects[kind, "test", "root"])
        response = ConnectionResponse(receipt["uid"], "Approve")
        approved = await store.respond("test", receipt["name"], response, target)
        assert approved["expiresAt"] == receipt["expiresAt"]
        await settle(controller, key)
        assert api.objects[key]["status"]["phase"] == "Active"
        assert len(entries(api.objects[kind, "test", "root"])) == 1
        assert {r["resourceAttributes"]["verb"] for r in api.reviews if r["user"] == source.username} == {"connect"}
        assert {r["resourceAttributes"]["verb"] for r in api.reviews if r["user"] == target.username} == {"approve"}
        # An explicit refusal revokes even an already active grant; replaying the
        # original proposal cannot silently restore implicit consent.
        await store.respond("test", receipt["name"], ConnectionResponse(receipt["uid"], "Reject"), source)
        replay = await store.submit(request, source)
        assert replay["consent"][request.source] == "Reject"
        await settle(controller, key)
        assert api.objects[key]["status"]["phase"] == "Rejected"
        assert not entries(api.objects[kind, "test", "root"])

    asyncio.run(run())


def test_connection_events_publish_public_receipts_and_match_exact_graph_grants(tmp_path):
    """
    Deliver proposals to the owning graph's subscribers without leaking consent identities.
    """

    async def publish():
        graph, definitions = graph_fixture()
        api = ConnectionAPI(graph, *definitions)
        caller = participant(api, graph, "a")
        store = ConnectionStore(api, ConnectionSettings("test"))
        receipt = await store.submit(request_for(graph), caller)
        event_store = EventStore("redis://localhost", "test", visible=lambda obj: public_observation(api, obj))
        cache = AsyncMock()
        event_store.cache.client = cache
        try:
            await event_store.publish(api.objects["TemporaryConnection", "test", receipt["name"]])
            payload = json.loads(cache.eval.await_args.args[-2])
            assert payload["type"] == "connection"
            assert payload["connection"]["consent"] == {"a": "Approve"}
            assert payload["graph"] == {"kind": "Graph", "namespace": "test", "name": "root", "uid": graph["metadata"]["uid"]}
            assert "system:serviceaccount" not in json.dumps(payload)
            return payload
        finally:
            await event_store.close()

    payload = asyncio.run(publish())
    keys = registry(tmp_path)
    path = tmp_path / "config.json"
    configuration = json.loads(path.read_text())
    configuration["services"][0]["graphs"] = [{"name": "root", "namespace": "test", "kind": "Graph", "descendants": False}]
    path.write_text(json.dumps(configuration))
    stopping = Event()

    def read(cursor):
        stopping.set()
        unrelated = copy.deepcopy(payload)
        unrelated["graph"]["name"] = "unrelated"
        return [("1-0", json.dumps(payload)), ("2-0", json.dumps(unrelated))]

    app = EventAPIBuilder(access=access(keys), stopping=stopping).with_handlers(lambda cursor: "0-0", read).build()
    response = app.test_client().get("/v1/events", headers=header())
    assert "event: connection" in response.text
    assert "id: 1-0" in response.text and "id: 2-0" not in response.text
    response.close()


def test_reconciliation_pulses_are_shared_and_keep_cleanup_outside_the_budget(monkeypatch):
    """
    HA replicas share each resource lane; other clusters and connection cleanup remain independent.
    """
    monkeypatch.setenv("POLYAD_RECONCILIATION_COOLDOWN_SECONDS", "10")
    monkeypatch.setenv("POLYAD_RECONCILIATION_BURST", "2")

    async def run():
        queues = [SharedQueue("redis://localhost", "test", replica) for replica in ("a", "b")]
        try:
            for queue in queues:
                queue.client = AsyncMock()
                queue.client.eval.return_value = 0
                await queue.pulse(("Graph", "test", "pipeline"), cluster="west")
            assert queues[0].client.eval.await_args == queues[1].client.eval.await_args
            first = queues[0].client.eval.await_args
            await queues[0].pulse(("Graph", "test", "pipeline"), cluster="east")
            assert first != queues[0].client.eval.await_args
            queues[0].client.eval.side_effect = RuntimeError("cache unavailable")
            await queues[0].pulse(("TemporaryConnection", "test", "receipt"), cluster="west")
            with pytest.raises(RuntimeError, match="cache unavailable"):
                await queues[0].pulse(("Graph", "test", "pipeline"), cluster="west")
        finally:
            for queue in queues:
                await queue.close()

    asyncio.run(run())


def test_remote_cooldown_retains_delivery_before_reconciliation(monkeypatch):
    """
    A deferred pulse cannot acknowledge desired work or take a graph-family duty.
    """
    from polyad.operator.clusters.root import ClusterWorker
    from polyad.operator.lifecycle import health

    monkeypatch.setenv("POLYAD_RECONCILIATION_COOLDOWN_SECONDS", "10")
    monkeypatch.setattr(health.lifecycle, "draining", Event())
    monkeypatch.setattr(health.lifecycle, "replacement", Event())

    async def run():
        worker = ClusterWorker.__new__(ClusterWorker)
        worker.cluster = "west"
        worker.root = SimpleNamespace(coordinator=SimpleNamespace(guard=AsyncMock(), owned={0}, duty=AsyncMock()))
        worker.shared = SimpleNamespace(
            take=AsyncMock(return_value=("1-0", ("Graph", "test", "pipeline"))),
            pulse=AsyncMock(side_effect=PulseDeferred(5)),
            acknowledge=AsyncMock(),
        )
        worker.shard_for = AsyncMock(return_value=0)
        worker.controller = SimpleNamespace(reconcile=AsyncMock())
        await worker.consume()
        worker.shared.pulse.assert_awaited_once_with(("Graph", "test", "pipeline"), cluster="west")
        worker.shared.acknowledge.assert_not_awaited()
        worker.controller.reconcile.assert_not_awaited()
        worker.root.coordinator.duty.assert_not_called()

    asyncio.run(run())


def test_third_party_request_requires_both_endpoints_and_expires_without_them(monkeypatch):
    """
    Missing approval permission or a silent service cannot become implicit admission.
    """
    monkeypatch.setenv("POLYAD_CONNECTIONS_ENABLED", "true")

    async def run():
        graph, definitions = graph_fixture()
        api = ConnectionAPI(graph, *definitions)
        store = ConnectionStore(api, ConnectionSettings("test"))
        receipt = await store.submit(request_for(graph), CALLER)
        assert receipt["consent"] == {}
        target = participant(api, graph, "b")
        api.allowed = False
        with pytest.raises(Forbidden, match="approve"):
            await store.respond("test", receipt["name"], ConnectionResponse(receipt["uid"], "Approve"), target)
        api.allowed = True
        key = ("TemporaryConnection", "test", receipt["name"])
        controller = Controller(api)
        await settle(controller, key)
        assert api.objects[key]["status"]["awaitingApproval"] == ["a", "b"]
        api.objects[key]["metadata"]["creationTimestamp"] = (datetime.now(UTC) - timedelta(seconds=301)).isoformat()
        await settle(controller, key)
        assert api.objects[key]["status"]["phase"] == "Expired"
        assert not entries(api.objects["Graph", "test", "root"])
        with pytest.raises(Conflict, match="expired"):
            await store.respond("test", receipt["name"], ConnectionResponse(receipt["uid"], "Approve"), target)

    asyncio.run(run())


@pytest.mark.parametrize("controller_kind", ["Deployment", "StatefulSet", "DaemonSet", "Job"])
def test_consent_follows_live_controller_chain_and_nested_boundary(controller_kind):
    """
    Use the enclosing node identity and fence every native and nested owner by UID.
    """

    async def run():
        graph, definitions = graph_fixture("PolyGraph")
        api = ConnectionAPI(graph, *definitions)
        store = ConnectionStore(api, ConnectionSettings("test"))
        receipt = await store.submit(request_for(graph), CALLER)
        stored = api.objects["TemporaryConnection", "test", receipt["name"]]
        caller = participant(api, graph, "a")
        pod = api.objects["Pod", "test", caller.extra["authentication.kubernetes.io/pod-name"][0]]
        nested = resource("Graph", "nested", {})
        native = resource(controller_kind, "native", {})
        native["apiVersion"] = "batch/v1" if controller_kind == "Job" else "apps/v1"
        chain = [graph, nested, native]
        if controller_kind == "Deployment":
            replica_set = resource("ReplicaSet", "replica-set", {})
            replica_set["apiVersion"] = "apps/v1"
            chain.append(replica_set)
        chain.append(pod)
        for parent, child in zip(chain, chain[1:], strict=False):
            child["metadata"]["ownerReferences"] = [
                {
                    "apiVersion": parent["apiVersion"],
                    "kind": parent["kind"],
                    "name": parent["metadata"]["name"],
                    "uid": parent["metadata"]["uid"],
                    "controller": True,
                }
            ]
            child["metadata"]["labels"] = {f"{GROUP}/node": "a" if child is nested else "inner"}
            api.objects[child["kind"], "test", child["metadata"]["name"]] = child
        assert await endpoint(api, caller, stored) == "a"
        native["metadata"]["uid"] = "replacement"
        with pytest.raises(Forbidden):
            await endpoint(api, caller, stored)

    asyncio.run(run())


@pytest.mark.parametrize("changed", ["Pod", "ServiceAccount", "Graph", "permission", "token"])
def test_approval_is_revalidated_against_live_identity(changed):
    """
    A stale Pod, service account, graph or permission cannot activate a pending receipt.
    """

    async def run():
        graph, definitions = graph_fixture()
        api = ConnectionAPI(graph, *definitions)
        store = ConnectionStore(api, ConnectionSettings("test"))
        caller = participant(api, graph, "a")
        receipt = await store.submit(request_for(graph), caller)
        stored = api.objects["TemporaryConnection", "test", receipt["name"]]
        assert await confirmed(api, stored, store.settings) == {"a"}
        if changed == "permission":
            api.allowed = False
        elif changed == "token":
            # A shared SA without the original Pod binding is not a participant.
            with pytest.raises(Forbidden, match="Pod-bound"):
                await endpoint(api, evolve(caller, extra={}), stored)
            return
        else:
            name = "root" if changed == "Graph" else caller.extra["authentication.kubernetes.io/pod-name"][0]
            api.objects[changed, "test", name]["metadata"]["uid"] = "replaced"
        assert await confirmed(api, stored, store.settings) == set()

    asyncio.run(run())


def test_concurrent_response_conflict_preserves_other_consent_and_charges_once(monkeypatch):
    """
    Merge endpoint decisions on conflict while keeping unchanged retries outside the pulse budget.
    """

    async def run():
        graph, definitions = graph_fixture()
        api = ConnectionAPI(graph, *definitions)
        store = ConnectionStore(api, ConnectionSettings("test"))
        receipt = await store.submit(request_for(graph), CALLER)
        source, target = (participant(api, graph, node) for node in ("a", "b"))
        key = ("TemporaryConnection", "test", receipt["name"])
        response = ConnectionResponse(receipt["uid"], "Approve")
        # Keep one stale copy, then accept the other endpoint's response.
        stale = copy.deepcopy(api.objects[key])
        await store.respond("test", receipt["name"], response, source)
        pulse = AsyncMock()
        monkeypatch.setattr(store, "pulse", pulse)
        await store.record(stale, target, "b", "Approve", verb="approve")
        assert ConnectionStore.receipt(api.objects[key])["consent"] == {"a": "Approve", "b": "Approve"}
        pulse.assert_awaited_once()
        await store.respond("test", receipt["name"], response, target)
        pulse.assert_awaited_once()
        with pytest.raises(Conflict, match="UID"):
            await store.respond("test", receipt["name"], ConnectionResponse("stale", "Approve"), target)
        await store.respond("test", receipt["name"], ConnectionResponse(receipt["uid"], "Reject"), target)
        pulse.assert_awaited_once()
        # Public receipts never expose the identities retained for revalidation.
        public = json.dumps(ConnectionStore.receipt(api.objects[key]))
        assert CONSENTS not in public and "system:serviceaccount" not in public

    asyncio.run(run())


def test_connection_client_response_and_retry_after():
    """
    Route typed service decisions through the existing API and expose pulse throttling as HTTP 429.
    """
    received = []

    def respond(namespace, name, response, caller):
        received.append((namespace, name, response, caller))
        if response.decision == "Approve":
            raise PulseDeferred(1.2)
        return {"consent": {"b": "Reject"}}

    app = build_app(lambda token: CALLER, lambda *args: {}, lambda *args: {}, lambda *args: {}, respond=respond)
    client = Client("http://polyad:8093", "token")
    client._opener = Adapter(app)
    assert client.respond_connection("test", "connection-id", ConnectionResponse("uid", "Reject"))["consent"] == {"b": "Reject"}
    assert received[0] == ("test", "connection-id", ConnectionResponse("uid", "Reject"), CALLER)
    with pytest.raises(APIError) as error:
        client.respond_connection("test", "connection-id", ConnectionResponse("uid", "Approve"))
    assert error.value.status == 429
    reply = app.test_client().post(
        "/v1/connections/test/connection-id/response", json={"uid": "uid", "decision": "Approve"}, headers={"Authorization": "Bearer token"}
    )
    assert reply.headers["Retry-After"] == "2"


@pytest.mark.parametrize(
    "kwargs", [{"cooldown": True}, {"cooldown": float("nan")}, {"cooldown": 301}, {"burst": 1.5}, {"burst": True}, {"burst": 0}]
)
def test_pulse_settings_reject_ambiguous_types(kwargs):
    """
    Validate direct Python configuration as strictly as administrator values.
    """
    with pytest.raises(ValueError):
        PulsePolicy(**kwargs)


def test_pulse_policy_uses_shared_atomic_budget_and_disables_without_cache_access():
    """
    Replica processes use identical scoped keys, server TTLs and bounded retry delays.
    """

    async def run():
        client = AsyncMock()
        await PulsePolicy().admit(client, "graph")
        client.eval.assert_not_called()
        client.eval.side_effect = [0, 1200, 0]
        await PulsePolicy(2, 1).admit(client, "graph-a")
        with pytest.raises(PulseDeferred) as error:
            await PulsePolicy(2, 1).admit(client, "graph-a")
        assert error.value.retry_after == 2
        await PulsePolicy(2, 1).admit(client, "graph-b")
        first, second, third = [call.args for call in client.eval.await_args_list]
        assert first == second and first[2] != third[2]
        assert first[-2:] == ("2000", "1")
        client.eval.side_effect = ApiException(status=503)
        with pytest.raises(ApiException):
            await PulsePolicy(2, 1).admit(client, "graph-a")

    asyncio.run(run())
