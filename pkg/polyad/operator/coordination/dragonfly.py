"""
Bridge KEDA scale intent to the upstream Dragonfly operator without competing writers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from polyad.exceptions.coordination import NotOwner
from polyad.operator.adapters.kubernetes import API
from polyad.operator.coordination.contracts import capture_decision
from polyad.operator.coordination.leases import WRITE_BUDGET

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.coordination.leases import Coordinator
    from polyad.operator.coordination.shared_queue import SharedQueue

__all__ = (
    "connections",
    "reconcile",
    "run",
)


logger = logging.getLogger(__name__)


async def connections(shared: SharedQueue) -> dict[str, Any]:
    """
    Sample primary clients once per publication, without retaining failed observations.

    Args:
        shared (SharedQueue): Operator connection to the bundled primary endpoint.

    Returns:
        dict[str, Any]: A fresh connection count or an explicitly unavailable sample.
    """
    try:
        info = await shared.client.info()
        if info.get("role") != "master":
            await shared.client.connection_pool.disconnect()
            raise ValueError("cache connection no longer points to the primary")
        value = int(info["connected_clients"])
        if value < 0:
            raise ValueError("negative connection count")
        return {"enabled": True, "fresh": True, "connections": value}
    except Exception:
        logger.exception("Dragonfly connection sampling failed")
        return {"enabled": True, "fresh": False}


async def reconcile(api: API, namespace: str, name: str) -> None:
    """
    Refresh cache readiness before forwarding a bounded, single-instance scale step.

    Args:
        api (API): Lease-fenced management API, never a remote execution adapter.
        namespace (str): Release namespace containing the pool and cache.
        name (str): Shared pool and Dragonfly resource name.

    Returns:
        None: Pool status reports observed Pods; upstream owns their lifecycle.
    """
    pool = await api.get("DragonflyPool", namespace, name)
    cache = await api.get("Dragonfly", namespace, name)
    if not pool or not cache or any(obj["metadata"].get("deletionTimestamp") for obj in (pool, cache)):
        return
    spec = pool["spec"]
    if not 2 <= spec["minReplicas"] <= spec["replicas"] <= spec["maxReplicas"] <= 9:
        raise ValueError("Dragonfly replica request is outside HA bounds")
    sts = await api.get("StatefulSet", namespace, name)
    if not sts or sts["metadata"].get("deletionTimestamp"):
        return
    if not any(
        owner.get("uid") == cache["metadata"]["uid"] and owner.get("controller") for owner in sts["metadata"].get("ownerReferences", [])
    ):
        raise ValueError("Dragonfly does not own the observed StatefulSet")
    current = cache["spec"]["replicas"]
    observed = sts.get("status", {})
    ready = (
        cache.get("status", {}).get("phase", "").lower() == "ready"
        and not cache.get("status", {}).get("isRollingUpdate", False)
        and sts["spec"].get("replicas") == current
        and observed.get("observedGeneration") == sts["metadata"]["generation"]
        and observed.get("replicas") == current
        and observed.get("readyReplicas") == current
        and observed.get("currentRevision") == observed.get("updateRevision")
    )
    desired = spec["replicas"]
    selector = sts["spec"]["selector"]
    if selector.get("matchExpressions") or not selector.get("matchLabels"):
        raise ValueError("unsupported Dragonfly Pod selector")

    # Change one replica at a time, and wait for replication and rollout convergence between steps.
    if ready and current != desired:
        await api.request(
            "PATCH",
            "Dragonfly",
            namespace,
            name,
            {
                "metadata": {"resourceVersion": cache["metadata"]["resourceVersion"]},
                "spec": {"replicas": current + (1 if desired > current else -1)},
            },
        )
        ready = False
    status = {
        "replicas": observed.get("replicas", 0),
        "readyReplicas": observed.get("readyReplicas", 0),
        "labelSelector": ",".join(f"{key}={value}" for key, value in sorted(selector["matchLabels"].items())),
        "observedGeneration": pool["metadata"]["generation"],
        "phase": "Ready" if ready else "WaitingForReplication",
    }
    if pool.get("status") != status:
        await api.request(
            "PATCH",
            "DragonflyPool",
            namespace,
            name,
            {"metadata": {"resourceVersion": pool["metadata"]["resourceVersion"]}, "status": status},
            status=True,
        )


async def run(coordinator: Coordinator, shared: SharedQueue, name: str) -> None:
    """
    Fence cache scaling independently of graph shards assigned to execution workers.

    Args:
        coordinator (Coordinator): Root-local lease client and unique process identity.
        shared (SharedQueue): Root cache availability check before mutations.
        name (str): Configured release-owned pool; arbitrary pools are never scanned.

    Returns:
        None: Runs until shutdown cancels the task.
    """
    lease_name = f"polyad-cache-{name}"

    async def guard() -> None:
        await shared.ping()
        lease = await coordinator.api.get("Lease", coordinator.namespace, lease_name)
        if (
            not lease
            or lease["spec"].get("holderIdentity") != coordinator.identity
            or time.monotonic() >= coordinator.deadlines.get(lease_name, 0) - WRITE_BUDGET
        ):
            raise NotOwner("cache scaling lease lost or renewal overdue")

    api = API(before_write=guard)
    try:
        while True:
            try:
                if await coordinator.claim(lease_name):
                    with capture_decision(api, ("DragonflyPool", coordinator.namespace, name)):
                        await reconcile(api, coordinator.namespace, name)
            except Exception:
                logger.exception("Dragonfly scale reconciliation deferred")
            await asyncio.sleep(15)
    finally:
        api.client.close()
