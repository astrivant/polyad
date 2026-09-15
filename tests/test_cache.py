"""Verify shared cache isolation, configuration and finite connection behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from polyad.cache import Cache, cache_url
from polyad.operator.shared_queue import SharedQueue


def test_cache_endpoint_precedence(monkeypatch):
    """Support generic Redis endpoints while retaining Dragonfly configuration compatibility."""
    monkeypatch.delenv("POLYAD_CACHE_URL", raising=False)
    monkeypatch.setenv("POLYAD_DRAGONFLY_URL", "redis://dragonfly:6379/0")
    assert cache_url() == "redis://dragonfly:6379/0"
    monkeypatch.setenv("POLYAD_CACHE_URL", "rediss://redis:6379/2")
    assert cache_url() == "rediss://redis:6379/2"


def test_cache_json_ttl_and_connection_ownership():
    """Require expiry for cached values and give the replica queue the cache-owned pool."""

    async def scenario():
        queue = SharedQueue("redis://localhost:6379/0", "tenant", "replica")
        assert queue.client is queue.cache.client
        connection = queue.client.connection_pool.connection_kwargs
        assert connection["socket_timeout"] == connection["socket_connect_timeout"] == 5
        await queue.close()
        cache = Cache("redis://localhost:6379/0", "tenant")
        cache.client = AsyncMock()
        await cache.set("key", {"revision": 3}, ttl_seconds=30)
        cache.client.set.assert_awaited_once_with("polyad:tenant:cache:key", '{"revision": 3}', ex=30)
        cache.client.get.return_value = '{"revision": 3}'
        assert await cache.get("key") == {"revision": 3}
        cache.client.get.return_value = None
        assert await cache.get("missing") is None
        for ttl in (0, -1, True, 1.5):
            with pytest.raises(ValueError, match="TTL"):
                await cache.set("key", 1, ttl_seconds=ttl)
        await cache.close()
        cache.client.aclose.assert_awaited_once()

    asyncio.run(scenario())
