"""
Exercise workload neighbor discovery across structural edits and replica lifecycle changes.
"""

from __future__ import annotations

import asyncio
import json
import time
from threading import Event
from unittest.mock import AsyncMock

import pytest

from polyad.api.events.builder import EventAPIBuilder
from polyad.events.store import EventStore, TopologyReplaced
from polyad.events.topology import neighbors, topology_snapshot
from polyad.operator.lifecycle import handlers
from polyad.operator.reconciliation.controller import Controller
from polyad_sdk import APIError, Client
from polyad_types.resources import GROUP
from tests.test_client import Adapter
from tests.test_operator import FakeAPI, resource, template
from tests.test_replication import group, policy_family, start_family, turn


def test_topology_revision_ignores_heartbeats_and_order_but_detects_connections():
    """
    Changes in identity, ports, dependencies and membership count; status heartbeats do not.
    """

    async def scenario():
        obj = resource(
            "Graph",
            "pipeline",
            {
                "nodes": [{"name": name, "kind": "Workload", "ref": "worker"} for name in ("one", "two", "three")],
                "connections": [
                    {"source": "one", "target": "two", "ports": [{"port": 80}, {"port": 443}]},
                    {"source": "two", "target": "three"},
                ],
            },
        )
        api = FakeAPI(obj)
        first = await topology_snapshot(api, obj)
        obj["metadata"].update(resourceVersion="999", generation=2)
        obj["status"] = {"phase": "Running", "metricsObservedAt": "now", "secret": "never-publish"}
        obj["spec"]["nodes"].reverse()
        obj["spec"]["connections"].reverse()
        obj["spec"]["connections"][1]["ports"].reverse()
        assert await topology_snapshot(api, obj) == first
        obj["spec"]["connections"][0]["ports"] = [{"port": 8080}]
        changed = await topology_snapshot(api, obj)
        assert changed["revision"] != first["revision"]
        selected = neighbors(changed, "two")
        assert selected["incoming"][0]["node"]["name"] == "one"
        assert selected["outgoing"][0]["node"]["name"] == "three"
        assert selected["outgoing"][0]["ports"] == [{"port": 8080, "protocol": "TCP"}]
        obj["spec"]["nodes"][1]["requires"] = [{"node": "one", "condition": "completed"}]
        dependency = await topology_snapshot(api, obj)
        assert dependency["revision"] != changed["revision"]
        assert neighbors(dependency, "two")["dependencies"][0]["node"]["name"] == "one"
        obj["metadata"]["uid"] = "replacement"
        assert (await topology_snapshot(api, obj))["revision"] != dependency["revision"]
        assert "never-publish" not in json.dumps(dependency)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["Independent", "Ring", "Custom"])
def test_replica_scaling_changes_desired_and_observed_neighbors_separately(mode):
    """
    Detect desired scale-out, admission, termination and eventual disappearance independently.
    """

    async def scenario():
        connectivity = {"mode": mode}
        if mode == "Custom":
            connectivity["edges"] = [{"source": "replica-0", "target": "replica-2"}]
        api = FakeAPI(group(2, connectivity=connectivity), resource("Daemon", "worker", {"template": template(True)}))
        await turn(api)
        root = api.objects[("ReplicaGroup", "test", "copies")]
        first = await topology_snapshot(api, root)
        root["spec"]["replicas"] = 3
        root["metadata"]["generation"] += 1
        requested = await topology_snapshot(api, root)
        assert requested["revision"] != first["revision"]
        new_node = next(node for node in requested["nodes"] if node["name"] == "replica-2")
        assert new_node["desired"] and new_node["executions"] == []
        await turn(api)
        admitted = await topology_snapshot(api, root)
        assert admitted["revision"] != requested["revision"]
        new_node = next(node for node in admitted["nodes"] if node["name"] == "replica-2")
        assert len(new_node["executions"]) == 1
        root["spec"]["replicas"] = 2
        root["metadata"]["generation"] += 1
        await turn(api)
        retiring = await topology_snapshot(api, root)
        old_node = next(node for node in retiring["nodes"] if node["name"] == "replica-2")
        assert not old_node["desired"] and old_node["executions"][0]["terminating"]
        for key, child in list(api.objects.items()):
            if child["metadata"].get("deletionTimestamp"):
                del api.objects[key]
        removed = await topology_snapshot(api, root)
        assert removed["revision"] != retiring["revision"]
        assert {node["name"] for node in removed["nodes"]} == {"replica-0", "replica-1"}
        if mode == "Ring":
            outgoing = neighbors(admitted, "replica-1")["outgoing"]
            assert outgoing[0]["node"]["name"] == "replica-2"
            assert neighbors(removed, "replica-1")["outgoing"][0]["node"]["name"] == "replica-0"

    asyncio.run(scenario())


def test_shared_source_changes_are_visible_even_when_rules_block_admission():
    """
    Resolve live counts instead of trusting stale instance specs or status measurements.
    """

    async def scenario():
        api = policy_family(bound=3)
        source = api.objects[("ReplicaGroup", "test", "copies")]
        source["spec"]["connectivity"] = {"mode": "Ring"}
        (instance,) = await start_family(api)
        first = await topology_snapshot(api, instance)
        source["spec"]["replicas"] = 3
        source["metadata"]["generation"] += 1
        with pytest.raises(ValueError, match="expandedNodes=4"):
            await turn(api, instance["metadata"]["name"])
        updated = await topology_snapshot(api, instance)
        assert updated["revision"] != first["revision"]
        assert instance["spec"]["replicas"] == 2
        assert len(updated["nodes"]) == 3
        assert sum(len(node["executions"]) for node in updated["nodes"]) == 2
        source["metadata"]["uid"] = "replaced"
        unavailable = await topology_snapshot(api, instance)
        assert not unavailable["valid"] and not unavailable["connections"]
        assert sum(len(node["executions"]) for node in unavailable["nodes"]) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
def test_native_replica_counts_and_execution_replacement_change_membership(kind):
    """
    Notify internal native scaling without promoting individual Pods to graph vertices.
    """

    async def scenario():
        obj = resource("Graph", "root", {"mode": "persistent", "nodes": [{"name": "service", "kind": "Daemon", "ref": "server"}]})
        child = resource(kind, "server", {"replicas": 1, "template": {"secret": "never-publish"}})
        child["metadata"].update(ownerReferences=[{"uid": obj["metadata"]["uid"]}], labels={f"{GROUP}/node": "service"})
        api = FakeAPI(obj, child)
        first = await topology_snapshot(api, obj)
        stored = api.objects[(kind, "test", "server")]
        stored["spec"]["replicas"] = 3
        updated = await topology_snapshot(api, obj)
        assert updated["revision"] != first["revision"]
        assert len(updated["nodes"]) == 1
        assert updated["nodes"][0]["executions"][0]["replicas"] == 3
        stored["metadata"]["uid"] = "new-execution"
        assert (await topology_snapshot(api, obj))["revision"] != updated["revision"]
        assert "never-publish" not in json.dumps(updated)

    asyncio.run(scenario())


def test_operator_publishes_topology_after_guarding_ownership(monkeypatch):
    """
    Refresh execution membership on the same post-reconciliation path used for lifecycle events.
    """

    async def scenario():
        api = FakeAPI(group())
        publisher = AsyncMock()
        coordinator = AsyncMock()
        monkeypatch.setattr(handlers, "controller", Controller(api))
        monkeypatch.setattr(handlers, "coordinator", coordinator)
        monkeypatch.setattr(handlers, "events", publisher)
        await handlers.publish_observation(("ReplicaGroup", "test", "copies"))
        coordinator.guard.assert_awaited_once()
        snapshot = publisher.publish.call_args.kwargs["topology"]
        assert snapshot["graph"]["uid"] == "uid-copies"
        assert len(snapshot["nodes"]) == 2
        coordinator.guard.side_effect = RuntimeError("ownership lost")
        with pytest.raises(RuntimeError, match="ownership lost"):
            await handlers.publish_observation(("ReplicaGroup", "test", "copies"))
        assert publisher.publish.await_count == 1

    asyncio.run(scenario())


def test_nested_boundaries_and_activation_members_keep_their_own_identities():
    """
    Describe ancestor peers as graph instances and pulse executions as members of a logical node.
    """

    async def scenario():
        api = policy_family(bound=10, replicas=1, uses=("left", "right"))
        root = api.objects[("PolyGraph", "test", "root")]
        root["spec"]["connections"] = [{"source": "left", "target": "right"}]
        left, right = await start_family(api)
        snapshot = await topology_snapshot(api, root)
        peer = neighbors(snapshot, "left")["outgoing"][0]["node"]
        assert peer["kind"] == "ReplicaGroup" and peer["executions"][0]["uid"] == right["metadata"]["uid"]
        child = next(
            item for item in api.children("Deployment") if item["metadata"]["ownerReferences"][0]["uid"] == left["metadata"]["uid"]
        )
        child["metadata"]["labels"][f"{GROUP}/runtime-node"] = "replica-0-pulse-123"
        child["metadata"]["name"] = "pulse-execution"
        view = await topology_snapshot(api, left)
        assert view["nodes"][0]["name"] == "replica-0"
        assert view["nodes"][0]["executions"][0]["runtimeNode"] == "replica-0-pulse-123"

    asyncio.run(scenario())


def test_snapshot_api_and_client_bootstrap_before_receiving_topology_events():
    """
    Use a UID-fenced neighbor snapshot and resume after its cursor through the client API.
    """
    snapshot = {"graph": {"uid": "incarnation"}, "revision": "structure-a", "cursor": "4-0", "node": {"name": "replica-1"}, "outgoing": []}
    stopping = Event()

    def read(cursor):
        stopping.set()
        assert cursor == "4-0"
        return [("5-0", '{"type":"topology","uid":"incarnation","revision":"structure-b"}')]

    def get_snapshot(kind, name, uid, node):
        assert (kind, name, node) == ("ReplicaGroup", "copies", "replica-1")
        if uid != "incarnation":
            raise TopologyReplaced("replaced")
        return snapshot

    app = (
        EventAPIBuilder(stopping=stopping)
        .with_handlers(lambda cursor: cursor or "4-0", read)
        .with_topology_handler(get_snapshot)
        .with_bearer_token("token")
        .build()
    )
    client = Client("http://events:8091", "token")
    client._opener = Adapter(app)
    current = client.topology(kind="ReplicaGroup", graph="copies", graph_uid="incarnation", node="replica-1")
    assert current == snapshot
    with pytest.raises(APIError) as error:
        client.topology(kind="ReplicaGroup", graph="copies", graph_uid="old", node="replica-1")
    assert error.value.status == 409
    events = list(client.events(last_event_id=current["cursor"]))
    assert events[0].event == "topology" and events[0].id == "5-0"


def test_snapshot_store_rejects_stale_or_replaced_graphs_and_selects_neighbors():
    """
    Recover snapshots without returning stale membership or another graph incarnation.
    """

    async def scenario():
        snapshot = await topology_snapshot(FakeAPI(), group(connectivity={"mode": "Ring"}))
        snapshot["observedAt"] = time.time()
        store = EventStore("redis://localhost", "test", visible=AsyncMock(return_value=True))
        client = AsyncMock()
        client.eval.return_value = [json.dumps(snapshot), "15-0"]
        store.cache.client = client
        selected = await store.topology("ReplicaGroup", "copies", "uid-copies", "replica-0")
        assert selected["cursor"] == "15-0" and selected["outgoing"][0]["node"]["name"] == "replica-1"
        with pytest.raises(TopologyReplaced):
            await store.topology("ReplicaGroup", "copies", "old")
        snapshot["observedAt"] -= 31
        client.eval.return_value = [json.dumps(snapshot), "15-0"]
        with pytest.raises(RuntimeError, match="stale"):
            await store.topology("ReplicaGroup", "copies")
        client.eval.return_value = None
        with pytest.raises(KeyError):
            await store.topology("ReplicaGroup", "missing")
        await store.close()

    asyncio.run(scenario())


def test_topology_publication_does_not_depend_on_resource_version_changes():
    """
    Publish membership independently because children and inherited counts can change first.
    """

    async def scenario():
        obj = group(connectivity={"mode": "Ring"})
        api = FakeAPI(obj)
        store = EventStore("redis://localhost", "test", visible=AsyncMock(return_value=True))
        client = AsyncMock()
        client.eval.return_value = False
        store.cache.client = client
        first = await topology_snapshot(api, obj)
        await store.publish(obj, topology=first)
        obj["spec"]["replicas"] = 3
        second = await topology_snapshot(api, obj)
        await store.publish(obj, topology=second)
        calls = client.eval.await_args_list
        assert len(calls) == 4
        assert calls[0].args[5] == calls[2].args[5] == "1"
        assert calls[1].args[6] != calls[3].args[6]
        assert json.loads(calls[3].args[7])["type"] == "topology"
        await store.close()

    asyncio.run(scenario())
