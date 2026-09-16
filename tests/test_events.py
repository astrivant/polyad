"""
Verify bounded event replay, authentication and replica replacement health.
"""

from __future__ import annotations

import asyncio
import json
import os
from threading import Event
from uuid import uuid4

import pytest
from openapi_spec_validator import validate

from polyad.events.builder import EventAPIBuilder
from polyad.events.store import CursorExpired, EventStore
from polyad.events.topology import topology_snapshot
from polyad.operator import health as health_state
from tests.test_operator import FakeAPI, resource


def test_events_are_authenticated_and_have_a_separate_openapi_schema():
    """
    Expose a read-only stream with replay headers and no composition write endpoint.
    """
    stopping = Event()

    def read(cursor):
        stopping.set()
        assert cursor == "12-0"
        return [("13-0", '{"uid":"graph-one"}')]

    app = EventAPIBuilder(stopping=stopping).with_handlers(lambda cursor: cursor or "12-0", read).with_bearer_token("subscriber").build()
    client = app.test_client()
    assert client.get("/v1/events").status_code == 401
    headers = {"Authorization": "Bearer subscriber", "Last-Event-ID": "12-0"}
    schema = client.get("/openapi.json", headers=headers).json
    validate(schema)
    response = client.get("/v1/events", headers=headers)
    assert response.mimetype == "text/event-stream"
    assert 'id: 13-0\nevent: graph\ndata: {"uid":"graph-one"}' in response.text
    response.close()
    assert client.post("/v1/compositions", headers=headers).status_code == 404


def test_event_capacity_and_expired_cursors_release_slots():
    """
    Bound simultaneous subscribers and return capacity after disconnect or invalid replay.
    """

    def resolve(cursor):
        if cursor == "1-0":
            raise CursorExpired("expired")
        return "2-0"

    app = EventAPIBuilder(max_connections=1).with_handlers(resolve, lambda cursor: []).with_bearer_token("token").build()
    client = app.test_client()
    headers = {"Authorization": "Bearer token"}
    assert client.get("/v1/events", headers={**headers, "Last-Event-ID": "1-0"}).status_code == 410
    first = client.get("/v1/events", headers=headers, buffered=False)
    assert first.status_code == 200
    assert client.get("/v1/events", headers=headers).status_code == 503
    first.close()
    reopened = client.get("/v1/events", headers=headers, buffered=False)
    assert reopened.status_code == 200
    reopened.close()


def test_projected_credentials_request_replacement_without_api_writes(tmp_path, monkeypatch):
    """
    Secret replacement or removal invalidates the loaded credential fingerprint.
    """
    state = health_state.Lifecycle()
    monkeypatch.setattr(health_state, "lifecycle", state)
    token = tmp_path / "token"
    token.write_text("old-secret")
    monkeypatch.setenv("POLYAD_API_TOKEN_FILE", str(token))
    assert health_state.credential_token("API") == "old-secret"
    assert not health_state.credentials_changed()
    token.write_text("rotated-secret")
    assert health_state.credentials_changed()
    token.unlink()
    assert health_state.credentials_changed()
    assert b"old-secret" not in state.credentials.values()


@pytest.mark.skipif(not os.environ.get("POLYAD_TEST_DRAGONFLY_URL"), reason="requires an isolated Dragonfly cache")
def test_replicas_share_replay_deduplication_and_retention():
    """
    Publish and consume across separate pools while bounding replay and duplicate state.
    """

    async def run():
        namespace = f"events-{uuid4()}"
        first = EventStore(os.environ["POLYAD_TEST_DRAGONFLY_URL"], namespace, retention=100)
        second = EventStore(os.environ["POLYAD_TEST_DRAGONFLY_URL"], namespace, retention=100)
        try:
            obj = resource("Graph", "graph", {"token": "never-publish-me"})
            obj["metadata"]["namespace"] = namespace
            await first.publish(obj)
            await second.publish(obj)
            events = await second.read("0-0")
            assert len(events) == 1 and "never-publish-me" not in events[0][1]
            cursor = events[0][0]
            for revision in range(2, 110):
                obj["metadata"]["resourceVersion"] = str(revision)
                await first.publish(obj)
            assert await first.cache.client.xlen(first.key) == 100
            with pytest.raises(CursorExpired):
                await second.cursor(cursor)
            with pytest.raises(ValueError):
                await second.cursor("9999999999999999999-0")
            with pytest.raises(ValueError):
                await second.cursor("not-an-id")
            assert len(await second.read("0-0")) == 64
            obj["spec"] = {"mode": "persistent", "nodes": [{"name": "one", "kind": "Daemon", "ref": "worker"}]}
            snapshot = await topology_snapshot(FakeAPI(obj), obj)
            assert snapshot["valid"]
            await first.publish(obj, topology=snapshot)
            initial = await second.topology("Graph", "graph", obj["metadata"]["uid"])
            before = await second.cache.client.xlen(second.key)
            await second.publish(obj, topology=snapshot)
            assert await second.cache.client.xlen(second.key) == before
            assert (await second.topology("Graph", "graph"))["cursor"] == initial["cursor"]
            # A changed membership can arrive with the same graph resourceVersion.
            obj["spec"]["nodes"].append({"name": "two", "kind": "Daemon", "ref": "worker"})
            snapshot = await topology_snapshot(FakeAPI(obj), obj)
            await first.publish(obj, topology=snapshot)
            changes = await second.read(initial["cursor"])
            assert len(changes) == 1 and json.loads(changes[0][1])["type"] == "topology"
            current = await second.topology("Graph", "graph")
            assert current["revision"] == snapshot["revision"]
            assert current["cursor"] == changes[0][0]
            assert len(current["nodes"]) == 2
        finally:
            await first.cache.client.delete(first.key, first.key + ":versions", first.key + ":topologies")
            await first.close()
            await second.close()

    asyncio.run(run())


def test_signals_make_health_require_replacement(monkeypatch):
    """
    SIGHUP leaves health observable; termination marks draining and forwards shutdown.
    """
    from polyad.operator import handlers, runtime

    state = health_state.Lifecycle()
    monkeypatch.setattr(runtime, "lifecycle", state)
    monkeypatch.setattr(handlers, "lifecycle", state)
    callbacks = {}
    monkeypatch.setattr(runtime.signal, "signal", lambda sig, handler: callbacks.setdefault(sig, handler))
    monkeypatch.setattr("sys.argv", ["polyad-operator"])

    class FakeThread:
        def is_alive(self):
            return False

    class FakeRuntime:
        thread = FakeThread()
        stopped = False

        def __init__(self, **options):
            pass

        def start(self):
            callbacks[runtime.signal.SIGHUP](runtime.signal.SIGHUP, None)
            assert not self.stopped
            with pytest.raises(RuntimeError, match="replacement required"):
                handlers.health()
            callbacks[runtime.signal.SIGTERM](runtime.signal.SIGTERM, None)
            assert state.draining.is_set() and self.stopped

        def stop(self):
            self.stopped = True

        def join(self):
            pass

    monkeypatch.setattr(runtime, "OperatorThread", FakeRuntime)
    runtime.main()


@pytest.mark.skipif(not os.environ.get("POLYAD_TEST_DRAGONFLY_URL"), reason="requires an isolated Dragonfly cache")
def test_live_event_server_streams_and_stops_cleanly(monkeypatch):
    """
    Exercise real WSGI streaming, worker bridging and shutdown against the shared cache.
    """
    import urllib.request

    from polyad.events.server import EventServer

    monkeypatch.setenv("POLYAD_CACHE_URL", os.environ["POLYAD_TEST_DRAGONFLY_URL"])

    async def run():
        namespace = f"server-{uuid4()}"
        store = EventStore(os.environ["POLYAD_TEST_DRAGONFLY_URL"], namespace)
        server = EventServer(store, namespace, "subscriber", port=0, connections=1)
        try:
            obj = resource("Graph", "sample")
            obj["metadata"]["namespace"] = namespace
            await store.publish(obj, topology=await topology_snapshot(FakeAPI(obj), obj))

            def receive():
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.server.effective_port}/v1/events",
                    headers={"Authorization": "Bearer subscriber", "Last-Event-ID": "0-0"},
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    lines = []
                    for _ in range(8):
                        line = response.readline().decode()
                        lines.append(line)
                        if line.startswith("data:"):
                            return "".join(lines)
                raise AssertionError("no event received")

            result = await asyncio.to_thread(receive)
            assert '"name": "sample"' in result

            def neighbors():
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.server.effective_port}/v1/graphs/Graph/sample/topology?uid=uid-sample",
                    headers={"Authorization": "Bearer subscriber"},
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    return json.load(response)

            view = await asyncio.to_thread(neighbors)
            assert view["graph"]["namespace"] == namespace and view["cursor"] != "0-0"
        finally:
            await server.close()
            await store.cache.client.delete(store.key, store.key + ":versions", store.key + ":topologies")
            await store.close()
        assert not server.thread.is_alive()

    asyncio.run(run())
