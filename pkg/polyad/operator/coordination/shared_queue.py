"""
Store shard notifications and recover unacknowledged deliveries in Dragonfly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, cast

from redis.exceptions import ResponseError

from polyad.cache import Cache

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterable
    from typing import Any

    from polyad.operator.coordination.queue import Key

PUBLISH = """
if redis.call('EXISTS', KEYS[2]) == 1 then return false end
local id = redis.call('XADD', KEYS[1], '*', 'key', ARGV[1])
redis.call('SET', KEYS[2], id, 'EX', 5)
return id
"""


BACKLOG = """
local total = redis.call('XLEN', KEYS[1])
if total == 0 then return {0, 0} end
local pending = redis.pcall('XPENDING', KEYS[1], ARGV[1])
if pending.err then
    if string.find(pending.err, 'NOGROUP', 1, true) then return {total, 0} end
    return redis.error_reply(pending.err)
end
return {total, pending[1]}
"""


class SharedQueue:
    """
    Use one consumer group per shard, with Kubernetes Leases fencing consumers.
    """

    def __init__(self, url: str, namespace: str, consumer: str) -> None:
        """
        Bound cache I/O and isolate keys by operator namespace.

        Args:
            url (str): Redis-compatible Dragonfly connection URL.
            namespace (str): Namespace containing the operator resources.
            consumer (str): Replica identity used for stream delivery ownership.
        """
        self.cache = Cache(url, namespace)
        self.client = self.cache.client
        self.prefix = f"polyad:{namespace}"
        self.consumer = consumer
        self.group = "operators"
        self.groups: set[int] = set()
        self.last_success = 0.0
        self.backlog_sample: tuple[float, dict[int, tuple[int, int]]] | None = None
        self.backlog_sample_ok = False

    def stream(self, shard: int) -> str:
        """
        Keep related queue keys in the same Redis hash slot.

        Args:
            shard (int): Shard index identifying the shared notification stream.

        Returns:
            str: Namespace-scoped Redis stream key with a shard hash tag.
        """
        return f"{self.prefix}:{{{shard}}}:work"

    async def publish(self, shard: int, key: Key) -> None:
        """
        Atomically cache duplicate notifications and append refreshed work hints.

        Args:
            shard (int): Shard index identifying the shared notification stream.
            key (Key): Resource kind, namespace and name to reconcile from fresh API state.

        Returns:
            None: No return value.
        """
        encoded = json.dumps(key)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        message_id = await cast(
            "Awaitable[Any]", self.client.eval(PUBLISH, 2, self.stream(shard), f"{self.prefix}:{{{shard}}}:dedup:{digest}", encoded)
        )
        self.last_success = time.monotonic()
        logger.debug("Shared update published shard=%s kind=%s namespace=%s name=%s coalesced=%s", shard, *key, not bool(message_id))

    async def take(self, shard: int) -> tuple[str, Key] | None:
        """
        Recover the oldest pending delivery before reading newly queued work.

        Args:
            shard (int): Shard index identifying the shared notification stream.

        Returns:
            tuple[str, Key] | None: Message ID and resource key, or None when no work is available.
        """
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
            logger.debug(
                "Shared update delivered consumer=%s shard=%s kind=%s namespace=%s name=%s reclaimed=%s",
                self.consumer,
                shard,
                kind,
                namespace,
                name,
                bool(pending[1]),
            )
            return message_id, (kind, namespace, name)
        except ResponseError:
            self.groups.discard(shard)  # A restarted cache may have lost the stream/group.
            raise

    async def acknowledge(self, shard: int, message_id: str) -> None:
        """
        Remove only an acknowledged attempt; lost replies may cause safe redelivery.

        Args:
            shard (int): Shard index identifying the shared notification stream.
            message_id (str): Stream message ID returned by take.

        Returns:
            None: No return value.
        """
        async with self.client.pipeline(transaction=True) as transaction:
            transaction.xack(self.stream(shard), self.group, message_id)
            transaction.xdel(self.stream(shard), message_id)
            await transaction.execute()
        logger.debug("Shared update acknowledged consumer=%s shard=%s message_id=%s", self.consumer, shard, message_id)
        self.last_success = time.monotonic()

    async def ping(self) -> None:
        """
        Keep cache readiness current even on replicas with no assigned shards.

        Returns:
            None: No return value.
        """
        await self.client.ping()
        self.last_success = time.monotonic()

    async def sample_backlog(self, shards: Iterable[int]) -> None:
        """
        Sample all shard streams without creating missing streams or consumer groups.

        Args:
            shards (Iterable[int]): Shard IDs whose stream backlog should be sampled.

        Returns:
            None: No return value.
        """
        shard_ids = tuple(shards)
        try:
            async with self.client.pipeline(transaction=False) as pipeline:
                for shard in shard_ids:
                    pipeline.eval(BACKLOG, 1, self.stream(shard), self.group)
                results = await pipeline.execute()
            counts = {shard: (int(total), int(pending)) for shard, (total, pending) in zip(shard_ids, results, strict=True)}
            self.backlog_sample = (time.monotonic(), counts)
            self.backlog_sample_ok = True
        except Exception:
            self.backlog_sample_ok = False
            raise

    def backlog(self, owned: Iterable[int]) -> dict[str, Any]:
        """
        Expose cached namespace and owned-shard gauges with explicit sample freshness.

        Args:
            owned (Iterable[int]): Shards currently owned by this replica.

        Returns:
            dict[str, Any]: Namespace and owned-shard counts with sample age and freshness.
        """
        sample = self.backlog_sample
        if sample is None:
            return {
                "scope": "namespace",
                "queued": None,
                "unacknowledged": None,
                "total": None,
                "owned": None,
                "sampleAgeSeconds": None,
                "fresh": False,
            }
        sampled_at, counts = sample

        def totals(shards: Iterable[int]) -> dict[str, int]:
            total = sum(counts.get(shard, (0, 0))[0] for shard in shards)
            pending = sum(counts.get(shard, (0, 0))[1] for shard in shards)
            return {"queued": total - pending, "unacknowledged": pending, "total": total}

        age = time.monotonic() - sampled_at
        return {
            "scope": "namespace",
            **totals(tuple(counts)),
            "owned": totals(tuple(owned)),
            "sampleAgeSeconds": age,
            "fresh": self.backlog_sample_ok and age < 15,
        }

    async def close(self) -> None:
        """
        Close pooled connections after the consumer has joined.

        Returns:
            None: No return value.
        """
        await self.cache.close()
