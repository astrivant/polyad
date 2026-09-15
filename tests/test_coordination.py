"""
Exercise replica contention, failover and write guards with a CAS API.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from unittest.mock import MagicMock, Mock

import pytest

from polyad.operator.api import API
from polyad.operator.controller import FINALIZER, Controller, Pending
from polyad.operator.coordination import DURATION, SHARDS, Coordinator, NotOwner, active_shard, assignment
from tests.test_operator import FakeAPI, resource


class LeaseAPI(FakeAPI):
    """
    Model versioned lease writes with deliberate coroutine interleaving.
    """

    async def request(self, method, kind, namespace, name="", body=None, **kwargs):
        """
        List a fresh snapshot or delegate writes to the compare-and-swap fake.
        """
        await asyncio.sleep(0)
        if method == "GET":
            return {
                "items": [
                    {field: copy.deepcopy(value) for field, value in obj.items() if field not in {"kind", "apiVersion"}}
                    for key, obj in self.objects.items()
                    if key[:2] == (kind, namespace)
                ]
            }
        return await super().request(method, kind, namespace, name, body, **kwargs)


def test_assignment_stability():
    """
    New replicas only take their own shards; survivor assignments stay stable.
    """
    before = assignment(["a", "b"])
    after = assignment(["c", "b", "a"])
    assert len(before) == SHARDS
    assert set(before.values()) == {"a", "b"}
    assert all(after[key] in {value, "c"} for key, value in before.items())
    assert assignment([]) == {}
    assert before == assignment(["b", "a"])


def test_election_contention_and_expiry(monkeypatch):
    """
    Only one CAS contender wins; stale holders cannot write after takeover.
    """
    now = [100.0]
    monkeypatch.setattr("polyad.operator.coordination.time.monotonic", lambda: now[0])

    async def scenario():
        api = LeaseAPI()
        first, second = Coordinator(api, "test", "first"), Coordinator(api, "test", "second")
        results = await asyncio.gather(first.claim("polyad-shard-0"), second.claim("polyad-shard-0"))
        assert sum(results) == 1
        winner, loser = (first, second) if results[0] else (second, first)
        winner.owned.add(0)
        assert not await loser.claim("polyad-shard-0")
        now[0] += DURATION + 1
        assert await loser.claim("polyad-shard-0")
        token = active_shard.set(0)
        try:
            with pytest.raises(NotOwner):
                await winner.guard()
        finally:
            active_shard.reset(token)

    asyncio.run(scenario())


def test_replica_rebalance_and_leader_failover(monkeypatch):
    """
    Scale-out transfers expired shards; a survivor replaces a failed planner.
    """
    now = [100.0]
    monkeypatch.setattr("polyad.operator.coordination.time.monotonic", lambda: now[0])

    async def scenario():
        api = LeaseAPI()
        first, second = Coordinator(api, "test", "first"), Coordinator(api, "test", "second")
        await first.tick()
        assert first.leader and len(first.owned) == SHARDS
        await second.tick()
        await first.tick()
        await second.tick()
        assert not second.leader
        assert not first.owned & second.owned
        now[0] += DURATION + 1
        await first.tick()
        await second.tick()
        assert first.owned and second.owned
        assert first.owned | second.owned == set(range(SHARDS))
        assert not first.owned & second.owned
        # Observe the last renewals before allowing the first process to disappear.
        await second.tick()
        now[0] += DURATION + 1
        await second.tick()
        assert second.leader
        assert second.owned == set(range(SHARDS))

    asyncio.run(scenario())


def test_nested_graphs_and_rewrites_share_duty():
    """
    Target rewrites and nested boundaries serialize with their root graph.
    """

    async def scenario():
        root = resource("Graph", "root")
        child = resource("Feedback", "nested")
        child["metadata"]["ownerReferences"] = [
            {
                "apiVersion": root["apiVersion"],
                "kind": "Graph",
                "name": "root",
                "uid": root["metadata"]["uid"],
                "controller": True,
            }
        ]
        rewrite = resource("Rewrite", "edit", {"graph": "nested", "kind": "Feedback"})
        coordinator = Coordinator(LeaseAPI(root, child, rewrite), "test")
        shards = [await coordinator.shard_for((obj["kind"], "test", obj["metadata"]["name"])) for obj in (root, child, rewrite)]
        assert len(set(shards)) == 1
        with pytest.raises(NotOwner):
            async with coordinator.duty(("Graph", "test", "root")):
                pytest.fail("unowned duty entered")

    asyncio.run(scenario())


def test_renewal_deadline_fails_closed(monkeypatch):
    """
    A slow or unavailable renewal stops admission before the lease expires.
    """
    now = [100.0]
    monkeypatch.setattr("polyad.operator.coordination.time.monotonic", lambda: now[0])

    async def scenario():
        coordinator = Coordinator(LeaseAPI(), "test", "first")
        assert await coordinator.claim("polyad-shard-0")
        coordinator.owned.add(0)
        token = active_shard.set(0)
        try:
            await coordinator.guard()
            now[0] += DURATION - 30
            with pytest.raises(NotOwner):
                await coordinator.guard()
        finally:
            active_shard.reset(token)

    asyncio.run(scenario())


def test_finalizer_is_acknowledged_before_children_and_preserves_others():
    """
    Add and remove the drain finalizer using resource-version fenced patches.
    """

    async def scenario():
        obj = resource("Graph", "root", {"nodes": []})
        obj["metadata"]["finalizers"] = ["example.com/other"]
        api = FakeAPI(obj)
        controller = Controller(api)
        key = "Graph", "test", "root"
        with pytest.raises(Pending):
            await controller.reconcile(key)
        assert api.objects[key]["metadata"]["finalizers"] == ["example.com/other", FINALIZER]
        # The acknowledged finalizer is followed by a separate, refreshed status patch.
        assert api.calls == [("PATCH", "Graph", "root"), ("PATCH", "Graph", "root")]
        assert api.objects[key]["status"]["metrics"]["resources"]["total"] == 0
        api.objects[key]["metadata"]["deletionTimestamp"] = "now"
        await controller.reconcile(key)
        assert api.objects[key]["metadata"]["finalizers"] == ["example.com/other"]
        assert api.objects[key]["status"]["phase"] == "Draining"

    asyncio.run(scenario())


def test_transport_cancellation_joins_outstanding_write():
    """
    Do not leave HTTP running after the surrounding duty exits on cancellation.
    """

    async def scenario():
        started, finish = threading.Event(), threading.Event()
        api = API.__new__(API)
        api.before_write = None
        api.client = Mock()

        def request(*args, **kwargs):
            started.set()
            assert finish.wait(5)

        api.client.call_api.side_effect = request
        task = asyncio.create_task(api.request("POST", "Graph", "test", body={}))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_config_resolution_and_guard(monkeypatch):
    """
    Prefer service-account credentials, fall back locally, and reject unowned writes.
    """
    from kubernetes import config

    from polyad.operator import api as module

    incluster, local = Mock(), Mock()
    monkeypatch.setattr(module.config, "load_incluster_config", incluster)
    monkeypatch.setattr(module.config, "load_kube_config", local)
    monkeypatch.setattr(module.client, "ApiClient", MagicMock())
    API()
    local.assert_not_called()
    incluster.side_effect = config.ConfigException("outside cluster")
    api = API()
    local.assert_called_once()

    async def reject():
        raise NotOwner("lost lease")

    api.before_write = reject
    with pytest.raises(NotOwner):
        asyncio.run(api.request("POST", "Graph", "test", body={}))
    api.client.call_api.assert_not_called()
