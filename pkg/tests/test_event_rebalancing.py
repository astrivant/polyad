"""
Verify paced subscription migration, authoritative discovery and replay-safe recovery.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import jsonschema
import pytest

from polyad.api.events.builder import EventAPIBuilder
from polyad.events import rebalance as module
from polyad.events.rebalance import Rebalancer
from polyad_client import Client
from polyad_client.client import APIError
from polyad_client.routing import addresses
from polyad_client.subscriptions import StreamInterrupted
from polyad_schemas.events import event_schema
from polyad_types import CopulseEvent, Event, EventRebalanceSettings
from tests.test_chart import CHART, render


@pytest.fixture
def clock(monkeypatch, tmp_path):
    """
    Isolate termination signals and control the scheduler without real sleeps.
    """
    now = [100.0]
    monkeypatch.setattr(module, "DRAIN_FILE", tmp_path / "draining")
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.random, "uniform", lambda low, high: low)
    return now


def members(count):
    """
    Build unique live Pod identities with routable private addresses.
    """
    return [{"uid": str(index), "address": f"10.1.0.{index + 1}", "port": 8091} for index in range(count)]


def test_membership_roll_batches_coalesces_and_excludes_new_subscribers(clock):
    """
    A new replica triggers paced resets; later changes wait and fresh connections stay put.
    """
    roll = Rebalancer(EventRebalanceSettings(enabled=True, batchPercent=25, intervalSeconds=2, cooldownSeconds=20))
    roll.update(members(2), "")
    old = [roll.register() for _ in range(8)]
    roll.update(members(3), "")
    new = roll.register()
    due = [identity for identity in old if roll.control(identity)]
    assert len(due) == 2
    assert roll.control(new) is None
    for identity in due:
        roll.release(identity)
    roll.update(members(4), "admin-1")
    assert roll.pending == "administrator_requested"
    assert all(roll.control(identity) is None for identity in old if identity not in due)
    clock[0] += 2
    assert len([identity for identity in old if roll.control(identity)]) == 2
    clock[0] += 21
    roll.update(members(4), "admin-1")
    assert not roll.pending and new in roll.schedule


def test_pretermination_drain_overrides_cooldown_and_rejects_new_streams(clock):
    """
    Every existing stream receives a copulse before preStop ends, even during an earlier roll.
    """
    roll = Rebalancer(EventRebalanceSettings(enabled=True, batchPercent=1, intervalSeconds=30, drainSeconds=5))
    roll.update(members(2), "")
    identities = [roll.register() for _ in range(128)]
    roll.update(members(3), "")
    module.DRAIN_FILE.touch()
    with pytest.raises(RuntimeError, match="draining"):
        roll.register()
    roll.control(identities[0])
    assert roll.draining
    assert all(when < 105 for when, _ in roll.schedule.values())
    clock[0] = 105
    for identity in identities:
        payload = roll.control(identity)
        if payload is not None:
            assert payload["reason"] == "replica_draining"
        roll.release(identity)
    assert not roll.streams and not roll.schedule


def test_manual_roll_and_age_rotation_do_not_require_automatic_membership_rolls(clock):
    """
    Administrative and age triggers retain pacing when automatic membership feedback is disabled.
    """
    roll = Rebalancer(EventRebalanceSettings(enabled=True, automatic=False, batchPercent=50, maxConnectionSeconds=60))
    roll.update(members(1), "")
    identities = [roll.register() for _ in range(4)]
    roll.update(members(2), "")
    assert not roll.schedule
    roll.update(members(2), "manual")
    assert len(roll.schedule) == 4
    roll.schedule.clear()
    clock[0] += 61
    assert len([identity for identity in identities if roll.control(identity)]) == 2


def test_overdue_batches_do_not_burst_after_slow_polling(clock):
    """
    Missing several scheduled intervals never releases all ordinary copulses at once.
    """
    roll = Rebalancer(EventRebalanceSettings(enabled=True, batchPercent=25))
    roll.update(members(2), "")
    identities = [roll.register() for _ in range(8)]
    roll.update(members(3), "")
    clock[0] += 10
    assert len([identity for identity in identities if roll.control(identity)]) == 2
    assert all(roll.control(identity) is None for identity in identities)


def test_discovery_expires_and_routes_require_authorization(clock):
    """
    Readers receive only current local event membership and a cursor after stream authorization.
    """
    roll = Rebalancer(EventRebalanceSettings(enabled=True, routing="Direct"))
    roll.update(members(2), "")
    authorize = Mock()
    app = (
        EventAPIBuilder(rebalance=roll, authorize_stream=authorize)
        .with_handlers(lambda cursor: "7-0", Mock())
        .with_bearer_token("reader")
        .build()
    )
    client = app.test_client()
    assert client.get("/v1/events/endpoints").status_code == 401
    response = client.get("/v1/events/endpoints", headers={"Authorization": "Bearer reader"})
    assert response.json["endpoints"] == members(2)
    assert response.json["cursor"] == "7-0"
    authorize.assert_called_once_with(None, None)
    clock[0] += 16
    assert client.get("/v1/events/endpoints", headers={"Authorization": "Bearer reader"}).status_code == 503


@pytest.mark.parametrize("websocket", [False, True])
def test_control_preserves_cursor_contract_and_releases_capacity(clock, websocket):
    """
    A copulse carries no checkpoint and closes either transport with all subscriber slots reclaimed.
    """
    roll = Rebalancer(EventRebalanceSettings(enabled=True))
    roll.update(members(1), "")

    def read(cursor):
        assert cursor == "9-0"
        roll.update(members(2), "")
        return []

    builder = EventAPIBuilder(rebalance=roll, websockets=websocket, max_connections=1).with_handlers(lambda cursor: "9-0", read)
    client = builder.with_bearer_token("reader").build().test_client()
    response = client.get(
        "/v1/events/ws" if websocket else "/v1/events",
        headers={"Authorization": "Bearer reader"},
        environ_overrides={"polyad.websocket": websocket},
    )
    chunks = list(response.response)
    last = chunks[-1].decode()
    document = json.loads(last) if websocket else {"id": "", "event": "copulse", "data": json.loads(last.split("data: ")[1])}
    assert isinstance(Event(**document).typed(), CopulseEvent)
    jsonschema.validate(document, event_schema())
    response.close()
    assert not roll.streams
    with pytest.raises(ValueError):
        Event("9-0", "copulse", document["data"]).typed()


def test_membership_excludes_unready_and_terminating_pods(clock, monkeypatch):
    """
    Discovery is tied to the exact Service selector and never advertises draining Pods.
    """

    async def run():
        service = {"metadata": {}, "spec": {"selector": {"component": "gateway"}, "ports": [{"port": 8091}]}}
        pods = [
            {"metadata": {"uid": "ready"}, "status": {"podIP": "10.1.0.1", "conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"uid": "unready"}, "status": {"podIP": "10.1.0.2"}},
            {
                "metadata": {"uid": "old", "deletionTimestamp": "now"},
                "status": {"podIP": "10.1.0.3", "conditions": [{"type": "Ready", "status": "True"}]},
            },
        ]
        api = SimpleNamespace(get=AsyncMock(return_value=service), request=AsyncMock(return_value={"items": pods}))
        roll = Rebalancer(EventRebalanceSettings(enabled=True, routing="Direct"))
        monkeypatch.setattr(module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await roll.watch(api, "operators", "events")
        assert roll.endpoints()["endpoints"] == [{"uid": "ready", "address": "10.1.0.1", "port": 8091}]
        assert api.request.call_args.kwargs["query"][0] == ("labelSelector", "component=gateway")

    asyncio.run(run())


def test_client_rediscovery_rotates_targets_and_only_checkpoints_success(monkeypatch):
    """
    Clients refresh from their fixed authority and resume after the last completed hook.
    """
    client = Client("https://events.example", "reader")
    subscription = client.subscribe(transport="websocket", rebalance=True)
    subscription._rotation = 0
    monkeypatch.setattr(subscription._stopped, "wait", lambda delay: None)
    monkeypatch.setattr(
        client, "event_endpoints", Mock(return_value={"routing": "Direct", "replicas": 3, "cursor": "5-0", "endpoints": members(3)})
    )
    calls, closed = [], []

    def stream(**kwargs):
        calls.append(kwargs)
        try:
            if len(calls) == 1:
                yield Event("6-0", "graph", {})
                yield Event("", "copulse", {"reason": "membership_changed", "revision": "new", "retryAfterSeconds": 0})
            else:
                yield Event("7-0", "graph", {})
                subscription.stop()
        finally:
            closed.append(True)

    monkeypatch.setattr(client, "events", stream)
    subscription.run()
    assert [call["endpoint"] for call in calls] == [("10.1.0.1", 8091), ("10.1.0.2", 8091)]
    assert [call["last_event_id"] for call in calls] == ["5-0", "6-0"]
    assert subscription.cursor == "7-0" and len(closed) == 2
    assert client.url == "https://events.example"


@pytest.mark.parametrize("failure", ["callback", "expired", "denied"])
def test_managed_subscription_does_not_retry_application_or_permission_failures(monkeypatch, failure):
    """
    Callback failures, replay expiry and denied grants need explicit application recovery.
    """
    client = Client("http://events", "reader")
    subscription = client.subscribe(rebalance=True)
    monkeypatch.setattr(client, "event_endpoints", Mock(return_value={"routing": "Service", "replicas": 1, "cursor": "5-0"}))

    def stream(**kwargs):
        if failure == "denied":
            raise APIError(403, {})
        yield Event("", "reset", {"reason": "expired"}) if failure == "expired" else Event("6-0", "graph", {})

    def callback(event):
        raise OSError("application failure is not a transport retry")

    subscription.on(lambda event: True, callback)
    monkeypatch.setattr(client, "events", stream)
    with pytest.raises({"callback": OSError, "expired": StreamInterrupted, "denied": APIError}[failure]):
        subscription.run()
    assert subscription.cursor == "5-0"


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "::1", "https://evil.example", "10.0.0.1/path"])
def test_discovery_rejects_redirect_urls_and_special_destinations(address):
    """
    A directory cannot turn a connection control into arbitrary URL or metadata-service access.
    """
    with pytest.raises(ValueError):
        addresses({"routing": "Direct", "endpoints": [{"address": address, "port": 8091}]})


def test_chart_wires_copulses_istio_and_native_sidecar_drain():
    """
    The overlay enables one event runtime, safe termination and mesh routing for new connections.
    """
    objects = render(values_files=(CHART / "values-event-rebalancing.reference.yaml",))
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    pod = deployment["spec"]["template"]
    operator = pod["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in operator["env"]}
    assert EventRebalanceSettings(**json.loads(env["POLYAD_EVENTS_REBALANCE"])).enabled
    assert env["POLYAD_EVENTS_SERVICE"] == "test-polyad-events"
    assert operator["lifecycle"]["preStop"]["exec"]["command"] == ["python", "-m", "polyad.events.rebalance"]
    assert json.loads(pod["metadata"]["annotations"]["proxy.istio.io/config"])["terminationDrainDuration"] == "20s"
    rule = next(obj for obj in objects if obj["kind"] == "DestinationRule")
    assert rule["spec"]["trafficPolicy"]["loadBalancer"] == {"simple": "LEAST_REQUEST", "warmup": {"duration": "30s"}}


@pytest.mark.parametrize("transport", ["sse", "websocket"])
def test_direct_socket_preserves_authority_and_receives_real_stream(monkeypatch, transport):
    """
    Both transports dial the supplied IP without resolving or replacing the configured authority.
    """
    from flask import request

    from polyad.api.http.server import APIServer
    from tests.test_temporary_connections import ConnectionAPI

    monkeypatch.setenv("POLYAD_API_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("POLYAD_SERVICE_ACCESS", '{"discovery":"Cluster"}')

    async def run():
        api = ConnectionAPI()
        api.client = SimpleNamespace(close=Mock())

        async def read(cursor):
            await asyncio.sleep(0.02)
            return [("2-0", '{"uid":"graph","type":"topology"}')] if cursor == "0-0" else []

        runtime = APIServer(api)
        runtime.events(SimpleNamespace(cursor=AsyncMock(return_value="0-0"), read=read), "test", "reader", websockets=True)
        hosts = []

        @runtime.app.before_request
        def record_authority():
            hosts.append(request.headers["Host"])

        runtime.start(host="127.0.0.1", ports={"events": 0})
        port = runtime.server.effective_listen[0][1]

        def exercise():
            client = Client("http://events.invalid:8091", "reader", timeout=2)
            stream = client.events(transport=transport, endpoint=("127.0.0.1", port))
            try:
                assert next(stream).id == "2-0"
            finally:
                stream.close()

        try:
            await asyncio.to_thread(exercise)
            assert hosts == ["events.invalid:8091"]
        finally:
            await runtime.close()

    asyncio.run(run())
