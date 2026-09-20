"""
Publish namespace observations to a bounded Redis stream shared by operator replicas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

from redis.exceptions import ResponseError

from polyad.cache import Cache
from polyad.events.settings import settings_from_environment
from polyad.events.topology import neighbors
from polyad.lua import script
from polyad_types.events.envelope import Event, EventTooLarge
from polyad_types.resources import GROUP

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from polyad_types.events.envelope import EventStreamSettings

PUBLISH = script("events/publish.lua")


READ = script("events/read.lua")

PUBLISH_TOPOLOGY = script("events/publish-topology.lua")

SNAPSHOT = script("events/snapshot.lua")
logger = logging.getLogger(__name__)


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
        retention: int | None = None,
        cluster: str | None = None,
        ancestry: Callable[[dict[str, Any]], Awaitable[list[dict[str, str]]]] | None = None,
        archive: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        settings: EventStreamSettings | None = None,
    ) -> None:
        """
        Configure the namespace stream and maximum retained event count.

        Args:
            url (str): Shared Redis or Dragonfly URL.
            namespace (str): Namespace visible to subscribers.
            visible (Callable[[dict[str, Any]], Awaitable[bool]]): Required graph-family visibility check before publication.
            retention (int | None): Maximum retained observations; omitted uses the chart/environment setting.
            cluster (str | None): Remote stream identity when reports are held at the root.
            ancestry (Callable[[dict[str, Any]], Awaitable[list[dict[str, str]]]] | None): Verified graph ownership reader.
            archive (Callable[[dict[str, Any]], Awaitable[None]] | None): Optional durable archive of approved event payloads.
            settings (EventStreamSettings | None): Event budgets; omitted uses the chart/environment settings.
        """
        self.settings = settings if settings is not None else settings_from_environment()
        retention = int(os.environ.get("POLYAD_EVENTS_RETENTION", "10000")) if retention is None else retention
        if type(retention) is not int or not 100 <= retention <= 100000:
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

    def _serialize(self, payload: dict[str, Any]) -> str:
        kind = payload["type"] if payload["type"] in {"topology", "connection"} else "graph"

        # Redis assigns the cursor after admission. Reserve its maximum supported width.
        event = Event("9" * 20 + "-" + "9" * 20, kind, payload)
        event.typed()
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        try:
            event.encode("sse", self.settings.maxEventBytes, raw=encoded)
            event.encode("websocket", self.settings.maxEventBytes)
        except EventTooLarge:
            logger.warning(
                "Rejected oversized %s event for %s/%s; maximum serialized event size is %d bytes",
                kind,
                payload.get("kind", "graph"),
                payload.get("name", payload.get("graph", {}).get("name", "unknown")),
                self.settings.maxEventBytes,
            )
            raise
        return encoded

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
        if obj["kind"] == "TemporaryConnection":
            from polyad.api.connections.store import ConnectionStore

            payload["type"] = "connection"
            payload["connection"] = ConnectionStore.receipt(obj)
            payload["graph"] = {
                **({"cluster": self.cluster} if self.cluster else {}),
                "kind": obj["spec"]["kind"],
                "namespace": meta["namespace"],
                "name": obj["spec"]["graph"],
                "uid": obj["spec"]["graphUid"],
            }
        encoded = self._serialize(payload)
        if self.archive is not None:
            await self.archive(payload)
        await cast(
            "Awaitable[Any]",
            self.cache.client.eval(
                PUBLISH, 2, self.key, self.key + ":versions", meta["uid"], meta["resourceVersion"], encoded, str(self.retention)
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
            encoded = self._serialize(event)
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
                    encoded,
                    str(self.retention),
                ),
            )

    async def publish_connection(self, receipt: dict[str, Any], graph: dict[str, Any], participant: str) -> None:
        """
        Deliver a consent proposal within the exact participant's authorized graph stream.

        Args:
            receipt (dict[str, Any]): Durable common-boundary receipt.
            graph (dict[str, Any]): Fresh UID-matching participant graph in this stream's cluster.
            participant (str): Source or target side represented by this projection.

        Returns:
            None: Internal graphs are excluded and replay deduplicates each receipt revision per side.
        """
        from polyad.api.connections.store import ConnectionStore

        peer = receipt["spec"]["peers"][participant]
        meta = graph["metadata"]
        if meta["uid"] != peer["graphUid"] or not await self.visible(graph):
            return
        ancestry = await self.ancestry(graph) if self.ancestry else []
        payload = {
            "type": "connection",
            "participant": participant,
            "graph": {"cluster": peer["cluster"], "kind": graph["kind"], **{key: meta[key] for key in ("namespace", "name", "uid")}},
            "ancestry": ancestry,
            "connection": ConnectionStore.receipt(receipt),
        }
        encoded = self._serialize(payload)
        if self.archive is not None:
            await self.archive(payload)
        await cast(
            "Awaitable[Any]",
            self.cache.client.eval(
                PUBLISH,
                2,
                self.key,
                self.key + ":versions",
                receipt["metadata"]["uid"] + ":" + participant,
                receipt["metadata"]["resourceVersion"],
                encoded,
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
            entries = await cast("Awaitable[Any]", self.cache.client.eval(READ, 1, self.key, cursor, str(self.settings.readBatchSize)))
        except ResponseError as error:
            if "CURSOR_EXPIRED" in str(error):
                raise CursorExpired("event cursor expired") from error
            raise
        if not entries:
            await asyncio.sleep(self.settings.pollIntervalSeconds)
        return [(identity, fields[1]) for identity, fields in entries]

    async def close(self) -> None:
        """
        Close the connection pool after publishers and subscribers stop.

        Returns:
            None: No return value.
        """
        await self.cache.close()
