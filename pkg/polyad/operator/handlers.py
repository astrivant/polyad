"""Observe with Kopf; coordinate all mutations through leased, refreshed queues."""

import asyncio
import logging
import os
import time
from typing import Any

import kopf
from cattrs.errors import BaseValidationError

from polyad.operator.api import API, GROUP, VERSION
from polyad.operator.controller import Controller, Pending
from polyad.operator.coordination import Coordinator, NotOwner, active_shard
from polyad.operator.queue import Key, RefreshQueue
from polyad.operator.shared_queue import SharedQueue

queue: RefreshQueue | None = None
controller: Controller | None = None
coordinator: Coordinator | None = None
shared: SharedQueue | None = None
last_api_success = 0.0
initialized = False
background: list[asyncio.Task[None]] = []
KINDS = ("Graph", "EphemeralGraph", "Feedback", "Rewrite")
logger = logging.getLogger(__name__)


@kopf.on.startup()
async def startup(settings: kopf.OperatorSettings, **_: Any) -> None:
    """Watch without Kopf progress/finalizer mutations that bypass shard ownership."""
    global queue, controller, coordinator, shared, initialized
    settings.posting.enabled = False
    settings.scanning.disabled = True
    settings.networking.request_timeout = 30
    namespace = os.environ.get("POLYAD_NAMESPACE", "default")
    coordinator = Coordinator(API(), namespace)
    controller = Controller(API(before_write=coordinator.guard))
    shared = SharedQueue(os.environ.get("POLYAD_DRAGONFLY_URL", "redis://localhost:6379/0"), namespace, coordinator.identity)
    queue = RefreshQueue(reconcile)
    queue.start()
    initialized = True
    background.extend([asyncio.create_task(coordination_loop()), asyncio.create_task(rescan_loop()), asyncio.create_task(consume_loop())])


async def coordination_loop() -> None:
    """Renew ownership independently of slow reconciliation, failing closed on expiry."""
    assert coordinator is not None and shared is not None
    while True:
        try:
            await shared.ping()  # A replica unable to consume must stop advertising availability.
            await coordinator.tick()
        except Exception:
            coordinator.leader = False
            logger.exception("Coordination failed; expired ownership will stop writes")
        await asyncio.sleep(5)


async def rescan_loop() -> None:
    """Rediscover duties after handoff and retry from API state without watch dependence."""
    global last_api_success
    assert coordinator is not None and queue is not None
    while True:
        try:
            for kind in KINDS:
                result = await coordinator.api.request("GET", kind, coordinator.namespace)
                last_api_success = time.monotonic()
                for obj in result.get("items", []):
                    await publish((kind, coordinator.namespace, obj["metadata"]["name"]))
        except Exception:
            logger.exception("Resource rescan failed; retrying from fresh state")
        await asyncio.sleep(5)


async def reconcile(key: Key) -> None:
    """Hold graph-family ownership through reads, mutations, and validation status."""
    global last_api_success
    assert controller is not None and coordinator is not None
    async with coordinator.duty(key):
        try:
            await controller.reconcile(key)
        except (ValueError, TypeError, BaseValidationError) as error:
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
        last_api_success = time.monotonic()


async def publish(key: Key) -> None:
    """Publish resource hints to shared storage rather than replica-local watch queues."""
    assert coordinator is not None and shared is not None
    await shared.publish(await coordinator.shard_for(key), key)


async def consume_loop() -> None:
    """Consume leased shards in order, acknowledging only after a refreshed attempt."""
    assert coordinator is not None and shared is not None and queue is not None
    while True:
        try:
            await shared.ping()
            for shard in sorted(coordinator.owned):
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
    """Raw observation handlers never ask Kopf to patch resource bookkeeping."""
    assert namespace is not None
    await publish((body["kind"], namespace, name))


for plural in ("graphs", "ephemeralgraphs", "feedbacks", "rewrites"):
    kopf.on.event(GROUP, VERSION, plural)(handle)


@kopf.on.probe(id="scheduler")
def health(**_: Any) -> dict[str, Any]:
    """Check workers and renewal loops; an idle follower remains healthy."""
    if not initialized or queue is None or queue.task is None or queue.task.done() or any(task.done() for task in background):
        raise RuntimeError("operator worker is unavailable")
    return {
        "initialized": initialized,
        "worker": True,
        "pending": queue.queue.qsize(),
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
    """Join outstanding writes before stopping renewals; let leases expire on shutdown."""
    global initialized
    initialized = False
    if queue:
        await queue.stop()
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    background.clear()
    if shared:
        await shared.close()
