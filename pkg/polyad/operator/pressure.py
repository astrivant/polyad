"""
Measure component HTTP demand and aggregate short-lived reports at the root.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any

    from polyad.operator.shared_queue import SharedQueue


class Pressure:
    """
    Count requests through response completion, including open event streams.
    """

    def __init__(self) -> None:
        """
        Keep a bounded sixty-second request window shared by listener threads.
        """
        self.lock = threading.Lock()
        self.arrivals: deque[float] = deque(maxlen=100000)
        self.in_flight = 0

    def wrap(self, app: Any) -> Any:
        """
        Instrument a WSGI application without changing its response or streaming semantics.

        Args:
            app (Any): WSGI application accepted by Waitress.

        Returns:
            Any: WSGI wrapper retaining request pressure until response cleanup.
        """

        def application(environ: Any, start_response: Any) -> Iterable[bytes]:
            with self.lock:
                self.arrivals.append(time.monotonic())
                self.in_flight += 1
            response = None
            try:
                response = app(environ, start_response)
                yield from response
            finally:
                try:
                    if response is not None and hasattr(response, "close"):
                        response.close()
                finally:
                    with self.lock:
                        self.in_flight -= 1

        return application

    def snapshot(self) -> dict[str, float]:
        """
        Sample concurrent responses and mean arrivals per second over the last minute.

        Returns:
            dict[str, float]: Per-process values summed once by the root metrics component.
        """
        with self.lock:
            cutoff = time.monotonic() - 60
            while self.arrivals and self.arrivals[0] < cutoff:
                self.arrivals.popleft()
            return {"requestsPerSecond": len(self.arrivals) / 60, "inFlight": self.in_flight}


pressure = Pressure()


async def report(shared: SharedQueue, component: str) -> None:
    """
    Register one process's measured demand with a bounded heartbeat lifetime.

    Args:
        shared (SharedQueue): Root cache connection and unique process identity.
        component (str): Deployment component served by this process.

    Returns:
        None: Missing heartbeats remain visible as stale during the membership grace period.
    """
    prefix = f"{shared.prefix}:components"
    seconds, microseconds = await shared.client.time()
    observed = seconds + microseconds / 1_000_000
    async with shared.client.pipeline(transaction=True) as transaction:
        transaction.set(prefix + ":" + shared.consumer, json.dumps({"component": component, **pressure.snapshot()}), ex=15)
        transaction.zadd(prefix, {shared.consumer: observed})
        transaction.zremrangebyscore(prefix, "-inf", observed - 90)
        await transaction.execute()


async def collect(shared: SharedQueue) -> dict[str, Any]:
    """
    Read each recent process once, avoiding duplicate metrics from HA scrape replicas.

    Args:
        shared (SharedQueue): Root cache namespace shared by the service components.

    Returns:
        dict[str, Any]: Component totals and explicit aggregate freshness.
    """
    prefix = f"{shared.prefix}:components"
    try:
        members = await shared.client.zrange(prefix, 0, -1)
        values = await shared.client.mget([prefix + ":" + member for member in members]) if members else []
        result: dict[str, Any] = {"fresh": bool(members) and all(values), "roles": {}}
        for value in values:
            if not value:
                continue
            entry = json.loads(value)
            group = result["roles"].setdefault(entry["component"], {"replicas": 0, "requestsPerSecond": 0.0, "inFlight": 0})
            group["replicas"] += 1
            for field in ("requestsPerSecond", "inFlight"):
                group[field] += entry[field]
        return result
    except Exception:
        return {"fresh": False, "roles": {}}


def demand(snapshot: dict[str, Any], component: str, metric: str) -> float:
    """
    Select fresh global demand for a component's KEDA AverageValue target.

    Args:
        snapshot (dict[str, Any]): Cached root metrics snapshot.
        component (str): Executor, gateway or telemetry component.
        metric (str): backlog, requestsPerSecond or inFlight.

    Returns:
        float: Global measured demand; unavailable samples raise instead of returning zero.
    """
    if component == "executor" and metric == "backlog":
        samples = [snapshot, *snapshot.get("clusters", {}).values()]
        if any(not sample.get("inbound", {}).get("fresh") for sample in samples):
            raise ValueError("queue demand is stale")
        return float(sum(sample["inbound"]["total"] for sample in samples))
    if component not in {"gateway", "telemetry"} or metric not in {"requestsPerSecond", "inFlight"}:
        raise KeyError("unknown component metric")
    components = snapshot.get("components", {})
    if not components.get("fresh") or component not in components.get("roles", {}):
        raise ValueError("component demand is stale")
    return float(components["roles"][component][metric])
