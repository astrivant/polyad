"""Verify shared stream ordering, failover recovery and cache reconstruction."""

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from polyad.operator.shared_queue import SharedQueue
from redis.exceptions import ResponseError


def test_pending_entries_precede_new_work():
    """Never overtake an unacknowledged request when a new replica takes a shard."""

    async def scenario():
        queue = SharedQueue("redis://localhost", "test", "worker")
        queue.client = AsyncMock()
        queue.client.xautoclaim.return_value = ["0-0", [("1-0", {"key": '["Graph", "test", "a"]'})], []]
        assert await queue.take(0) == ("1-0", ("Graph", "test", "a"))
        queue.client.xreadgroup.assert_not_called()
        queue.client.xautoclaim.return_value = ["0-0", [], []]
        queue.client.xreadgroup.return_value = [[queue.stream(0), [("2-0", {"key": '["Graph", "test", "b"]'})]]]
        assert await queue.take(0) == ("2-0", ("Graph", "test", "b"))

    asyncio.run(scenario())


def test_cache_restart_recreates_consumer_groups():
    """Forget cached group existence after the server loses its stream state."""

    async def scenario():
        queue = SharedQueue("redis://localhost", "test", "worker")
        queue.client = AsyncMock()
        queue.groups.add(0)
        queue.client.xautoclaim.side_effect = ResponseError("NOGROUP")
        with pytest.raises(ResponseError):
            await queue.take(0)
        assert 0 not in queue.groups

    asyncio.run(scenario())


@pytest.mark.skipif(not os.environ.get("POLYAD_TEST_DRAGONFLY_URL"), reason="requires an isolated Dragonfly test endpoint")
def test_dragonfly_queue_recovery():
    """Use the real Dragonfly protocol to verify deduplication, ordering and reclaim."""

    async def scenario():
        namespace = f"test-{uuid.uuid4()}"
        first = SharedQueue(os.environ["POLYAD_TEST_DRAGONFLY_URL"], namespace, "first")
        second = SharedQueue(os.environ["POLYAD_TEST_DRAGONFLY_URL"], namespace, "second")
        try:
            key = "Graph", namespace, "root"
            await asyncio.gather(*(first.publish(0, key) for _ in range(10)))
            assert await first.client.xlen(first.stream(0)) == 1
            delivered = await first.take(0)
            assert delivered is not None and delivered[1] == key
            await first.publish(0, ("Graph", namespace, "next"))
            assert await second.take(0) == delivered
            await second.acknowledge(0, delivered[0])
            next_message = await second.take(0)
            assert next_message is not None and next_message[1][2] == "next"
            await second.acknowledge(0, next_message[0])
            assert await second.take(0) is None
            assert await second.client.xlen(second.stream(0)) == 0
            # Simulate cache loss only for this test's keys, then repopulate from intent.
            await first.client.delete(first.stream(0))
            with pytest.raises(ResponseError):
                await second.take(0)
            await first.publish(0, ("Graph", namespace, "recovered"))
            recovered = await second.take(0)
            assert recovered is not None and recovered[1][2] == "recovered"
        finally:
            keys = [key async for key in first.client.scan_iter(match=f"{first.prefix}:*")]
            if keys:
                await first.client.delete(*keys)
            await first.close()
            await second.close()

    asyncio.run(scenario())
