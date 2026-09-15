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
from cattrs.errors import CattrsError
from kubernetes.client.exceptions import ApiException

from polyad.api.server import CompositionServer
from polyad.cache import cache_url
from polyad.events.server import EventServer
from polyad.events.store import EventStore
from polyad.metrics.inventory import inventory
from polyad.metrics.server import MetricsServer
from polyad.metrics.store import MetricsStore
from polyad.operator.api import API, GROUP, VERSION
from polyad.operator.controller import Controller, Pending
from polyad.operator.coordination import SHARDS, Coordinator, NotOwner, active_shard
from polyad.operator.health import credential_token, lifecycle, watch_credentials
from polyad.operator.queue import RefreshQueue
from polyad.operator.shared_queue import SharedQueue

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.queue import Key

queue: RefreshQueue | None = None
controller: Controller | None = None
coordinator: Coordinator | None = None
shared: SharedQueue | None = None
http: CompositionServer | None = None
events: EventStore | None = None
event_http: EventServer | None = None
metrics_http: MetricsServer | None = None
metrics_store = MetricsStore()
inventory_sample: tuple[float, dict[str, Any]] | None = None
inventory_sample_ok = False
last_api_success = 0.0
initialized = False
background: list[asyncio.Task[None]] = []
KINDS = ("Graph", "EphemeralGraph", "Feedback", "PolyGraph", "Rewrite", "Composition")
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
    global queue, controller, coordinator, shared, initialized, http, events, event_http, metrics_http
    settings.posting.enabled = False
    settings.scanning.disabled = True
    settings.networking.request_timeout = 30
    if os.environ.get("POLYAD_OPERATOR_MESH_ENABLED", "false").lower() == "true":
        injection = json.loads(os.environ.get("POLYAD_MESH_INJECTION_STATUS", "{}") or "{}")
        if "istio-proxy" not in injection.get("initContainers", []):
            raise RuntimeError("operator mesh authorization requires injected Istio native sidecars")
    namespace = os.environ.get("POLYAD_NAMESPACE", "default")
    coordinator = Coordinator(API(), namespace)
    controller = Controller(API(before_write=coordinator.guard))
    shared = SharedQueue(cache_url(), namespace, coordinator.identity)
    queue = RefreshQueue(reconcile)
    queue.start()
    if os.environ.get("POLYAD_API_ENABLED", "false").lower() == "true":
        http = CompositionServer(API(), namespace, credential_token("API"))
    if os.environ.get("POLYAD_EVENTS_ENABLED", "false").lower() == "true":
        events = EventStore(cache_url(), namespace, retention=int(os.environ.get("POLYAD_EVENTS_RETENTION", "10000")))
        event_http = EventServer(
            events, namespace, credential_token("EVENTS"), connections=int(os.environ.get("POLYAD_EVENTS_CONNECTIONS", "16"))
        )
    if os.environ.get("POLYAD_METRICS_ENABLED", "false").lower() == "true":
        metrics_http = MetricsServer(metrics_store)
        background.append(asyncio.create_task(metrics_loop()))
    background.append(asyncio.create_task(watch_credentials()))
    logger.debug(
        "Operator workers started namespace=%s replica=%s metrics=%s events=%s composition=%s",
        namespace,
        coordinator.identity,
        metrics_http is not None,
        event_http is not None,
        http is not None,
    )
    initialized = True
    background.extend(
        [
            asyncio.create_task(coordination_loop()),
            asyncio.create_task(rescan_loop()),
            asyncio.create_task(consume_loop()),
            asyncio.create_task(backlog_loop()),
        ]
    )


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
            await coordinator.tick()
        except Exception:
            coordinator.leader = False
            logger.exception("Coordination failed; expired ownership will stop writes")
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
        await asyncio.sleep(5)


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
            objects = []
            definitions = ("Workload", "Daemon", "Ephemeral", "Resource", "Gate", "ShutdownPolicy", "GraphRule") if metrics_http else ()
            for kind in (*KINDS, *definitions):
                result = await coordinator.api.request("GET", kind, coordinator.namespace)
                last_api_success = time.monotonic()
                if metrics_http:
                    objects.extend(result.get("items", []))
                if kind in KINDS:
                    for obj in result.get("items", []):
                        await publish((kind, coordinator.namespace, obj["metadata"]["name"]))
            if metrics_http:
                inventory_sample = (scan_started, inventory(objects))
                inventory_sample_ok = True
        except Exception:
            inventory_sample_ok = False
            logger.exception("Resource rescan failed; retrying from fresh state")
        await asyncio.sleep(5)


async def metrics_loop() -> None:
    """
    Publish cached observations without adding API requests to HTTP scrape paths.

    Returns:
        None: No return value.
    """
    assert coordinator is not None and shared is not None and queue is not None
    while True:
        age = time.monotonic() - inventory_sample[0] if inventory_sample else None
        tracked = dict(inventory_sample[1]) if inventory_sample else {"total": None, "byKind": [], "objects": []}
        tracked.update(sampleAgeSeconds=age, fresh=inventory_sample_ok and age is not None and age < 30)
        await asyncio.to_thread(
            metrics_store.publish,
            {
                "namespace": coordinator.namespace,
                "replica": coordinator.identity,
                "leader": coordinator.leader,
                "shards": sorted(coordinator.owned),
                "pending": queue.queue.qsize(),
                "writes": write_backlog(),
                "inbound": shared.backlog(tuple(coordinator.owned)),
                "shardBacklogs": shared.backlog_sample[1] if shared.backlog_sample else {},
                "inventory": tracked,
            },
            graph_labels=os.environ.get("POLYAD_METRICS_GRAPH_LABELS", "false").lower() == "true",
        )
        await asyncio.sleep(5)


async def reconcile(key: Key) -> None:
    """
    Hold graph-family ownership through reads, mutations, and validation status.

    Args:
        key (Key): Resource kind, namespace and name to reconcile from fresh API state.

    Returns:
        None: No return value.
    """
    global last_api_success
    assert controller is not None and coordinator is not None
    async with coordinator.duty(key):
        try:
            await controller.reconcile(key)
        except Pending:
            if events is not None:
                obj = await controller.api.get(*key)
                if obj is not None:
                    await coordinator.guard()
                    await events.publish(obj)
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
        if events is not None:
            obj = await controller.api.get(*key)
            if obj is not None and key[0] in KINDS:
                await coordinator.guard()
                await events.publish(obj)


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
    Consume leased shards in order, acknowledging only after a refreshed attempt.

    Returns:
        None: No return value.
    """
    assert coordinator is not None and shared is not None and queue is not None
    while True:
        try:
            await shared.ping()
            for shard in sorted(coordinator.owned):
                if lifecycle.replacement.is_set() or lifecycle.draining.is_set():
                    break
                token = active_shard.set(shard)
                try:
                    await coordinator.guard()
                    message = await shared.take(shard)
                    if message is None:
                        continue
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
                except NotOwner:
                    pass  # Keep pending messages for the next lease holder.
                except Exception:
                    logger.exception("Shard %s delivery failed; keeping it pending", shard)
                finally:
                    active_shard.reset(token)
        except Exception:
            logger.exception("Dragonfly unavailable; queue consumption paused")
        await asyncio.sleep(1)


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
    await publish((body["kind"], namespace, name))
    for owner in body.get("metadata", {}).get("ownerReferences", []):
        if owner.get("controller") and owner.get("apiVersion") == f"{GROUP}/{VERSION}" and owner.get("kind") in KINDS:
            await publish((owner["kind"], namespace, owner["name"]))


for plural in ("graphs", "ephemeralgraphs", "feedbacks", "polygraphs", "rewrites", "compositions"):
    kopf.on.event(GROUP, VERSION, plural)(handle)


def write_backlog() -> dict[str, Any]:
    """
    Report local concrete API pressure, including separate coordination traffic.

    Returns:
        dict[str, Any]: Replica write gauges, including workload and coordination subtotals.
    """
    assert controller is not None and coordinator is not None
    workloads = controller.api.writes.snapshot()
    coordination = coordinator.api.writes.snapshot()
    intake = http.api.writes.snapshot() if http else {"queued": 0, "inFlight": 0, "total": 0}
    return {
        "scope": "replica",
        **{key: workloads[key] + coordination[key] + intake[key] for key in ("queued", "inFlight", "total")},
        "workloads": workloads,
        "coordination": coordination,
        "compositionIntake": intake,
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
        raise RuntimeError("composition API thread is unavailable")
    if event_http is not None and not event_http.thread.is_alive():
        raise RuntimeError("events API thread is unavailable")
    if metrics_http is not None and not metrics_http.thread.is_alive():
        raise RuntimeError("metrics API thread is unavailable")
    return {
        "metricsEnabled": metrics_http is not None,
        "eventsEnabled": event_http is not None,
        "initialized": initialized,
        "worker": True,
        "pending": queue.queue.qsize(),
        "backlog": {
            "inboundUpdates": shared.backlog(tuple(coordinator.owned)) if shared and coordinator else None,
            "kubernetesWrites": write_backlog(),
        },
        "apiFresh": time.monotonic() - last_api_success < 120
        and coordinator is not None
        and time.monotonic() - coordinator.last_success < 60,
        "cacheFresh": shared is not None and time.monotonic() - shared.last_success < 30,
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
    global initialized
    logger.debug("Operator cleanup started; joining listeners, queued work and API transports")
    initialized = False
    if metrics_http:
        await metrics_http.close()
    if event_http:
        await event_http.close()
    if http:
        await http.close()
    if queue:
        await queue.stop()
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    background.clear()
    if events:
        await events.close()
    if shared:
        await shared.close()
