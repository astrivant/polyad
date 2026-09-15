"""Bound Redis and Dragonfly cache I/O and share connection policy across replicas."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ReadOnlyError

if TYPE_CHECKING:
    from typing import Any


def cache_url() -> str:
    """
    Resolve the shared cache endpoint, retaining the original Dragonfly setting.

    Returns:
        str: Redis or TLS Redis connection URL; credentials may come from a Secret.
    """
    return os.environ.get("POLYAD_CACHE_URL") or os.environ.get("POLYAD_DRAGONFLY_URL", "redis://localhost:6379/0")


class Cache:
    """Own an asynchronous connection pool and namespace transient JSON cache entries."""

    def __init__(self, url: str, namespace: str) -> None:
        """
        Configure bounded requests with no blind replay of uncertain writes.

        Args:
            url (str): Redis-compatible server URL, including optional authentication and TLS.
            namespace (str): Operator namespace isolating cache entries.
        """
        self.client: Redis = Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry=Retry(NoBackoff(), 0),
            retry_on_error=[ReadOnlyError],
        )
        self.prefix = f"polyad:{namespace}:cache:"

    async def get(self, key: str) -> Any:
        """
        Read a transient JSON value; misses return None.

        Args:
            key (str): Cache key within this operator namespace.

        Returns:
            Any: Decoded value, or None when the entry is absent.
        """
        value = await self.client.get(self.prefix + key)
        return json.loads(value) if value is not None else None

    async def set(self, key: str, value: Any, *, ttl_seconds: int) -> None:
        """
        Store a JSON value with an explicit bounded lifetime.

        Args:
            key (str): Cache key within this operator namespace.
            value (Any): JSON-serializable transient data.
            ttl_seconds (int): Positive entry lifetime in seconds.

        Returns:
            None: No return value.
        """
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
            raise ValueError("cache TTL must be a positive integer")
        await self.client.set(self.prefix + key, json.dumps(value, allow_nan=False), ex=ttl_seconds)

    async def close(self) -> None:
        """
        Release connections after the replica's cache users have joined.

        Returns:
            None: No return value.
        """
        await self.client.aclose()
