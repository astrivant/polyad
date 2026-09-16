"""
Isolate reserved control-plane graphs from public observations, topology and replay.
"""

from __future__ import annotations

import asyncio
import copy
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from polyad.events.builder import EventAPIBuilder
from polyad.events.store import PUBLISH, PUBLISH_TOPOLOGY, READ, SNAPSHOT, EventStore
from polyad.events.topology import topology_snapshot
from polyad.events.visibility import INTERNAL, public_observation
from polyad.operator.controller import Controller
from polyad.operator.coordination import root_shard
from polyad.operator.root import ClusterWorker
from polyad_types.resources import to_document
from tests.test_operator import FakeAPI, resource


def child(kind, name, parent):
    """
    Create an observation with the same controlling owner references as compiled resources.
    """
    obj = resource(kind, name)
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


@pytest.mark.parametrize("deleting", [False, True])
def test_reserved_family_and_requests_never_publish_observations_or_topologies(deleting):
    """
    Hide every descendant and graph-targeted receipt, including terminal observations.
    """

    async def scenario():
        root = resource("Graph", "control-plane")
        group = child("ReplicaGroup", "gateway-copies", root)
        nested = child("Graph", "nested", group)
        objects = [root, group, nested]
        for kind in ("Rewrite", "Activation", "TemporaryConnection"):
            objects.append(resource(kind, kind.lower(), {"kind": "Graph", "graph": "nested", "graphUid": "uid-nested"}))
        if deleting:
            for obj in objects:
                obj["metadata"]["deletionTimestamp"] = "now"
        api = FakeAPI(*objects)
        store = EventStore(
            "redis://localhost", "test", visible=lambda obj: public_observation(api, obj, reserved_graph=("test", "control-plane"))
        )
        cache = AsyncMock()
        store.cache.client = cache
        try:
            for obj in objects:
                await store.publish(obj, topology={"should": "never be serialized"})
            cache.eval.assert_not_awaited()
        finally:
            await store.close()

    asyncio.run(scenario())


def test_marked_definitions_and_compiled_descendants_remain_internal():
    """
    Keep reusable component definitions and their ownership-independent marker private.
    """

    async def scenario():
        root = resource("Graph", "renamed-operator-graph")
        root["metadata"]["labels"] = {INTERNAL: "true"}
        api = FakeAPI(root)
        for kind in ("Graph", "ReplicaGroup", "Daemon", "GraphRule"):
            definition = resource(kind, "definition")
            definition["metadata"]["labels"] = {INTERNAL: "true"}
            assert not await public_observation(api, definition)
        compiled = to_document(Controller(api).child(root, "gateway", "ReplicaGroup", {}))
        compiled["metadata"]["uid"] = "compiled-uid"
        assert compiled["metadata"]["labels"][INTERNAL] == "true"
        assert not await public_observation(FakeAPI(), compiled)
        # Even older descendants without the inherited marker resolve current owners.
        del compiled["metadata"]["labels"][INTERNAL]
        assert not await public_observation(api, compiled)
        assert not await public_observation(FakeAPI(), compiled)

    asyncio.run(scenario())


@pytest.mark.parametrize("broken", ["missing", "replaced", "cycle"])
def test_unresolved_ancestry_does_not_fall_through_to_public_events(broken):
    """
    Reject orphaned and ambiguous ancestry instead of treating it as an application root.
    """

    async def scenario():
        root = resource("Graph", "parent")
        nested = child("ReplicaGroup", "copies", root)
        if broken == "replaced":
            root["metadata"]["uid"] = "different-incarnation"
        elif broken == "cycle":
            root["metadata"]["ownerReferences"] = child("Graph", "unused", nested)["metadata"]["ownerReferences"]
        api = FakeAPI(nested, *([] if broken == "missing" else [root]))
        assert not await public_observation(api, nested)

    asyncio.run(scenario())


def test_applications_on_reserved_shard_and_same_named_remote_graphs_stay_public():
    """
    Apply exact local family identity, without suppressing shard collisions or remote names.
    """

    async def scenario():
        reserved = "test", "control-plane"
        shard = root_shard("Graph", *reserved)
        name = next(f"application-{i}" for i in range(1000) if root_shard("Graph", "test", f"application-{i}") == shard)
        root = resource("Graph", name)
        root["metadata"]["deletionTimestamp"] = "now"
        nested = child("ReplicaGroup", "copies", root)
        request = resource("Rewrite", "change", {"kind": "Graph", "graph": name})
        api = FakeAPI(root, nested, request)
        for obj in (root, nested, request):
            assert await public_observation(api, obj, reserved_graph=reserved)
        other_namespace = resource("Graph", "control-plane")
        other_namespace["metadata"]["namespace"] = "applications"
        assert await public_observation(api, other_namespace, reserved_graph=reserved)
        remote = resource("Graph", "control-plane")
        # A remote store checks its own labels and ancestry, without the root's name reservation.
        assert await public_observation(FakeAPI(remote), remote)

    asyncio.run(scenario())


def test_public_replay_and_topology_never_read_legacy_unfiltered_cache():
    """
    Serve application events and reject internal snapshots even when old cache data remains.
    """

    async def scenario():
        internal = resource("Graph", "control-plane")
        public = resource("Graph", "application", {"mode": "persistent", "nodes": []})
        api = FakeAPI(internal, public)
        store = EventStore(
            "redis://localhost", "test", visible=lambda obj: public_observation(api, obj, reserved_graph=("test", "control-plane"))
        )
        legacy = "polyad:{events:test}:observations"
        streams = {legacy: [("1-0", ["event", json.dumps({"name": "control-plane", "type": "observation"})])]}
        snapshots = {legacy + ":topologies": {"Graph/control-plane": json.dumps({"private": "snapshot"})}}
        cache = AsyncMock()

        async def evaluate(script, count, *args):
            if script == PUBLISH:
                key = args[0]
                streams.setdefault(key, []).append(("2-0", ["event", args[4]]))
            elif script == PUBLISH_TOPOLOGY:
                snapshots.setdefault(args[1], {})[args[2]] = args[3]
                streams.setdefault(args[0], []).append(("3-0", ["event", args[5]]))
            elif script == READ:
                return streams.get(args[0], [])
            elif script == SNAPSHOT:
                value = snapshots.get(args[1], {}).get(args[2])
                return [value, streams[args[0]][-1][0]] if value else None
            else:
                raise AssertionError("unexpected cache operation")

        cache.eval.side_effect = evaluate
        store.cache.client = cache
        try:
            await store.publish(internal, topology=await topology_snapshot(api, internal))
            await store.publish(public, topology=await topology_snapshot(api, public))
            batch = await store.read("0-0")
            assert len(batch) == 2 and all(json.loads(data)["name"] == "application" for _, data in batch)
            snapshot = await store.topology("Graph", "application")
            assert snapshot["graph"]["name"] == "application" and snapshot["cursor"] == "3-0"
            with pytest.raises(KeyError):
                await store.topology("Graph", "control-plane")
            assert legacy in streams and legacy + ":topologies" in snapshots
            assert all(call.args[2] != legacy for call in cache.eval.await_args_list)
            return batch
        finally:
            await store.close()

    batch = asyncio.run(scenario())
    stopping = Event()

    def read(cursor):
        stopping.set()
        return batch

    app = EventAPIBuilder(stopping=stopping).with_handlers(lambda cursor: "0-0", read).with_bearer_token("token").build()
    response = app.test_client().get("/v1/events", headers={"Authorization": "Bearer token"})
    assert "application" in response.text and "control-plane" not in response.text
    response.close()


def test_visibility_failure_cannot_publish_an_unchecked_observation():
    """
    Require successful classification before writes when Kubernetes reads fail.
    """

    async def scenario():
        api = AsyncMock()
        api.get.side_effect = RuntimeError("Kubernetes unavailable")
        obj = child("ReplicaGroup", "copies", resource("Graph", "parent"))
        store = EventStore("redis://localhost", "test", visible=lambda obj: public_observation(api, obj))
        cache = AsyncMock()
        store.cache.client = cache
        try:
            with pytest.raises(RuntimeError, match="Kubernetes unavailable"):
                await store.publish(copy.deepcopy(obj))
            cache.eval.assert_not_awaited()
        finally:
            await store.close()

    asyncio.run(scenario())


def test_remote_publication_uses_destination_ancestry_and_refreshed_api(monkeypatch):
    """
    Wire the same policy into root-held remote streams without reserving a local name there.
    """
    monkeypatch.setenv("POLYAD_CLUSTER_NAME", "management")
    monkeypatch.setenv("POLYAD_FEDERATION_CLUSTERS", "[]")

    async def scenario():
        remote_root = resource("Graph", "remote-operator")
        remote_root["metadata"]["labels"] = {INTERNAL: "true"}
        nested = child("ReplicaGroup", "remote-copies", remote_root)
        application = resource("Graph", "control-plane")
        api = FakeAPI(remote_root, nested, application)
        root = SimpleNamespace(
            state=None,
            coordinator=SimpleNamespace(namespace="test", identity="root", self_graph="control-plane"),
            federation=SimpleNamespace(target=lambda cluster: (api, "test")),
            resolve=lambda cluster: (api, "test"),
        )
        worker = ClusterWorker(root, "west", "test")
        cache = AsyncMock()
        worker.events.cache.client = cache
        try:
            await worker.events.publish(nested)
            cache.eval.assert_not_awaited()
            await worker.events.publish(application)
            assert json.loads(cache.eval.await_args.args[6])["cluster"] == "west"
            # Remote API adapters are replaced as projected credentials rotate.
            nested = child("ReplicaGroup", "application-copies", application)
            assert await worker.events.visible(nested)
            worker.controller.api = FakeAPI()
            await worker.events.publish(nested)
            assert cache.eval.await_count == 1
        finally:
            await worker.events.close()
            await worker.shared.close()

    asyncio.run(scenario())
