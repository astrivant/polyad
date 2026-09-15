"""
Verify telemetry ownership, freshness, series replacement and read-only HTTP behavior.
"""

from __future__ import annotations

import copy
import json
import time

import pytest
from openapi_spec_validator import validate
from prometheus_client.parser import text_string_to_metric_families

from polyad.metrics.builder import MetricsAPIBuilder
from polyad.metrics.inventory import inventory
from polyad.metrics.store import MetricsStore
from polyad.operator.api import GROUP, VERSION
from polyad.operator.coordination import root_shard


def graph(name, kind="Graph", parent=None, **spec):
    """
    Create a graph with direct resource and recursive observations that deliberately differ.
    """
    obj = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": kind,
        "metadata": {"name": name, "namespace": "test", "uid": name + "-uid", "generation": 1},
        "spec": spec,
        "status": {
            "observedGeneration": 1,
            "phase": "Running",
            "metrics": {
                "observedGeneration": 1,
                "resources": {"total": 2, "byKind": {"Job": 2}},
                "rollup": {"resourceCount": 99},
                "topology": {"nodeCount": 2, "admission": {"breadth": 2, "depth": 1, "edgeCount": 0}},
            },
        },
    }
    if parent:
        obj["metadata"]["ownerReferences"] = [
            {
                "apiVersion": parent["apiVersion"],
                "kind": parent["kind"],
                "name": parent["metadata"]["name"],
                "uid": parent["metadata"]["uid"],
                "controller": True,
            }
        ]
    return obj


def snapshot(objects=()):
    """
    Supply namespace and replica observations without any external clients.
    """
    return {
        "namespace": "test",
        "replica": "replica-a",
        "leader": True,
        "shards": [0],
        "pending": 2,
        "writes": {writer: {"queued": 3, "inFlight": 1} for writer in ("workloads", "coordination", "compositionIntake")},
        "inbound": {"fresh": True, "sampleAgeSeconds": 0},
        "shardBacklogs": {0: (8, 3), 1: (0, 0)},
        "inventory": {**inventory(list(objects)), "fresh": True, "sampleAgeSeconds": 0},
    }


def samples(store):
    """
    Decode actual Prometheus wire data for assertions.
    """
    return [sample for family in text_string_to_metric_families(store.read()[0].decode()) for sample in family.samples]


def test_inventory_uid_fences_hierarchies_and_separates_definitions():
    """
    Nested children use their root's shard while stale ownership cannot attach to a replacement.
    """
    root = graph("root", "PolyGraph")
    child = graph("child", parent=root)
    leaf = graph("leaf", parent=child)
    definition = graph("template", templateOnly=True)
    data = inventory([leaf, definition, root, child])
    records = {obj["name"]: obj for obj in data["objects"]}
    assert records["leaf"]["depth"] == 2
    assert records["leaf"]["parent"]["name"] == "child"
    assert records["leaf"]["root"]["name"] == "root"
    assert records["leaf"]["shard"] == root_shard("PolyGraph", "test", "root")
    assert records["template"]["role"] == "definition"
    root["metadata"]["uid"] = "replacement"
    assert all(not obj["hierarchyComplete"] for obj in inventory([root, child, leaf])["objects"] if obj["name"] != "root")
    child["metadata"]["generation"] = 2
    assert inventory([child])["objects"][0]["resources"] is None


def test_rewrite_duty_uses_target_family_and_cycles_are_unknown():
    """
    Rewrite queue groups follow target graphs without inventing controller ownership.
    """
    root = graph("root", "PolyGraph")
    child = graph("child", parent=root)
    rewrite = graph("change", "Rewrite", graph="child")
    records = {obj["name"]: obj for obj in inventory([root, child, rewrite])["objects"]}
    assert records["change"]["shard"] == records["child"]["shard"]
    assert records["change"]["parent"] is None
    root["metadata"]["ownerReferences"] = copy.deepcopy(child["metadata"]["ownerReferences"])
    assert not inventory([root])["objects"][0]["hierarchyComplete"]


def test_prometheus_shared_counts_direct_resources_and_cardinality():
    """
    Keep shared state separate from writes and avoid adding recursive resource rollups twice.
    """
    store = MetricsStore()
    root = graph("root", "PolyGraph")
    store.publish(snapshot([root, graph("child", parent=root)]))
    values = samples(store)
    assert not any(sample.name == "polyad_hierarchy_info" for sample in values)
    assert next(sample.value for sample in values if sample.name == "polyad_observed_resources") == 4
    queue = [sample for sample in values if sample.name == "polyad_inbound_updates" and sample.labels["shard"] == "0"]
    assert {sample.labels["state"]: sample.value for sample in queue} == {"queued": 5, "unacknowledged": 3}
    assert all(sample.labels["replica"] == "replica-a" for sample in values)
    store.publish(snapshot([root]), graph_labels=True)
    assert len([sample for sample in samples(store) if sample.name == "polyad_hierarchy_info"]) == 1
    assert (
        next(sample.value for sample in samples(store) if sample.name == "polyad_graph_shape" and sample.labels["dimension"] == "nodes")
        == 2
    )
    store.publish(snapshot())
    assert not any(sample.name == "polyad_hierarchy_info" for sample in samples(store))
    assert next(sample.value for sample in samples(store) if sample.name == "polyad_tracked_objects_total_count") == 0


def test_stale_samples_disappear_without_fabricating_zero_demand():
    """
    Preserve stale JSON observations while omitting them from actionable Prometheus gauges.
    """
    store = MetricsStore()
    data = snapshot([graph("root")])
    data["inbound"]["fresh"] = False
    data["inventory"]["fresh"] = False
    store.publish(data)
    values = samples(store)
    assert not any(sample.name in {"polyad_inbound_updates", "polyad_tracked_objects"} for sample in values)
    assert json.loads(store.read()[1])["inventory"]["total"] == 1
    monkeytime = time.monotonic() - 16
    with store.lock:
        store.published = (monkeytime, b"stale", b"stale")
    assert store.read() is None


def test_api_builder_schema_snapshot_and_retirement(monkeypatch):
    """
    Serve valid OpenAPI and both formats; never return empty success before sampling or while retiring.
    """
    from polyad.metrics import builder
    from polyad.operator.health import Lifecycle

    state = Lifecycle()
    monkeypatch.setattr(builder, "lifecycle", state)
    empty = MetricsAPIBuilder()
    with pytest.raises(ValueError):
        empty.build()
    store = MetricsStore()
    app = empty.with_store(store).build().test_client()
    assert empty.store is None
    validate(app.get("/openapi.json").json)
    assert app.get("/metrics").status_code == 503
    store.publish(snapshot())
    assert app.get("/metrics").content_type.startswith("text/plain")
    assert app.get("/v1/metrics").json["namespace"] == "test"
    assert app.post("/metrics").status_code == 405
    assert app.get("/metrics").headers["Cache-Control"] == "no-store"
    state.replacement.set()
    assert app.get("/metrics").status_code == 503


def test_rescan_retains_complete_inventory_on_partial_failure(monkeypatch):
    """
    A failed namespace scan cannot turn missing kinds into apparent deletions.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from polyad.operator import handlers

    async def scenario():
        original = (time.monotonic(), inventory([graph("old")]))
        request = AsyncMock(side_effect=[{"items": [graph("new")]}, RuntimeError("API timed out")])
        monkeypatch.setattr(handlers, "coordinator", SimpleNamespace(api=SimpleNamespace(request=request), namespace="test"))
        monkeypatch.setattr(handlers, "queue", object())
        monkeypatch.setattr(handlers, "metrics_http", object())
        monkeypatch.setattr(handlers, "inventory_sample", original)
        monkeypatch.setattr(handlers, "inventory_sample_ok", True)
        monkeypatch.setattr(handlers, "publish", AsyncMock())
        monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await handlers.rescan_loop()
        assert handlers.inventory_sample is original
        assert not handlers.inventory_sample_ok
        request.side_effect = [{"items": [graph("new")]}] + [{"items": []}] * 12
        with pytest.raises(asyncio.CancelledError):
            await handlers.rescan_loop()
        assert handlers.inventory_sample_ok
        assert handlers.inventory_sample[1]["objects"][0]["name"] == "new"

    asyncio.run(scenario())
