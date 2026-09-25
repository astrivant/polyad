"""
Extend one root scheduler across registered clusters using centrally leased workers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from cattrs.errors import CattrsError
from kubernetes.client.exceptions import ApiException

from polyad.cache import cache_url
from polyad.compiler.registry import RECONCILED_KINDS
from polyad.events.store import EventStore
from polyad.events.topology import topology_snapshot
from polyad.events.visibility import observation_ancestry, public_observation
from polyad.exceptions.coordination import NotOwner, PulseDeferred
from polyad.exceptions.reconciliation import Pending
from polyad.metrics.inventory import inventory
from polyad.operator.clusters.federation import Federation
from polyad.operator.clusters.pools import PoolManager
from polyad.operator.coordination.leases import SHARDS, active_shard
from polyad.operator.coordination.queue import batches, reconciliation_workers
from polyad.operator.coordination.shared_queue import SharedQueue
from polyad.operator.coordination.validation import invalidate
from polyad.operator.lifecycle.health import lifecycle
from polyad.operator.lifecycle.roles import executes, role
from polyad.operator.reconciliation.controller import Controller
from polyad_types.resources import BOUNDARY_KINDS

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.interfaces import StateBackend
    from polyad.operator.adapters.kubernetes import API
    from polyad.operator.coordination.leases import Coordinator
    from polyad.operator.coordination.queue import Key
    from polyad.operator.lifecycle.tuning import OperatorTuning

__all__ = (
    "ClusterWorker",
    "DEFINITIONS",
    "RootControlPlane",
)


logger = logging.getLogger(__name__)
DEFINITIONS = ("Workload", "Daemon", "Resource", "Gate", "ShutdownPolicy", "GraphPolicy")


class ClusterWorker:
    """
    Execute remote graph duties under root leases and publish observations to root storage.
    """

    def __init__(self, root: RootControlPlane, cluster: str, namespace: str) -> None:
        """
        Isolate graph identities and streams by registered cluster.

        Args:
            root (RootControlPlane): Shared root coordination and transport registry.
            cluster (str): Registered cluster name.
            namespace (str): Registered workload namespace.
        """
        self.root, self.cluster, self.namespace = root, cluster, namespace
        prefix = f"{root.coordinator.namespace}:clusters:{cluster}:{namespace}"
        self.shared = SharedQueue(cache_url(), prefix, root.coordinator.identity)
        self.controller = Controller(root.federation.target(cluster)[0])
        self.controller.api.on_write_drift = self.publish_refresh
        self.events = EventStore(
            cache_url(),
            prefix,
            cluster=cluster,
            visible=lambda obj: public_observation(self.controller.api, obj),
            ancestry=lambda obj: observation_ancestry(self.controller.api, obj, cluster=cluster, resolve=root.resolve),
            archive=root.state.record_event if root.state else None,
        )
        federation = Federation(self.controller.api)
        federation.name = cluster
        federation.resolver = root.resolve
        self.controller.federation = federation
        self.sample: dict[str, Any] = {"namespace": namespace, "inventory": {"fresh": False}}

    async def publish_refresh(self, key: Key) -> None:
        """
        Route drift recovery through this cluster's root-coordinated work stream.

        Args:
            key (Key): Affected graph resource or owner in the remote namespace.

        Returns:
            None: Publishes a coalesced hint without recursively acquiring another duty.
        """
        await self.shared.publish(await self.shard_for(key), key)

    async def scan(self) -> None:
        """
        Refresh one complete remote inventory and enqueue graph duties centrally.

        Returns:
            None: Failed or incomplete scans remain unavailable to autoscaling.
        """
        started = time.monotonic()
        ticket = None
        if self.root.state:
            try:
                ticket = await self.root.state.begin()
            except Exception:
                logger.warning("PostgreSQL unavailable; continuing remote observation without persistence")
        objects = []
        self.sample["inventory"]["fresh"] = False
        self.controller.api = self.root.federation.target(self.cluster)[0]
        self.controller.api.on_write_drift = self.publish_refresh
        for kind in (*sorted(RECONCILED_KINDS), *DEFINITIONS):
            listing = await self.controller.api.request("GET", kind, self.namespace)
            if listing is None:
                raise ValueError(f"remote {kind} API is unavailable; install the pool before executing workloads")
            for obj in (listing or {}).get("items", []):
                invalidate(self.controller.api, (kind, self.namespace, obj["metadata"]["name"]))
                obj.setdefault("kind", kind)
                objects.append(obj)
                if kind in RECONCILED_KINDS:
                    key = kind, self.namespace, obj["metadata"]["name"]
                    await self.shared.publish(await self.shard_for(key), key)
        await self.shared.sample_backlog(range(SHARDS))
        age = time.monotonic() - started
        self.sample = {
            "namespace": self.namespace,
            "inventory": {**inventory(objects, cluster=self.cluster), "fresh": age < 30, "sampleAgeSeconds": age},
            "inbound": self.shared.backlog(range(SHARDS)),
            "shardBacklogs": self.shared.backlog_sample[1] if self.shared.backlog_sample else {},
        }
        if self.root.state and ticket is not None:
            try:
                await self.root.state.save(self.cluster, self.namespace, ticket, objects, self.sample)
            except Exception:
                logger.warning("Remote state commit failed; retaining previous durable observation")

        # Redis TTL measures transit freshness without trusting a remote worker's wall clock.
        await self.root.shared.client.set(self.root.sample_key(self.cluster), json.dumps(self.sample), ex=max(1, min(15, int(30 - age))))

    async def shard_for(self, key: Key) -> int:
        """
        Resolve a cluster-qualified family to the root's fixed shard set.

        Args:
            key (Key): Remote graph resource identity.

        Returns:
            int: Shard whose lease resides exclusively in the root cluster.
        """
        return await self.root.coordinator.shard_for(key, api=self.controller.api, cluster=self.cluster)

    async def consume(self) -> None:
        """
        Acknowledge refreshed attempts only while their root lease remains valid.

        Returns:
            None: Unacknowledged attempts remain recoverable after worker loss.
        """

        async def deliver(shard: int) -> None:
            """
            Reconcile one remote delivery while retaining root graph-family ownership.

            Args:
                shard (int): Root-owned delivery shard.

            Returns:
                None: Failed deliveries remain pending for refreshed retry.
            """
            if lifecycle.draining.is_set() or lifecycle.replacement.is_set():
                return
            token = active_shard.set(shard)
            try:
                await self.root.coordinator.guard()
                message = await self.shared.take(shard)
                if message is None:
                    return
                message_id, key = message
                routed = await self.shard_for(key)
                if routed != shard:
                    await self.shared.publish(routed, key)
                else:
                    from polyad.operator.coordination.settings import WorkGraphSettings

                    if WorkGraphSettings.from_environment().reconciliation_cooldown:
                        await self.shared.pulse(key, cluster=self.cluster)
                    async with self.root.coordinator.duty(key, api=self.controller.api, cluster=self.cluster):
                        try:
                            await self.controller.reconcile(key)
                        except Pending:
                            pass
                        except (ValueError, TypeError, KeyError, CattrsError, ApiException) as error:
                            if isinstance(error, ApiException) and error.status not in {400, 422}:
                                raise
                            obj = await self.controller.api.get(*key)
                            if obj:
                                await self.controller.status(obj, {"phase": "Invalid", "message": str(error), "ready": False})
                        obj = await self.controller.api.get(*key)
                        if obj:
                            snapshot = (
                                await topology_snapshot(self.controller.api, obj, await self.controller.children(obj))
                                if key[0] in BOUNDARY_KINDS
                                else None
                            )
                            await self.root.coordinator.guard()
                            await self.events.publish(obj, topology=snapshot)
                await self.root.coordinator.guard()
                await self.shared.acknowledge(shard, message_id)
            except PulseDeferred as error:
                logger.info(
                    "Remote reconciliation pulse deferred cluster=%s shard=%s retryAfter=%s", self.cluster, shard, error.retry_after
                )
            except NotOwner:
                pass
            except Exception:
                logger.exception("Remote shard %s failed in cluster %s; keeping its delivery pending", shard, self.cluster)
            finally:
                active_shard.reset(token)

        await batches(sorted(self.root.coordinator.owned), deliver, reconciliation_workers())

    async def run(self, operation: str, interval: float) -> None:
        """
        Retry cluster-specific failures without stopping other clusters or heartbeats.

        Args:
            operation (str): Scan or consume operation.
            interval (float): Retry cadence in seconds.

        Returns:
            None: Runs until shutdown cancellation.
        """
        while True:
            try:
                await (self.scan() if operation == "scan" else self.consume())
            except Exception:
                logger.exception("Root worker %s failed for cluster %s; retaining workloads", operation, self.cluster)
            await asyncio.sleep(interval)


class RootControlPlane:
    """
    Keep authority, events, demand and worker capacity in a single management cluster.
    """

    def __init__(self, coordinator: Coordinator, controller: Controller, shared: SharedQueue, *, state: StateBackend | None = None) -> None:
        """
        Share existing leases and guarded transports with remote execution workers.

        Args:
            coordinator (Coordinator): Root planner and shard leases.
            controller (Controller): Root graph controller.
            shared (SharedQueue): Root Dragonfly transport.
            state (StateBackend | None): Optional durable store shared by root observations.
        """
        self.coordinator, self.controller, self.shared = coordinator, controller, shared
        self.state = state
        controller.api.before_write = self.guard
        self.federation = controller.federation
        self.workers = {name: ClusterWorker(self, name, entry["namespace"]) for name, entry in self.federation.clusters.items()}
        self.pools = PoolManager(self)
        self.tasks: list[asyncio.Task[None]] = []

    async def guard(self) -> None:
        """
        Require both root storage and root lease authority before each mutation.

        Returns:
            None: Unreachable root services stop dispatch without deleting existing workloads.
        """
        await self.shared.ping()
        await self.coordinator.guard()

    def resolve(self, cluster: str) -> tuple[API, str]:
        """
        Resolve nested placements through the root's complete cluster registry.

        Args:
            cluster (str): Explicit placement cluster.

        Returns:
            tuple[API, str]: Root-fenced adapter and registered namespace.
        """
        if cluster == self.federation.name:
            return self.controller.api, self.coordinator.namespace
        return self.federation.target(cluster)

    def sample_key(self, cluster: str) -> str:
        """
        Name a root-held observation without conflating equal namespace names.

        Args:
            cluster (str): Registered cluster identity.

        Returns:
            str: Shared cache address.
        """
        return f"polyad:{self.coordinator.namespace}:cluster-observation:{cluster}"

    def start(self, tuning: OperatorTuning) -> list[asyncio.Task[None]]:
        """
        Run independent readers and consumers while the root retains planner authority.

        Args:
            tuning (OperatorTuning): Existing rescan and consumption cadences.

        Returns:
            list[asyncio.Task[None]]: Tasks included in operator health and shutdown.
        """
        for worker in self.workers.values():
            if role() in {"dense", "bootstrap", "telemetry"}:
                self.tasks.append(asyncio.create_task(worker.run("scan", tuning.rescan)))
            if executes():
                self.tasks.append(asyncio.create_task(worker.run("consume", tuning.consume)))
        if executes():
            self.tasks.append(asyncio.create_task(self.pools.run()))
        return self.tasks

    async def observations(self) -> dict[str, Any]:
        """
        Collect live root-held cluster samples and explicitly expose missing observations.

        Returns:
            dict[str, Any]: Cluster-qualified snapshots; expired reports never become zero demand.
        """
        result = {}
        for cluster, worker in self.workers.items():
            try:
                value = await self.shared.client.get(self.sample_key(cluster))
            except Exception:
                value = None
            result[cluster] = json.loads(value) if value else {"namespace": worker.namespace, "inventory": {"fresh": False}}
        return result

    async def telemetry(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """
        Send each worker's pressure to root storage and collect reports with short TTLs.

        Args:
            snapshot (dict[str, Any]): This process's current queue and write observations.

        Returns:
            dict[str, Any]: Live worker reports keyed by unique process identity.
        """
        prefix = f"polyad:{self.coordinator.namespace}:worker-observation:"
        report = {key: snapshot[key] for key in ("replica", "shards", "pending", "writes")}
        report["workGraph"] = snapshot.get("workGraph", {})
        report["root"] = self.coordinator.planner
        report["fresh"] = True
        try:
            await self.shared.client.set(prefix + self.coordinator.identity, json.dumps(report), ex=15)
            members = self.coordinator.members
            values = await self.shared.client.mget([prefix + name for name in members]) if members else []
            return {name: json.loads(value) if value else {"fresh": False} for name, value in zip(members, values, strict=True)}
        except Exception:
            return {name: {"fresh": False} for name in self.coordinator.members}

    async def close(self) -> None:
        """
        Close remote streams after all root-owned worker tasks have joined.

        Returns:
            None: Remote workloads remain untouched on process exit.
        """
        for worker in self.workers.values():
            await worker.shared.close()
            await worker.events.close()
