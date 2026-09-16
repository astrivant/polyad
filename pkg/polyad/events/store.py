"""
Publish namespace observations to a bounded Redis stream shared by operator replicas.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

from redis.exceptions import ResponseError

from polyad.cache import Cache
from polyad.events.topology import neighbors
from polyad_types.resources import GROUP

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

PUBLISH = """
if redis.call('HGET', KEYS[2], ARGV[1]) == ARGV[2] then return false end
if redis.call('HLEN', KEYS[2]) >= tonumber(ARGV[4]) and redis.call('HEXISTS', KEYS[2], ARGV[1]) == 0 then
    redis.call('DEL', KEYS[2])
end
local id = redis.call('XADD', KEYS[1], 'MAXLEN', ARGV[4], '*', 'event', ARGV[3])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('EXPIRE', KEYS[2], 86400)
return id
"""


READ = """
local first = redis.call('XRANGE', KEYS[1], '-', '+', 'COUNT', 1)
local function older(a, b)
    local am, as = string.match(a, '^(%d+)%-(%d+)$')
    local bm, bs = string.match(b, '^(%d+)%-(%d+)$')
    if #am ~= #bm then return #am < #bm end
    if am ~= bm then return am < bm end
    if #as ~= #bs then return #as < #bs end
    return as < bs
end
if ARGV[1] ~= '0-0' and (#first == 0 or older(ARGV[1], first[1][1])) then
    return redis.error_reply('CURSOR_EXPIRED')
end
return redis.call('XRANGE', KEYS[1], '(' .. ARGV[1], '+', 'COUNT', 64)
"""

PUBLISH_TOPOLOGY = """
local previous = redis.call('HGET', KEYS[2], ARGV[1])
local changed = not previous or cjson.decode(previous).revision ~= ARGV[3]
if not previous and redis.call('HLEN', KEYS[2]) >= tonumber(ARGV[5]) then
    redis.call('DEL', KEYS[2])
end
local id = false
if changed then
    id = redis.call('XADD', KEYS[1], 'MAXLEN', ARGV[5], '*', 'event', ARGV[4])
end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('EXPIRE', KEYS[2], 86400)
return id
"""

SNAPSHOT = """
local snapshot = redis.call('HGET', KEYS[2], ARGV[1])
if not snapshot then return false end
local last = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1)
return {snapshot, #last > 0 and last[1][1] or '0-0'}
"""


class TopologyReplaced(ValueError):
    """
    Reject a snapshot belonging to a replacement graph incarnation.
    """


class CursorExpired(ValueError):
    """
    Require a fresh Kubernetes/API snapshot after the bounded replay window expires.
    """


class EventStore:
    """
    Share at-least-once observation delivery without retaining workload payloads or credentials.
    """

    def __init__(
        self,
        url: str,
        namespace: str,
        *,
        visible: Callable[[dict[str, Any]], Awaitable[bool]],
        retention: int = 10000,
        cluster: str | None = None,
        ancestry: Callable[[dict[str, Any]], Awaitable[list[dict[str, str]]]] | None = None,
        archive: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        """
        Configure the namespace stream and maximum retained event count.

        Args:
            url (str): Shared Redis or Dragonfly URL.
            namespace (str): Namespace visible to subscribers.
            visible (Callable[[dict[str, Any]], Awaitable[bool]]): Required graph-family visibility check before publication.
            retention (int): Maximum retained observations and deduplication identities.
            cluster (str | None): Remote stream identity when reports are held at the root.
            ancestry (Callable[[dict[str, Any]], Awaitable[list[dict[str, str]]]] | None): Verified graph ownership reader.
            archive (Callable[[dict[str, Any]], Awaitable[None]] | None): Optional durable archive of approved event payloads.
        """
        if not 100 <= retention <= 100000:
            raise ValueError("event retention must be between 100 and 100000")
        self.cache = Cache(url, namespace)
        # Separate approved public observations from older, unfiltered replay and
        # topology caches. Readers never fall back to the previous namespace stream.
        self.key = f"polyad:{{events:{namespace}}}:public-v1:observations"
        self.visible = visible
        self.retention = retention
        self.cluster = cluster
        self.ancestry = ancestry
        self.archive = archive

    async def publish(self, obj: dict[str, Any], *, topology: dict[str, Any] | None = None) -> None:
        """
        Atomically deduplicate an observed revision and append a small audit-linked event.

        Args:
            obj (dict[str, Any]): Fresh graph or composition observation from the owning shard.
            topology (dict[str, Any] | None): Current neighbor and execution snapshot for a graph boundary.

        Returns:
            None: No return value.
        """
        if not await self.visible(obj):
            return
        meta, status = obj["metadata"], obj.get("status", {})
        ancestry = await self.ancestry(obj) if self.ancestry else []
        metrics = status.get("metrics", {})
        payload = {
            **({"cluster": self.cluster} if self.cluster else {}),
            "type": "deleting" if meta.get("deletionTimestamp") else "observation",
            "apiVersion": obj["apiVersion"],
            "kind": obj["kind"],
            "namespace": meta["namespace"],
            "name": meta["name"],
            "uid": meta["uid"],
            "resourceVersion": meta["resourceVersion"],
            "generation": meta.get("generation", 1),
            "owners": [{key: owner[key] for key in ("kind", "name", "uid")} for owner in meta.get("ownerReferences", [])],
            "ancestry": ancestry,
            "audit": {
                key: value
                for key, value in meta.get("labels", {}).items()
                if key.startswith(f"{GROUP}/") and any(part in key for part in ("request", "composition", "node"))
            },
            "status": {
                key: status[key]
                for key in ("phase", "ready", "completed", "failed", "observedGeneration", "activations", "throughput")
                if key in status
            },
            "resources": metrics.get("resources", {}),
        }
        if self.archive is not None:
            await self.archive(payload)
        await cast(
            "Awaitable[Any]",
            self.cache.client.eval(
                PUBLISH, 2, self.key, self.key + ":versions", meta["uid"], meta["resourceVersion"], json.dumps(payload), str(self.retention)
            ),
        )
        if topology is not None:
            snapshot = {**topology, "observedAt": time.time(), "ancestry": ancestry}
            if self.cluster:
                snapshot["graph"] = {**snapshot["graph"], "cluster": self.cluster}
            event = {
                **({"cluster": self.cluster} if self.cluster else {}),
                **{key: payload[key] for key in ("apiVersion", "kind", "namespace", "name", "uid", "generation", "resourceVersion")},
                "type": "topology",
                "ancestry": ancestry,
                "revision": topology["revision"],
                "snapshot": f"/v1/graphs/{obj['kind']}/{meta['name']}/topology"
                + ("?" + urlencode({"cluster": self.cluster}) if self.cluster else ""),
                "valid": topology["valid"],
                "nodeCount": len(topology["nodes"]),
                "connectionCount": len(topology["connections"]),
            }
            if self.archive is not None:
                await self.archive(event)
            await cast(
                "Awaitable[Any]",
                self.cache.client.eval(
                    PUBLISH_TOPOLOGY,
                    2,
                    self.key,
                    self.key + ":topologies",
                    f"{obj['kind']}/{meta['name']}",
                    json.dumps(snapshot, separators=(",", ":")),
                    topology["revision"],
                    json.dumps(event),
                    str(self.retention),
                ),
            )

    async def topology(self, kind: str, name: str, uid: str | None = None, node: str | None = None) -> dict[str, Any]:
        """
        Read fresh neighbors and an atomic stream cursor for race-free subscription startup.

        Args:
            kind (str): Graph boundary kind.
            name (str): Graph name within this event store's namespace.
            uid (str | None): Expected graph incarnation, when known.
            node (str | None): Restrict the response to one node's neighbors.

        Returns:
            dict[str, Any]: Complete snapshot or node neighbors, with a resumable cursor.
        """
        result = await cast("Awaitable[Any]", self.cache.client.eval(SNAPSHOT, 2, self.key, self.key + ":topologies", f"{kind}/{name}"))
        if not result:
            raise KeyError(name)
        snapshot = json.loads(result[0])
        if uid is not None and snapshot["graph"]["uid"] != uid:
            raise TopologyReplaced("graph UID changed; refresh graph identity")
        if not 0 <= time.time() - snapshot["observedAt"] <= 30:
            raise RuntimeError("topology observation is stale; retry after reconciliation")
        snapshot["cursor"] = result[1]
        return neighbors(snapshot, node) if node is not None else cast("dict[str, Any]", snapshot)

    async def cursor(self, supplied: str | None) -> str:
        """
        Validate a reconnect cursor or begin after the latest retained observation.

        Args:
            supplied (str | None): Last-Event-ID header, or None for live observations.

        Returns:
            str: Redis stream cursor valid at the time of this read.
        """
        if supplied is not None and not re.fullmatch(r"(?:0|[1-9][0-9]{0,19})-(?:0|[1-9][0-9]{0,19})", supplied):
            raise ValueError("Last-Event-ID must be a Redis stream ID")
        first = await self.cache.client.xrange(self.key, count=1)
        if supplied is not None:
            if supplied != "0-0" and (not first or tuple(map(int, supplied.split("-"))) < tuple(map(int, first[0][0].split("-")))):
                raise CursorExpired("event cursor expired; refresh graph status before reconnecting")
            last = await self.cache.client.xrevrange(self.key, count=1)
            if last and tuple(map(int, supplied.split("-"))) > tuple(map(int, last[0][0].split("-"))):
                raise ValueError("event cursor is ahead of the stream")
            return supplied
        last = await self.cache.client.xrevrange(self.key, count=1)
        return last[0][0] if last else "0-0"

    async def read(self, cursor: str) -> list[tuple[str, str]]:
        """
        Read a bounded batch without blocking the scheduler's event loop.

        Args:
            cursor (str): Last observation delivered to this subscriber.

        Returns:
            list[tuple[str, str]]: Stream IDs and serialized JSON observations.
        """
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,19})-(?:0|[1-9][0-9]{0,19})", cursor):
            raise ValueError("invalid event cursor")
        try:
            entries = await cast("Awaitable[Any]", self.cache.client.eval(READ, 1, self.key, cursor))
        except ResponseError as error:
            if "CURSOR_EXPIRED" in str(error):
                raise CursorExpired("event cursor expired") from error
            raise
        if not entries:
            await asyncio.sleep(1)
        return [(identity, fields[1]) for identity, fields in entries]

    async def close(self) -> None:
        """
        Close the connection pool after publishers and subscribers stop.

        Returns:
            None: No return value.
        """
        await self.cache.close()
