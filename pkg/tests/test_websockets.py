"""
Verify optional WebSocket subscriptions share HTTP authorization, replay and bounded capacity.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from openapi_spec_validator import validate
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.sync.client import connect

from polyad.api.events.builder import EventAPIBuilder
from polyad.api.http.server import APIServer
from polyad.exceptions.events import CursorExpired
from polyad_sdk import Client
from polyad_sdk.exceptions.api import APIError
from tests.test_temporary_connections import ConnectionAPI


def test_websocket_runtime_preserves_auth_replay_capacity_and_cleanup(monkeypatch):
    """
    Real upgraded sockets and SSE compete for one slot and release it on disconnect.
    """
    monkeypatch.setenv("POLYAD_API_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("POLYAD_SERVICE_ACCESS", '{"discovery":"Cluster"}')

    async def run():
        api = ConnectionAPI()
        api.client = SimpleNamespace(close=Mock())

        def cursor(value):
            if value == "1-0":
                raise CursorExpired("expired")
            return value or "0-0"

        async def read(value):
            await asyncio.sleep(0.02)
            if value == "0-0":
                return [("2-0", '{"uid":"visible","type":"topology"}')]
            return []

        store = SimpleNamespace(cursor=AsyncMock(side_effect=cursor), read=read)
        runtime = APIServer(api)
        runtime.events(store, "test", "reader", connections=1, websockets=True)
        runtime.start(host="127.0.0.1", ports={"events": 0})
        port = runtime.server.effective_listen[0][1]
        base = f"http://127.0.0.1:{port}"
        uri = f"ws://127.0.0.1:{port}/v1/events/ws"
        headers = {"Authorization": "Bearer reader"}

        def exercise():
            for _ in range(100):
                try:
                    with connect(uri, additional_headers=headers, open_timeout=1) as stream:
                        assert json.loads(stream.recv(timeout=2))["event"] == "heartbeat"
                    break
                except OSError:
                    time.sleep(0.02)
            else:
                pytest.fail("shared server did not start")

            def available():
                for _ in range(100):
                    try:
                        return connect(uri, additional_headers=headers, open_timeout=2)
                    except InvalidStatus as error:
                        assert error.response.status_code == 503
                        time.sleep(0.02)
                pytest.fail("subscription did not release its slot")

            with available() as stream:
                assert json.loads(stream.recv(timeout=2))["event"] == "heartbeat"
                event = json.loads(stream.recv(timeout=2))
                assert event == {"id": "2-0", "event": "topology", "data": {"uid": "visible", "type": "topology"}}
                with pytest.raises(InvalidStatus) as full:
                    connect(uri, additional_headers=headers)
                assert full.value.response.status_code == 503
                with pytest.raises(HTTPError) as full_sse:
                    urlopen(Request(base + "/v1/events", headers=headers), timeout=2)
                assert full_sse.value.code == 503
                with urlopen(Request(base + "/openapi.json", headers=headers), timeout=2) as response:
                    assert "/v1/events/ws" in json.load(response)["paths"]

            # Authorization and route checks precede stream capacity admission.
            for path, extra, status in (
                ("/v1/events/ws", {"Authorization": "Bearer wrong"}, 401),
                ("/v1/events/ws", {**headers, "Origin": "https://browser.example"}, 403),
                ("/v1/events", headers, 404),
                ("/openapi.json", headers, 404),
            ):
                with pytest.raises(InvalidStatus) as error:
                    connect(f"ws://127.0.0.1:{port}" + path, additional_headers=extra)
                assert error.value.response.status_code == status
            with pytest.raises(HTTPError) as upgrade:
                urlopen(Request(base + "/v1/events/ws", headers=headers), timeout=2)
            assert upgrade.value.code == 426

            with available() as stream:
                stream.send("mutate")
                with pytest.raises(ConnectionClosedError) as closed:
                    while True:
                        stream.recv(timeout=2)
                assert closed.value.rcvd.code == 1008

            # A close must also free the slot for the existing SSE transport.
            with available():
                pass
            for _ in range(100):
                try:
                    iterator = Client(base, "reader").events(last_event_id="0-0", transport="websocket")
                    assert next(iterator).id == "2-0"
                    iterator.close()
                    break
                except APIError as error:
                    assert error.status == 503
                    time.sleep(0.02)
            else:
                pytest.fail("client could not resume after disconnect")

            with available():
                pass
            for _ in range(100):
                try:
                    with urlopen(Request(base + "/v1/events", headers=headers), timeout=2) as response:
                        assert response.readline() == b"retry: 3000\n"
                    break
                except HTTPError as error:
                    assert error.code == 503
                    time.sleep(0.02)
            else:
                pytest.fail("SSE could not reclaim the shared slot")

        try:
            await asyncio.to_thread(exercise)
        finally:
            await runtime.close()
        assert not runtime.thread.is_alive()
        assert not runtime.pending
        assert runtime.server.active == 0

    asyncio.run(run())


def test_websocket_builder_is_opt_in_and_shares_cursor_errors():
    """
    Disabled routes stay absent from the schema, and invalid replay cannot consume a slot.
    """

    def resolve(cursor):
        if cursor == "1-0":
            raise CursorExpired("expired")
        return "2-0"

    headers = {"Authorization": "Bearer token"}
    for enabled in (False, True):
        app = EventAPIBuilder(websockets=enabled).with_handlers(resolve, lambda cursor: []).with_bearer_token("token").build()
        client = app.test_client()
        schema = client.get("/openapi.json", headers=headers).json
        validate(schema)
        assert ("/v1/events/ws" in schema["paths"]) == enabled
        result = client.get("/v1/events/ws", headers=headers)
        assert result.status_code == (426 if enabled else 404)
        if enabled:
            result = client.get("/v1/events/ws", headers={**headers, "Last-Event-ID": "1-0"}, environ_overrides={"polyad.websocket": True})
            assert result.status_code == 410


@pytest.mark.parametrize("transport", ["sse", "websocket"])
def test_stream_scopes_and_key_rotation_apply_to_both_transports(tmp_path, transport):
    """
    Stream grants hide unrelated graphs and key replacement stops an active subscription.
    """
    from tests.test_authentication import access, header, registry

    keys = registry(tmp_path)
    path = tmp_path / "config.json"
    config = json.loads(path.read_text())
    config["services"][0]["graphs"] = [{"name": "allowed", "kind": "Graph", "namespace": "test"}]
    path.write_text(json.dumps(config))
    auth = access(keys)
    observations = [
        (f"{index}-0", json.dumps({"kind": "Graph", "name": name, "namespace": "test", "uid": name}))
        for index, name in enumerate(("unrelated", "allowed"), 1)
    ]
    app = (
        EventAPIBuilder(access=auth, websockets=True)
        .with_handlers(lambda cursor: "0-0", lambda cursor: observations if cursor == "0-0" else [])
        .build()
    )
    response = app.test_client().get(
        "/v1/events/ws" if transport == "websocket" else "/v1/events",
        headers=header(),
        environ_overrides={"polyad.websocket": True},
        buffered=False,
    )
    stream = response.response
    next(stream)  # Transport preamble.
    data = next(stream)
    assert b'"allowed"' in data and b'"unrelated"' not in data
    auth.lanes.release.assert_not_called()
    (tmp_path / "services" / "inbound").write_text("rotated")
    with pytest.raises(RuntimeError, match="revoked or changed"):
        next(stream)
    response.close()
    assert auth.lanes.release.called
