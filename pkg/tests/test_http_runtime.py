"""
Verify one HTTP runtime preserves endpoint isolation and streaming capacity.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from polyad.api.application import create_application
from polyad.api.builder import APIBuilder
from polyad.api.connections.store import ConnectionSettings
from polyad.api.server import APIServer
from polyad.events.builder import EventAPIBuilder
from polyad.metrics.builder import MetricsAPIBuilder
from polyad.metrics.store import MetricsStore
from tests.test_temporary_connections import ConnectionAPI


def test_shared_application_preserves_port_and_credential_isolation():
    """
    Host headers cannot route a metrics peer into composition or event operations.
    """
    app = create_application()
    APIBuilder().with_handlers(Mock(), Mock()).with_bearer_token("writer").build(app)
    EventAPIBuilder().with_handlers(Mock(), Mock()).with_bearer_token("reader").build(app)
    MetricsAPIBuilder(store=MetricsStore(), token="scraper").build(app)
    ports = {"8090": "composition", "8091": "events", "8092": "metrics"}
    app.extensions["polyad.ports"] = ports
    paths = {"composition": "/v1/compositions", "events": "/v1/events", "metrics": "/metrics"}
    tokens = {"composition": "writer", "events": "reader", "metrics": "scraper"}
    client = app.test_client()
    for port, domain in ports.items():
        environment = {"SERVER_PORT": port}
        own = {"Authorization": f"Bearer {tokens[domain]}", "Host": "spoofed:8090", "X-Forwarded-Port": "8090"}
        schema = client.get("/openapi.json", headers=own, environ_overrides=environment)
        assert schema.status_code == 200
        assert paths[domain] in schema.json["paths"]
        for other, token in tokens.items():
            if other == domain:
                continue
            response = client.get("/openapi.json", headers={"Authorization": f"Bearer {token}"}, environ_overrides=environment)
            assert response.status_code == 401
            response = client.get(paths[other], headers=own, environ_overrides=environment)
            assert response.status_code == 404
    assert client.get("/openapi.json", environ_overrides={"SERVER_PORT": "9000"}).status_code == 404
    malformed = client.get("/v1//metrics", environ_overrides={"SERVER_PORT": "8092"})
    assert malformed.status_code == 404
    assert "Location" not in malformed.headers
    with pytest.raises(ValueError, match="already registered"):
        MetricsAPIBuilder(store=MetricsStore()).build(app)


@pytest.mark.parametrize("demo", [False, True])
@pytest.mark.parametrize("websockets", [False, True])
def test_one_server_keeps_metrics_available_with_full_event_streams(monkeypatch, demo, websockets):
    """
    One dispatcher serves every API and reserves workers even when demo event slots are full.
    """
    from polyad.api import server as module

    monkeypatch.setenv("POLYAD_API_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("POLYAD_AUTH_MODE", "Disabled" if demo else "Required")
    monkeypatch.setenv("POLYAD_SERVICE_ACCESS", '{"discovery":"Cluster"}')
    monkeypatch.setenv("POLYAD_CACHE_URL", "redis://127.0.0.1:6379/0")
    application_factory = Mock(wraps=module.create_application)
    server_factory = Mock(wraps=module.create_server)
    monkeypatch.setattr(module, "create_application", application_factory)
    monkeypatch.setattr(module, "create_server", server_factory)

    async def run():
        api = ConnectionAPI()
        api.client = SimpleNamespace(close=Mock())

        async def read(cursor):
            await asyncio.sleep(0.05)
            return []

        store = SimpleNamespace(cursor=AsyncMock(return_value="0-0"), read=read)
        runtime = APIServer(api)
        runtime.composition("test", "writer")
        runtime.connections(ConnectionSettings("test"))
        runtime.events(store, "test", "reader", connections=1, websockets=websockets)
        runtime.metrics(MetricsStore(), "scraper")
        assert application_factory.call_count == 1
        assert not server_factory.called
        with ExitStack() as stack:
            sockets = [stack.enter_context(socket.socket()) for _ in runtime.ports]
            for sock in sockets:
                sock.bind(("127.0.0.1", 0))
            ports = {name: sock.getsockname()[1] for name, sock in zip(runtime.ports, sockets, strict=True)}
        runtime.start(host="127.0.0.1", ports=ports)
        with pytest.raises(RuntimeError, match="only be started once"):
            runtime.start(host="127.0.0.1", ports=ports)
        assert server_factory.call_count == (0 if websockets else 1)
        if not websockets:
            assert server_factory.call_args.kwargs["threads"] == 9
        else:
            assert runtime.server.workers == 9

        def fetch(domain, path="/openapi.json", method="GET"):
            token = {"composition": "writer", "connections": "projected", "events": "reader", "metrics": "scraper"}[domain]
            return urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{ports[domain]}{path}",
                    method=method,
                    headers={} if demo else {"Authorization": f"Bearer {token}"},
                ),
                timeout=5,
            )

        response = None
        try:
            for _ in range(100):
                try:
                    with await asyncio.to_thread(fetch, "metrics"):
                        break
                except urllib.error.URLError:
                    await asyncio.sleep(0.02)
            response = await asyncio.to_thread(fetch, "events", "/v1/events")
            assert await asyncio.to_thread(response.readline) == b"retry: 3000\n"
            with pytest.raises(urllib.error.HTTPError) as error:
                await asyncio.to_thread(fetch, "events", "/v1/events")
            assert error.value.code == 503
            for domain in ports:
                with await asyncio.to_thread(fetch, domain) as result:
                    schema = json.loads(await asyncio.to_thread(result.read))
                    assert schema["openapi"].startswith("3.")
                with await asyncio.to_thread(fetch, domain, method="HEAD") as result:
                    assert result.status == 200
                    assert await asyncio.to_thread(result.read) == b""
        finally:
            if response:
                response.close()
            await runtime.close()
            await runtime.close()
        assert not runtime.thread.is_alive()
        assert not runtime.pending
        api.client.close.assert_called_once()

    asyncio.run(run())
