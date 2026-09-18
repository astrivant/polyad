"""
Verify public event syntax trees, schema parity and configurable delivery budgets.
"""

from __future__ import annotations

import asyncio
import builtins
import copy
import io
import json
import runpy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import jsonschema
import pytest
from attrs import fields

from polyad.api.events.builder import EventAPIBuilder
from polyad.events.store import EventStore
from polyad_schemas import event_schema
from polyad_sdk import Client
from polyad_types import Event, EventStreamSettings, EventTooLarge, decode_event, to_dict
from tests.test_chart import CHART, render
from tests.test_client import Adapter
from tests.test_operator import resource

EXAMPLES = json.loads((Path(__file__).parent / "data/events.json").read_text())


@pytest.mark.parametrize("document", EXAMPLES)
def test_event_syntax_trees_match_the_shipped_schema(document):
    """
    All observations and controls validate, round-trip and preserve typed nested fields.
    """
    schema = event_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(document, schema)
    ast = decode_event(document)
    assert Event(**document).typed() == ast
    assert ast.event == document["event"]
    lowered = to_dict(ast)
    jsonschema.validate(lowered, schema)
    assert decode_event(lowered) == ast


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(event="custom"),
        lambda d: d.update(id="bad"),
        lambda d: d.update(extra="unknown"),
        lambda d: d["data"].update(nodeCount=True),
        lambda d: d["data"].update(nodeCount=-1),
        lambda d: d["data"].update(valid="true"),
        lambda d: d["data"].pop("snapshot"),
        lambda d: d["data"].update(secret="not-public"),
    ],
)
def test_event_decoder_and_schema_reject_invalid_documents(change):
    """
    Neither contract coerces primitives or silently accepts a different payload shape.
    """
    document = copy.deepcopy(EXAMPLES[1])
    change(document)
    with pytest.raises((ValueError, TypeError, KeyError)):
        decode_event(document)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, event_schema())


def test_event_schema_is_generated_from_models_and_packaged():
    """
    The shipped artifact and Helm ranges cannot drift from the importable tuning models.
    """
    generator = runpy.run_path(str(CHART.parents[1] / "scripts/schemas/generate-event-schemas.py"))
    assert generator["event_schema"]() == event_schema()
    helm = json.loads((CHART / "values.schema.json").read_text())["properties"]["events"]["properties"]
    for attribute in fields(EventStreamSettings):
        assert all(helm[attribute.name][key] == value for key, value in attribute.metadata["schema"].items())


def test_event_service_remains_available_without_the_optional_schema_distribution(monkeypatch):
    """
    Import schema artifacts only for their route and explain a missing extra without breaking event configuration.
    """
    original_import = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "polyad_schemas":
            raise ModuleNotFoundError("missing optional schemas package", name="polyad_schemas")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    app = EventAPIBuilder().with_handlers(lambda _: "0-0", lambda _: []).with_bearer_token("reader").build()
    client = app.test_client()
    headers = {"Authorization": "Bearer reader"}
    assert client.get("/v1/events/config", headers=headers).status_code == 200
    assert client.get("/v1/events/schema").status_code == 401
    response = client.get("/v1/events/schema", headers=headers)
    assert response.status_code == 503
    assert "polyad[schemas]" in response.json["error"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("maxEventBytes", True),
        ("maxEventBytes", 1023),
        ("maxEventBytes", 16777217),
        ("readBatchSize", 0),
        ("readBatchSize", 257),
        ("readBatchSize", 1.5),
        ("pollIntervalSeconds", 0),
        ("pollIntervalSeconds", 6),
        ("pollIntervalSeconds", float("nan")),
    ],
)
def test_invalid_event_tuning_is_rejected(key, value):
    """
    Invalid limits fail before publishers or clients reserve storage and memory.
    """
    with pytest.raises(ValueError):
        EventStreamSettings(**{key: value})


def test_publication_validates_and_bounds_events_before_archive_or_replay():
    """
    An oversized observation never reaches either persistence backend.
    """

    async def run():
        archive = AsyncMock()
        store = EventStore(
            "redis://localhost",
            "test",
            visible=AsyncMock(return_value=True),
            archive=archive,
            settings=EventStreamSettings(maxEventBytes=1024),
        )
        store.cache.client.eval = AsyncMock()
        obj = resource("Graph", "pipeline")
        obj["status"] = {"phase": "Running"}
        try:
            await store.publish(obj)
            payload = archive.call_args.args[0]
            jsonschema.validate({"id": "1-0", "event": "graph", "data": payload}, event_schema())
            archive.reset_mock()
            store.cache.client.eval.reset_mock()
            obj["status"]["throughput"] = {"reason": "x" * 2000}
            with pytest.raises(EventTooLarge):
                await store.publish(obj)
            archive.assert_not_called()
            store.cache.client.eval.assert_not_called()
        finally:
            await store.close()

    asyncio.run(run())


def test_local_and_root_streams_read_environment_tuning(monkeypatch):
    """
    Root-held cluster streams use the same tuning as local streams and pass batches into Lua.
    """
    monkeypatch.setenv("POLYAD_EVENTS_MAX_EVENT_BYTES", "2048")
    monkeypatch.setenv("POLYAD_EVENTS_READ_BATCH_SIZE", "3")
    monkeypatch.setenv("POLYAD_EVENTS_POLL_INTERVAL_SECONDS", "0.2")
    monkeypatch.setenv("POLYAD_EVENTS_RETENTION", "777")

    async def run():
        for cluster in (None, "west"):
            store = EventStore("redis://localhost", "test", cluster=cluster, visible=AsyncMock(return_value=True))
            store.cache.client.eval = AsyncMock(return_value=[])
            sleep = AsyncMock()
            monkeypatch.setattr("polyad.events.store.asyncio.sleep", sleep)
            try:
                assert store.settings == EventStreamSettings(2048, 3, 0.2)
                assert store.retention == 777
                assert await store.read("0-0") == []
                assert store.cache.client.eval.call_args.args[-1] == "3"
                sleep.assert_awaited_once_with(0.2)
            finally:
                await store.close()

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["sse", "websocket"])
def test_delivery_of_old_oversized_records_resets_without_acknowledging(transport):
    """
    Lowering the live limit never truncates or checkpoints an already-retained larger observation.
    """
    settings = EventStreamSettings(maxEventBytes=1024)
    app = (
        EventAPIBuilder(settings=settings, websockets=True)
        .with_handlers(lambda _: "1-0", lambda _: [("2-0", json.dumps({"uid": "large", "status": {"reason": "x" * 2048}}))])
        .with_bearer_token("reader")
        .build()
    )
    client = app.test_client()
    headers = {"Authorization": "Bearer reader"}
    assert client.get("/v1/events/config").status_code == 401
    assert client.get("/v1/events/config", headers=headers).json == to_dict(settings)
    assert client.get("/v1/events/schema", headers=headers).json == event_schema()
    response = client.get(
        "/v1/events/ws" if transport == "websocket" else "/v1/events",
        headers=headers,
        environ_overrides={"polyad.websocket": transport == "websocket"},
    )
    records = list(response.response)
    response.close()
    assert b"reset" in records[-1] and b"2-0" not in records[-1]
    assert all(len(frame) <= 1024 for frame in records)


@pytest.mark.parametrize("transport", ["sse", "websocket"])
def test_client_size_limit_counts_complete_utf8_records_and_closes(monkeypatch, transport):
    """
    Exactly-at-limit frames work, while larger Unicode frames cannot escape the receive cap.
    """
    from polyad_sdk import websocket

    event = Event("1-0", "graph", {"value": "é" * 600})
    raw = (
        event.encode("sse", 2048, raw=json.dumps(event.data, ensure_ascii=False))
        if transport == "sse"
        else json.dumps(to_dict(event), ensure_ascii=False)
    ).encode()
    size = len(raw)
    for limit in (size, size - 1):
        client = Client("http://events", "reader", max_event_bytes=limit)
        if transport == "sse":
            response = io.BytesIO(raw)
            client._open = MagicMock(return_value=response)
        else:
            connector = MagicMock()
            connector.return_value.__enter__.return_value.recv.return_value = raw.decode()
            monkeypatch.setattr(websocket, "_NoRedirect", connector)
        stream = client.events(transport=transport)
        if limit == size:
            assert next(stream) == event
        else:
            with pytest.raises(EventTooLarge):
                next(stream)
        stream.close()
        if transport == "sse":
            assert response.closed
        else:
            assert connector.call_args.kwargs["max_size"] == limit
            connector.return_value.__exit__.assert_called_once()


def test_client_can_read_settings_without_increasing_its_receive_budget():
    """
    An advertised server budget does not silently allocate more client memory.
    """
    app = (
        EventAPIBuilder(settings=EventStreamSettings(2048, 7, 0.5))
        .with_handlers(lambda _: "0-0", lambda _: [])
        .with_bearer_token("key")
        .build()
    )
    client = Client("http://events", "key", max_event_bytes=1024)
    client._opener = Adapter(app)
    assert client.event_settings() == EventStreamSettings(2048, 7, 0.5)
    assert client.max_event_bytes == 1024


def test_helm_passes_event_tuning_to_every_execution_profile():
    """
    All split roles receive identical budgets, including publishers that have no event listener.
    """
    objects = render(
        "ha=true",
        "architecture.mode=Distributed",
        "api.enabled=true",
        "metrics.enabled=true",
        "events.maxEventBytes=2048",
        "events.readBatchSize=7",
        "events.pollIntervalSeconds=0.5",
        values_files=(CHART / "values-events.reference.yaml",),
    )
    roles = [
        obj
        for obj in objects
        if obj["kind"] in {"Daemon", "Deployment"}
        and obj["metadata"]["name"] in {"test-polyad", "test-gateway", "test-executor", "test-telemetry"}
    ]
    assert len(roles) == 4
    for obj in roles:
        env = {item["name"]: item.get("value") for item in obj["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["POLYAD_EVENTS_MAX_EVENT_BYTES"] == "2048"
        assert env["POLYAD_EVENTS_READ_BATCH_SIZE"] == "7"
        assert env["POLYAD_EVENTS_POLL_INTERVAL_SECONDS"] == "0.5"
