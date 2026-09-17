"""
Verify backlog gauges remain truthful during delays, cancellation and outages.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from kubernetes.client.exceptions import ApiException

from polyad.operator.adapters.kubernetes import API
from polyad.operator.coordination.leases import Coordinator, NotOwner
from polyad.operator.coordination.queue import RefreshQueue
from polyad.operator.coordination.shared_queue import SharedQueue
from polyad.operator.observability.metrics import WriteBacklog
from polyad.operator.reconciliation.controller import Controller


def test_write_backlog_serialization_and_cancellation():
    """
    Count queued writes separately and retain in-flight counts until HTTP joins.
    """

    async def scenario():
        started, release = threading.Event(), threading.Event()
        api = API.__new__(API)
        api.max_pending_writes = 2  # Exercise the explicitly enabled burst allowance.
        api.client = Mock()
        calls = []

        def request(path, method, **kwargs):
            if method != "GET":
                calls.append(kwargs["body"]["id"])
                if len(calls) == 1:
                    started.set()
                    assert release.wait(5)

        api.client.call_api.side_effect = request
        first = asyncio.create_task(api.request("POST", "Graph", "test", body={"id": 1}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            second = asyncio.create_task(api.request("POST", "Graph", "test", body={"id": 2}))
            cancelled = asyncio.create_task(api.request("POST", "Graph", "test", body={"id": 3}))
            await asyncio.sleep(0)
            assert api.writes.snapshot()["queued"] == 2
            assert api.writes.snapshot()["inFlight"] == 1
            await api.request("GET", "Graph", "test")
            assert api.writes.snapshot()["total"] == 3
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            first.cancel()
            await asyncio.sleep(0.01)
            assert api.writes.snapshot()["inFlight"] == 1
            assert api.writes.snapshot()["queued"] == 1
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            await second
            assert calls == [1, 2]
            assert api.writes.snapshot()["total"] == 0
        finally:
            release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [NotOwner("lease lost"), ApiException(status=409), TimeoutError("slow API")])
def test_failed_writes_release_gauges(error):
    """
    Ownership rejection and transport errors must not leave phantom write backlog.
    """

    async def scenario():
        api = API.__new__(API)
        api.client = Mock()
        if isinstance(error, NotOwner):

            async def reject():
                assert api.writes.snapshot()["queued"] == 1
                assert api.writes.snapshot()["inFlight"] == 0
                raise error

            api.before_write = reject
        else:
            api.client.call_api.side_effect = error
        with pytest.raises(type(error)):
            await api.request("PATCH", "Graph", "test", "graph", {})
        assert api.writes.snapshot()["total"] == 0

    asyncio.run(scenario())


def test_write_age_tracks_each_stage(monkeypatch):
    """
    Measure waiting age and transport age from their respective start times.
    """
    now = [10.0]
    monkeypatch.setattr("polyad.operator.observability.metrics.time", SimpleNamespace(monotonic=lambda: now[0]))
    metrics = WriteBacklog()
    token = metrics.enqueue()
    now[0] = 13.0
    assert metrics.snapshot()["oldestQueuedSeconds"] == 3
    metrics.dispatch(token)
    now[0] = 17.0
    assert metrics.snapshot()["oldestQueuedSeconds"] == 0
    assert metrics.snapshot()["oldestInFlightSeconds"] == 4
    metrics.finish(token)
    assert metrics.snapshot()["oldestInFlightSeconds"] == 0


def test_health_uses_cached_backlogs_and_preserves_stale_values(monkeypatch):
    """
    Probe snapshots perform no network I/O, aggregate local writes, and flag stale samples.
    """
    from polyad.operator.lifecycle import handlers

    async def scenario():
        api, coordination_api = API.__new__(API), API.__new__(API)
        coordinator = Coordinator(coordination_api, "test")
        coordinator.owned = {0}
        coordinator.last_success = time.monotonic()
        shared = SharedQueue("redis://localhost", "test", "worker")
        shared.client = AsyncMock()
        queue = RefreshQueue(AsyncMock())
        queue.start()
        for name, value in {
            "initialized": True,
            "controller": Controller(api),
            "coordinator": coordinator,
            "shared": shared,
            "queue": queue,
            "background": [],
            "last_api_success": time.monotonic(),
        }.items():
            monkeypatch.setattr(handlers, name, value)
        try:
            assert handlers.health()["backlog"]["inboundUpdates"]["total"] is None
            shared.backlog_sample = time.monotonic(), {0: (5, 2), 1: (7, 1)}
            shared.backlog_sample_ok = True
            api.writes.enqueue()
            token = coordination_api.writes.enqueue()
            coordination_api.writes.dispatch(token)
            result = handlers.health()["backlog"]
            inbound, writes = result["inboundUpdates"], result["kubernetesWrites"]
            assert (inbound["queued"], inbound["unacknowledged"], inbound["total"]) == (9, 3, 12)
            assert inbound["owned"] == {"queued": 3, "unacknowledged": 2, "total": 5}
            assert inbound["fresh"]
            assert (writes["queued"], writes["inFlight"], writes["total"]) == (1, 1, 2)
            assert writes["workloads"]["queued"] == writes["coordination"]["inFlight"] == 1
            shared.backlog_sample = time.monotonic() - 20, shared.backlog_sample[1]
            stale = handlers.health()["backlog"]["inboundUpdates"]
            assert stale["total"] == 12 and not stale["fresh"]
            shared.client.assert_not_called()
            assert not shared.client.mock_calls
        finally:
            await queue.stop()

    asyncio.run(scenario())


def test_failed_sampling_retains_last_known_backlog():
    """
    A cache outage must expose stale counts rather than a misleading empty queue.
    """
    from unittest.mock import MagicMock

    async def scenario():
        shared = SharedQueue("redis://localhost", "test", "worker")
        shared.backlog_sample = time.monotonic(), {0: (3, 1)}
        shared.backlog_sample_ok = True
        shared.client = MagicMock()
        pipeline = shared.client.pipeline.return_value.__aenter__.return_value
        pipeline.eval = Mock(return_value=pipeline)
        pipeline.execute = AsyncMock(side_effect=ConnectionError("cache down"))
        with pytest.raises(ConnectionError):
            await shared.sample_backlog([0])
        result = shared.backlog([0])
        assert result["total"] == 3
        assert not result["fresh"]

    asyncio.run(scenario())
