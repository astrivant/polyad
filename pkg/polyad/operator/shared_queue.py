"""Store shard notifications and recover unacknowledged deliveries in Dragonfly."""

import hashlib
import json
import time
from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from polyad.operator.queue import Key

PUBLISH = """
if redis.call('EXISTS', KEYS[2]) == 1 then return false end
local id = redis.call('XADD', KEYS[1], '*', 'key', ARGV[1])
redis.call('SET', KEYS[2], id, 'EX', 5)
return id
"""


class SharedQueue:
    """Use one consumer group per shard, with Kubernetes Leases fencing consumers."""

    def __init__(self, url: str, namespace: str, consumer: str) -> None:
        """Bound cache I/O and isolate keys by operator namespace."""
        self.client: Redis = Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        self.prefix = f"polyad:{namespace}"
        self.consumer = consumer
        self.group = "operators"
        self.groups: set[int] = set()
        self.last_success = 0.0

    def stream(self, shard: int) -> str:
        """Keep related queue keys in the same Redis hash slot."""
        return f"{self.prefix}:{{{shard}}}:work"

    async def publish(self, shard: int, key: Key) -> None:
        """Atomically cache duplicate notifications and append refreshed work hints."""
        encoded = json.dumps(key)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        await cast(Awaitable[Any], self.client.eval(PUBLISH, 2, self.stream(shard), f"{self.prefix}:{{{shard}}}:dedup:{digest}", encoded))
        self.last_success = time.monotonic()

    async def take(self, shard: int) -> tuple[str, Key] | None:
        """Recover the oldest pending delivery before reading newly queued work."""
        stream = self.stream(shard)
        try:
            if shard not in self.groups:
                try:
                    await self.client.xgroup_create(stream, self.group, id="0-0", mkstream=True)
                except ResponseError as error:
                    if "BUSYGROUP" not in str(error):
                        raise
                self.groups.add(shard)
            # Caller holds the shard Lease; even a zero-idle pending delivery is safe to reclaim.
            pending: Any = await self.client.xautoclaim(stream, self.group, self.consumer, 0, "0-0", count=1)
            messages = pending[1]
            if not messages:
                fresh: Any = await self.client.xreadgroup(self.group, self.consumer, {stream: ">"}, count=1)
                messages = fresh[0][1] if fresh else []
            self.last_success = time.monotonic()
            if not messages:
                return None
            message_id, fields = messages[0]
            kind, namespace, name = json.loads(fields["key"])
            return message_id, (kind, namespace, name)
        except ResponseError:
            self.groups.discard(shard)  # A restarted cache may have lost the stream/group.
            raise

    async def acknowledge(self, shard: int, message_id: str) -> None:
        """Remove only an acknowledged attempt; lost replies may cause safe redelivery."""
        async with self.client.pipeline(transaction=True) as transaction:
            transaction.xack(self.stream(shard), self.group, message_id)
            transaction.xdel(self.stream(shard), message_id)
            await transaction.execute()
        self.last_success = time.monotonic()

    async def ping(self) -> None:
        """Keep cache readiness current even on replicas with no assigned shards."""
        await self.client.ping()
        self.last_success = time.monotonic()

    async def close(self) -> None:
        """Close pooled connections after the consumer has joined."""
        await self.client.aclose()
