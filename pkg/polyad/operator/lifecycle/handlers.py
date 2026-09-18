"""
Observe with Kopf; coordinate all mutations through leased, refreshed queues.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING

import kopf
from attrs import asdict
from cattrs.errors import CattrsError
from kubernetes.client.exceptions import ApiException

from polyad.cache import cache_url
from polyad.compiler.registry import DEFINITION_KINDS, GRAPH_OWNED_KINDS, RECONCILED_KINDS, RESOURCE_TYPES
from polyad.events.visibility import observation_ancestry, public_observation
from polyad.metrics.inventory import inventory
from polyad.operator.adapters.kubernetes import API, GROUP, VERSION
from polyad.operator.coordination.leases import SHARDS, Coordinator, NotOwner, active_shard
from polyad.operator.coordination.pulses import PulseDeferred
from polyad.operator.coordination.queue import RefreshQueue, batches
from polyad.operator.coordination.settings import WorkGraphSettings
from polyad.operator.coordination.shared_queue import SharedQueue
from polyad.operator.coordination.validation import invalidate
from polyad.operator.lifecycle.health import credential_token, lifecycle, watch_credentials
from polyad.operator.lifecycle.roles import executes, role, serves
from polyad.operator.lifecycle.tuning import OperatorTuning
from polyad.operator.observability.pressure import collect, report
from polyad.transport.pools import snapshot as connection_snapshot
from polyad_types.resources import BOUNDARY_KINDS

if TYPE_CHECKING:
    from typing import Any

    from polyad.api.http.server import APIServer
    from polyad.events.store import EventStore
    from polyad.metrics.store import MetricsStore
    from polyad.operator.adapters.interfaces import StateBackend
    from polyad.operator.clusters.root import RootControlPlane
    from polyad.operator.coordination.queue import Key
    from polyad.operator.reconciliation.controller import Controller

queue: RefreshQueue | None = None
controller: Controller | None = None
coordinator: Coordinator | None = None
shared: SharedQueue | None = None
http: APIServer | None = None
events: EventStore | None = None
metrics_store: MetricsStore | None = None
inventory_sample: tuple[float, dict[str, Any]] | None = None
inventory_sample_ok = False
last_api_success = 0.0
initialized = False
tuning = OperatorTuning()
work_graph = WorkGraphSettings()
background: list[asyncio.Task[None]] = []
root_plane: RootControlPlane | None = None
state: StateBackend | None = None
KINDS = tuple(sorted(RECONCILED_KINDS))
logger = logging.getLogger(__name__)


@kopf.on.startup()
async def startup(settings: kopf.OperatorSettings, **_: Any) -> None:
    """
    Watch without Kopf progress/finalizer mutations that bypass shard ownership.

    Args:
        settings (kopf.OperatorSettings): Kopf settings configured before watches and workers start.
        **_ (Any): Additional Kopf callback arguments.

    Returns:
        None: No return value.
    """
    global queue, controller, coordinator, shared, initialized, http, events, tuning, root_plane
    global state, metrics_store, work_graph
    if initialized or http is not None:
        raise RuntimeError("operator HTTP lifecycle is already initialized")
    from polyad.events.access import configuration

    configuration()  # Invalid parent chains prevent intake and execution startup.
    tuning = OperatorTuning.from_environment()
    work_graph = WorkGraphSettings.from_environment()
    settings.posting.enabled = False
    settings.scanning.disabled = True
    settings.networking.request_timeout = 30
    if os.environ.get("POLYAD_OPERATOR_MESH_ENABLED", "false").lower() == "true":
        injection = json.loads(os.environ.get("POLYAD_MESH_INJECTION_STATUS", "{}") or "{}")
        if "istio-proxy" not in injection.get("initContainers", []):
            raise RuntimeError("operator mesh authorization requires injected Istio native sidecars")
    if os.environ.get("POLYAD_CACHE_URL_FILE"):
        os.environ["POLYAD_CACHE_URL"] = credential_token("CACHE", setting="URL")
    namespace = os.environ.get("POLYAD_NAMESPACE", "default")
    coordinator = Coordinator(
        API(), namespace, planner=role() in {"dense", "bootstrap"} and os.environ.get("POLYAD_ROOT_WORKER", "false").lower() != "true"
    )
    if os.environ.get("POLYAD_POSTGRES_ENABLED", "false").lower() == "true":
        from polyad.operator.adapters.postgresql import StateStore

        state = StateStore.from_environment(namespace)
    if executes() or os.environ.get("POLYAD_ROOT_ENABLED", "false").lower() == "true":
        from polyad.operator.reconciliation.controller import Controller

        controller = Controller(API(before_write=coordinator.guard))
        controller.api.on_write_drift = publish
    shared = SharedQueue(cache_url(), namespace, coordinator.identity)
    if (pool := os.environ.get("POLYAD_DRAGONFLY_POOL")) and coordinator.planner:
        from polyad.operator.coordination.dragonfly import run as dragonfly_loop

        background.append(asyncio.create_task(dragonfly_loop(coordinator, shared, pool)))
    if os.environ.get("POLYAD_ROOT_ENABLED", "false").lower() == "true":
        from polyad.operator.clusters.root import RootControlPlane

        assert controller is not None
        root_plane = RootControlPlane(coordinator, controller, shared, state=state)
        background.extend(root_plane.start(tuning))
    queue = RefreshQueue(reconcile)
    queue.start()
    if any(serves(feature) for feature in ("API", "CONNECTIONS", "EVENTS", "METRICS")):
        from polyad.api.http.server import APIServer

        http = APIServer(API())
    if serves("CONNECTIONS"):
        from polyad.api.connections.store import ConnectionSettings

        assert http is not None
        http.connections(ConnectionSettings.from_environment(namespace))
        background.append(asyncio.create_task(connection_sweep_loop()))
    if serves("API"):
        assert http is not None
        http.composition(namespace, credential_token("API"))
    publish_events = os.environ.get("POLYAD_EVENT_PUBLICATION_ENABLED", os.environ.get("POLYAD_EVENTS_ENABLED", "false")).lower() == "true"
    if (executes() and publish_events) or serves("EVENTS"):
        from polyad.events.store import EventStore

        event_api = controller.api if controller else coordinator.api
        reserved = (namespace, coordinator.self_graph) if coordinator.self_graph else None
        events = EventStore(
            cache_url(),
            namespace,
            visible=lambda obj: public_observation(event_api, obj, reserved_graph=reserved),
            ancestry=lambda obj: observation_ancestry(event_api, obj),
            archive=state.record_event if state else None,
            retention=int(os.environ.get("POLYAD_EVENTS_RETENTION", "10000")),
        )
    if serves("EVENTS"):
        assert events is not None and http is not None
        http.events(
            events,
            namespace,
            credential_token("EVENTS"),
            connections=int(os.environ.get("POLYAD_EVENTS_CONNECTIONS", "16")),
            websockets=os.environ.get("POLYAD_EVENTS_WEBSOCKETS_ENABLED", "false").lower() == "true",
            clusters={name: worker.events for name, worker in root_plane.workers.items()} if root_plane else None,
        )
    if serves("METRICS"):
        from polyad.metrics.store import MetricsStore

        metrics_store = MetricsStore()
        token = credential_token("METRICS") if os.environ.get("POLYAD_METRICS_AUTH_ENABLED", "false").lower() == "true" else None
        assert http is not None
        http.metrics(metrics_store, token=token)
        background.append(asyncio.create_task(metrics_loop()))
    if http is not None:
        http.start()
    background.append(asyncio.create_task(watch_credentials()))
    logger.debug(
        "Operator workers started namespace=%s replica=%s metrics=%s events=%s composition=%s",
        namespace,
        coordinator.identity,
        serves("METRICS"),
        serves("EVENTS"),
        serves("API"),
    )
    initialized = True
    background.extend([asyncio.create_task(coordination_loop()), asyncio.create_task(backlog_loop())])
    background.append(asyncio.create_task(component_loop()))
    if role() in {"dense", "bootstrap", "telemetry"}:
        background.append(asyncio.create_task(rescan_loop()))
    if executes():
        background.append(asyncio.create_task(consume_loop()))


async def coordination_loop() -> None:
    """
    Renew ownership independently of slow reconciliation, failing closed on expiry.

    Returns:
        None: No return value.
    """
    assert coordinator is not None and shared is not None
    while True:
        try:
            await shared.ping()  # A replica unable to consume must stop advertising availability.
            if executes():
                await coordinator.tick()
            else:
                await coordinator.api.request("GET", "Graph", coordinator.namespace)
                listing = await coordinator.api.request(
                    "GET", "Lease", coordinator.namespace, query=[("labelSelector", f"{GROUP}/coordination=true")]
                )
                coordinator.members = [
                    lease["spec"]["holderIdentity"]
                    for lease in listing.get("items", [])
                    if lease["metadata"]["name"].startswith("polyad-member-") and not coordinator.expired(lease)
                ]
                coordinator.last_success = time.monotonic()
        except Exception:
            coordinator.leader = False
            logger.exception("Coordination failed; expired ownership will stop writes")
        await asyncio.sleep(5)


async def component_loop() -> None:
    """
    Publish worker and HTTP demand independently of which role serves metrics.

    Returns:
        None: Failed heartbeats expire and prevent treating unavailable demand as zero.
    """
    assert shared is not None and coordinator is not None and queue is not None
    while True:
        try:
            await report(shared, role())
            if root_plane and not serves("METRICS"):
                await root_plane.telemetry(
                    {
                        "replica": coordinator.identity,
                        "shards": sorted(coordinator.owned),
                        "pending": queue.queue.qsize(),
                        "workGraph": work_graph.document(),
                        "writes": write_backlog(),
                    }
                )
        except Exception:
            logger.warning("Component demand publication failed")
        await asyncio.sleep(tuning.metrics)


async def connection_sweep_loop() -> None:
    """
    Rediscover expiring receipts across replica restarts and missed watch events.

    Returns:
        None: Receipt reconciliation is published through the existing shared graph-family queue.
    """
    assert coordinator is not None
    while True:
        try:
            listing = await coordinator.api.request("GET", "TemporaryConnection", coordinator.namespace)
            for receipt in listing.get("items", []):
                await publish(("TemporaryConnection", coordinator.namespace, receipt["metadata"]["name"]))
        except Exception:
            logger.exception("Temporary connection sweep failed; durable receipts will be retried")
        await asyncio.sleep(5)


async def backlog_loop() -> None:
    """
    Sample shared backlog independently so slow API writes cannot freeze telemetry.

    Returns:
        None: No return value.
    """
    assert shared is not None
    while True:
        try:
            await shared.sample_backlog(range(SHARDS))
        except Exception:
            logger.warning("Backlog sampling failed; health metrics retain their stale sample")
        await asyncio.sleep(tuning.backlog)


async def rescan_loop() -> None:
    """
    Rediscover duties after handoff and retry from API state without watch dependence.

    Returns:
        None: No return value.
    """
    global last_api_success, inventory_sample, inventory_sample_ok
    assert coordinator is not None and queue is not None
    while True:
        try:
            scan_started = time.monotonic()
            ticket = None
            if state:
                try:
                    ticket = await state.begin()
                except Exception:
                    logger.warning("PostgreSQL unavailable; continuing live observation without persistence")
            objects = []
            definitions: tuple[str, ...] = (
                ("Workload", "Daemon", "Resource", "Gate", "ShutdownPolicy", "GraphRule") if serves("METRICS") or state else ()
            )
            if root_plane and (serves("METRICS") or state):
                definitions += ("OperatorPool", "RemoteScale")
            for kind in (*KINDS, *definitions):
                result = await coordinator.api.request("GET", kind, coordinator.namespace)
                last_api_success = time.monotonic()
                for obj in result.get("items", []):
                    obj.setdefault("kind", kind)
                    objects.append(obj)
                if kind in KINDS:
                    for obj in result.get("items", []):
                        await publish((kind, coordinator.namespace, obj["metadata"]["name"]))
            tracked = inventory(objects)
            if state and ticket is not None:
                try:
                    await state.save(
                        os.environ.get("POLYAD_CLUSTER_NAME") or "local", coordinator.namespace, ticket, objects, {"inventory": tracked}
                    )
                except Exception:
                    logger.warning("PostgreSQL state commit failed; retaining the previous durable observation")
            inventory_sample = (scan_started, tracked)
            inventory_sample_ok = True
        except Exception:
            inventory_sample_ok = False
            logger.exception("Resource rescan failed; retrying from fresh state")
        await asyncio.sleep(tuning.rescan)


async def metrics_loop() -> None:
    """
    Publish cached observations without adding API requests to HTTP scrape paths.

    Returns:
        None: No return value.
    """
    assert coordinator is not None and shared is not None and queue is not None and metrics_store is not None
    while True:
        age = time.monotonic() - inventory_sample[0] if inventory_sample else None
        tracked = dict(inventory_sample[1]) if inventory_sample else {"total": None, "byKind": [], "objects": []}
        tracked.update(sampleAgeSeconds=age, fresh=inventory_sample_ok and age is not None and age < 30)
        snapshot = {
            "namespace": coordinator.namespace,
            "replica": coordinator.identity,
            "leader": coordinator.leader,
            "shards": sorted(coordinator.owned),
            "pending": queue.queue.qsize(),
            "tuning": asdict(tuning),
            "connectionPools": connection_snapshot(),
            "workGraph": work_graph.document(),
            "writes": write_backlog(),
            "inbound": shared.backlog(tuple(coordinator.owned)),
            "shardBacklogs": shared.backlog_sample[1] if shared.backlog_sample else {},
            "inventory": tracked,
        }
        if root_plane:
            snapshot["workers"] = await root_plane.telemetry(snapshot)
            snapshot["clusters"] = await root_plane.observations()
        if state:
            snapshot["postgresql"] = await state.connections()
        if os.environ.get("POLYAD_DRAGONFLY_POOL"):
            from polyad.operator.coordination.dragonfly import connections as dragonfly_connections

            snapshot["dragonfly"] = await dragonfly_connections(shared)
        snapshot["components"] = await collect(shared)
        await asyncio.to_thread(
            metrics_store.publish,
            snapshot,
            graph_labels=os.environ.get("POLYAD_METRICS_GRAPH_LABELS", "false").lower() == "true",
        )
        await asyncio.sleep(tuning.metrics)


async def reconcile(key: Key) -> None:
    """
    Hold graph-family ownership through reads, mutations, and validation status.

    Args:
        key (Key): Resource kind, namespace and name to reconcile from fresh API state.

    Returns:
        None: No return value.
    """
    from polyad.operator.reconciliation.controller import Pending

    global last_api_success
    assert controller is not None and coordinator is not None
    if work_graph.reconciliation_cooldown:
        assert shared is not None
        await shared.pulse(key, cluster=os.environ.get("POLYAD_CLUSTER_NAME", ""))
    async with coordinator.duty(key):
        try:
            await controller.reconcile(key)
        except Pending:
            await publish_observation(key)
            raise
        except (ValueError, TypeError, KeyError, CattrsError, ApiException) as error:
            if isinstance(error, ApiException) and error.status not in {400, 422}:
                raise
            obj = await controller.api.get(*key)
            if obj is not None:
                await controller.status(
                    obj,
                    {
                        "phase": "Invalid",
                        "message": str(error),
                        "ready": False,
                        "completed": False,
                        "observedGeneration": obj["metadata"].get("generation", 1),
                    },
                )
                await controller.report_metrics(key)
        last_api_success = time.monotonic()
        await publish_observation(key)


async def publish_observation(key: Key) -> None:
    """
    Publish lifecycle and changed topology snapshots after refreshing owned executions.

    Args:
        key (Key): Reconciled resource identity.

    Returns:
        None: Observations are persisted only while the shard is still owned.
    """
    assert controller is not None and coordinator is not None
    if events is None or key[0] not in KINDS:
        return
    from polyad.events.topology import topology_snapshot

    obj = await controller.api.get(*key)
    if obj is not None:
        snapshot = await topology_snapshot(controller.api, obj, await controller.children(obj)) if key[0] in BOUNDARY_KINDS else None
        await coordinator.guard()
        await events.publish(obj, topology=snapshot)
        if key[0] == "TemporaryConnection":
            for side, peer in obj["spec"].get("peers", {}).items():
                stream, reader = events, controller.api
                if peer["cluster"] and peer["cluster"] != controller.federation.name:
                    if root_plane is None or peer["cluster"] not in root_plane.workers:
                        raise RuntimeError("participant event stream is unavailable")
                    worker = root_plane.workers[peer["cluster"]]
                    stream, reader = worker.events, worker.controller.api
                participant = await reader.get(peer["kind"], peer["namespace"], peer["graph"])
                if participant is not None:
                    await stream.publish_connection(obj, participant, side)
            target = await controller.api.get(obj["spec"]["kind"], key[1], obj["spec"]["graph"])
            if target is not None and target["metadata"]["uid"] == obj["spec"]["graphUid"]:
                await publish_observation((target["kind"], key[1], target["metadata"]["name"]))


async def publish(key: Key) -> None:
    """
    Publish resource hints to shared storage rather than replica-local watch queues.

    Args:
        key (Key): Resource kind, namespace and name to reconcile from fresh API state.

    Returns:
        None: No return value.
    """
    assert coordinator is not None and shared is not None
    await shared.publish(await coordinator.shard_for(key), key)


async def consume_loop() -> None:
    """
    Consume leased shards in bounded batches, acknowledging only after a refreshed attempt.

    Returns:
        None: No return value.
    """
    from polyad.operator.reconciliation.controller import Pending

    assert coordinator is not None and shared is not None and queue is not None

    async def deliver(shard: int) -> None:
        """
        Keep one shard delivery pending until its refreshed reconciliation acknowledges.

        Args:
            shard (int): Owned source shard.

        Returns:
            None: Failure retains the delivery for retry under its current owner.
        """
        assert coordinator is not None and shared is not None and queue is not None
        if lifecycle.replacement.is_set() or lifecycle.draining.is_set():
            return
        token = active_shard.set(shard)
        try:
            await coordinator.guard()
            message = await shared.take(shard)
            if message is None:
                return
            message_id, key = message
            if await coordinator.shard_for(key) != shard:
                await publish(key)  # Ownership may have moved since the hint was queued.
            else:
                try:
                    await queue.submit(key)
                except Pending:
                    pass  # Rescan will retry the intent after refreshed observations.
            await coordinator.guard()
            await shared.acknowledge(shard, message_id)
        except PulseDeferred as error:
            logger.info("Reconciliation pulse deferred for shard %s; retry after %s seconds", shard, error.retry_after)
        except NotOwner:
            pass  # Keep pending messages for the next lease holder.
        except Exception:
            logger.exception("Shard %s delivery failed; keeping it pending", shard)
        finally:
            active_shard.reset(token)

    while True:
        try:
            await shared.ping()
            await batches(sorted(coordinator.owned), deliver, queue.workers)
        except Exception:
            logger.exception("Dragonfly unavailable; queue consumption paused")
        await asyncio.sleep(tuning.consume)


async def handle(namespace: str | None, name: str, body: kopf.Body, **_: Any) -> None:
    """
    Raw observation handlers never ask Kopf to patch resource bookkeeping.

    Args:
        namespace (str | None): Namespace containing the operator resources.
        name (str): Resource name within its namespace.
        body (kopf.Body): Resource observed by the Kopf watch callback.
        **_ (Any): Additional Kopf callback arguments.

    Returns:
        None: No return value.
    """
    assert namespace is not None
    if controller is not None:
        invalidate(controller.api, (body["kind"], namespace, name))
    if role() not in {"dense", "bootstrap"}:
        return
    if body["kind"] in KINDS:
        await publish((body["kind"], namespace, name))
    for owner in body.get("metadata", {}).get("ownerReferences", []):
        if owner.get("controller") and owner.get("apiVersion") == f"{GROUP}/{VERSION}" and owner.get("kind") in KINDS:
            await publish((owner["kind"], namespace, owner["name"]))


for watched_kind in sorted(set(KINDS) | GRAPH_OWNED_KINDS | DEFINITION_KINDS):
    descriptor = RESOURCE_TYPES[watched_kind]
    if descriptor.required_feature and os.environ.get(f"POLYAD_{descriptor.required_feature.upper()}_ENABLED", "false").lower() != "true":
        continue
    kopf.on.event(descriptor.api_group, descriptor.api_version.rsplit("/", 1)[-1], descriptor.plural)(handle)


def write_backlog() -> dict[str, Any]:
    """
    Report local concrete API pressure, including separate coordination traffic.

    Returns:
        dict[str, Any]: Replica write gauges, including workload and coordination subtotals.
    """
    assert coordinator is not None
    workloads: dict[str, Any] = controller.api.writes.snapshot() if controller else {"queued": 0, "inFlight": 0, "total": 0}
    if root_plane:
        for _, api in root_plane.federation.clients.values():
            pressure = api.writes.snapshot()
            for key in ("queued", "inFlight", "total"):
                workloads[key] += pressure[key]
            for key in ("oldestQueuedSeconds", "oldestInFlightSeconds"):
                workloads[key] = max(workloads.get(key, 0), pressure.get(key, 0))
    coordination = coordinator.api.writes.snapshot()
    intake = http.api.writes.snapshot() if http else {"queued": 0, "inFlight": 0, "total": 0}
    return {
        "scope": "replica",
        **{key: workloads[key] + coordination[key] + intake[key] for key in ("queued", "inFlight", "total")},
        "workloads": workloads,
        "coordination": coordination,
        "apiIntake": intake,
    }


@kopf.on.probe(id="scheduler")
def health(**_: Any) -> dict[str, Any]:
    """
    Check workers and renewal loops; an idle follower remains healthy.

    Args:
        **_ (Any): Additional Kopf callback arguments.

    Returns:
        dict[str, Any]: Worker health, coordination state and cached backlog gauges.
    """
    if lifecycle.replacement.is_set():
        raise RuntimeError("replica replacement required: credential change or restart signal")
    if lifecycle.draining.is_set():
        raise RuntimeError("replica is draining for replacement")
    if not initialized or queue is None or queue.task is None or queue.task.done() or any(task.done() for task in background):
        raise RuntimeError("operator worker is unavailable")
    if http is not None and not http.thread.is_alive():
        raise RuntimeError("shared API thread is unavailable")
    return {
        "metricsEnabled": serves("METRICS"),
        "eventsEnabled": serves("EVENTS"),
        "connectionsEnabled": serves("CONNECTIONS"),
        "initialized": initialized,
        "worker": True,
        "pending": queue.queue.qsize(),
        "workGraph": work_graph.document(),
        "backlog": {
            "inboundUpdates": shared.backlog(tuple(coordinator.owned)) if shared and coordinator else None,
            "kubernetesWrites": write_backlog(),
        },
        "apiFresh": coordinator is not None and time.monotonic() - coordinator.last_success < 60,
        "cacheFresh": shared is not None and time.monotonic() - shared.last_success < 30,
        "attached": coordinator is not None and coordinator.attachment_ready,
        "identity": coordinator.identity if coordinator else None,
        "leader": coordinator.leader if coordinator else False,
        "shards": sorted(coordinator.owned) if coordinator else [],
    }


@kopf.on.cleanup()
async def cleanup(**_: Any) -> None:
    """
    Join outstanding writes before stopping renewals; let leases expire on shutdown.

    Args:
        **_ (Any): Additional Kopf callback arguments.

    Returns:
        None: No return value.
    """
    global initialized, http
    logger.debug("Operator cleanup started; joining listeners, queued work and API transports")
    initialized = False
    if http:
        await http.close()
        http = None
    if queue:
        await queue.stop()
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    background.clear()
    if root_plane:
        await root_plane.close()
    if controller and "federation" in controller.__dict__:
        controller.federation.close()
    if events:
        await events.close()
    if shared:
        await shared.close()
    if state:
        await state.close()
