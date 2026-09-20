"""
Discover ready event replicas and stagger connection rolls on the existing runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import os
import random
import time
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from polyad.operator.observability.decisions import decision
from polyad_types.events.envelope import EventRebalanceSettings

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API

__all__ = (
    "DRAIN_FILE",
    "Rebalancer",
    "TRIGGER",
    "configuration",
    "drain",
)


DRAIN_FILE = Path("/tmp/polyad-events-draining")
TRIGGER = "polyad.astrivant.com/event-copulse"
logger = logging.getLogger(__name__)


def configuration() -> EventRebalanceSettings:
    """
    Load the chart's typed optional connection policy.

    Returns:
        EventRebalanceSettings: Validated settings, disabled unless explicitly enabled.
    """
    return EventRebalanceSettings(**json.loads(os.environ.get("POLYAD_EVENTS_REBALANCE", "{}")))


class Rebalancer:
    """
    Share a bounded subscriber schedule between HTTP threads and one async membership poller.
    """

    def __init__(self, settings: EventRebalanceSettings, *, poll_interval: float = 1) -> None:
        """
        Initialize a replica-local roll coordinator without a second HTTP server.

        Args:
            settings (EventRebalanceSettings): Administrator-defined transport pacing.
            poll_interval (float): Event read delay reserved at the end of a termination drain.
        """
        self.settings = settings
        self.poll_interval = poll_interval
        self.lock = Lock()
        self.members: list[dict[str, Any]] = []
        self.revision = ""
        self.observed = 0.0
        self.trigger: str | None = None
        self.pending = ""
        self.next_roll = 0.0
        self.draining = False
        self.streams: dict[str, float] = {}
        self.schedule: dict[str, tuple[float, str]] = {}
        self.drain_deadline = math.inf
        self.batch_size = 1
        self.batch_interval = settings.intervalSeconds
        self.batch_reset = 0.0
        self.batch_sent = 0

    def register(self) -> str:
        """
        Enroll a subscription after HTTP authorization and capacity admission.

        Returns:
            str: Local opaque identity; a draining replica rejects enrollment.
        """
        with self.lock:
            if self.draining or DRAIN_FILE.exists():
                raise RuntimeError("event replica is draining")
            identity = uuid4().hex
            self.streams[identity] = time.monotonic()
            return identity

    def release(self, identity: str) -> None:
        """
        Forget a subscription and any scheduled reset when its response closes.

        Args:
            identity (str): Replica-local subscription identity.

        Returns:
            None: Repeated cleanup is harmless.
        """
        with self.lock:
            self.streams.pop(identity, None)
            self.schedule.pop(identity, None)

    def _roll(self, reason: str, now: float, *, drain: bool = False, identities: list[str] | None = None) -> None:
        identities = sorted(self.streams if identities is None else identities)
        batch = max(1, math.ceil(len(identities) * self.settings.batchPercent / 100))
        batches = max(1, math.ceil(len(identities) / batch))
        interval = self.settings.intervalSeconds
        if drain:
            remaining = max(0, self.drain_deadline - now - self.poll_interval - 1)
            interval = min(interval, remaining / (batches + 1))
        self.batch_size, self.batch_interval, self.batch_reset, self.batch_sent = batch, interval, now, 0

        # Jitter separates operators which observe the same membership change together.
        start = now + random.uniform(0, interval)
        for index, identity in enumerate(identities):
            self.schedule[identity] = (start + (index // batch) * interval, reason)
        self.next_roll = max(now + self.settings.cooldownSeconds, start + batches * interval)
        decision(
            "polyad.events.copulse",
            f"Scheduling {len(identities)} event subscriptions to reconnect in batches of {batch}: {reason}.",
            outcome="scheduled",
            reason=reason,
            attributes={"polyad.events.subscribers": len(identities), "polyad.events.batch_size": batch},
        )

    def update(self, members: list[dict[str, Any]], trigger: str) -> None:
        """
        Coalesce membership and administrative changes behind the local cooldown.

        Args:
            members (list[dict[str, Any]]): Ready nonterminating event Pods from the selected Service.
            trigger (str): Administrator's opaque Service annotation value.

        Returns:
            None: Newly opened connections never join an already scheduled roll.
        """
        members = sorted(members, key=lambda item: item["uid"])
        revision = hashlib.sha256(json.dumps(members, sort_keys=True).encode()).hexdigest()
        now = time.monotonic()
        with self.lock:
            if self.revision and revision != self.revision and self.settings.automatic:
                self.pending = "membership_changed"
            if self.trigger is not None and trigger != self.trigger:
                self.pending = "administrator_requested"
            self.members, self.revision, self.trigger, self.observed = members, revision, trigger, now
            if self.pending and members and not self.draining and now >= self.next_roll:
                self._roll(self.pending, now)
                self.pending = ""

    def endpoints(self) -> dict[str, Any]:
        """
        Advertise only current members of this exact operator event group.

        Returns:
            dict[str, Any]: Routing mode, ready membership and a bounded cache lifetime.
        """
        with self.lock:
            if not self.revision or time.monotonic() - self.observed > self.settings.refreshSeconds * 3:
                raise RuntimeError("operator endpoint discovery is stale")
            return {
                "routing": self.settings.routing,
                "revision": self.revision,
                "refreshSeconds": self.settings.refreshSeconds,
                "replicas": len(self.members),
                "endpoints": [dict(item) for item in self.members] if self.settings.routing == "Direct" else [],
            }

    def control(self, identity: str) -> dict[str, Any] | None:
        """
        Consume a due reset, or initiate a bounded pre-termination drain.

        Args:
            identity (str): Current subscription's local identity.

        Returns:
            dict[str, Any] | None: Copulse payload without a replay cursor or destination URL.
        """
        now = time.monotonic()
        with self.lock:
            if DRAIN_FILE.exists() and not self.draining:
                self.draining = True
                try:
                    started = float(DRAIN_FILE.read_text())
                    if not math.isfinite(started):
                        started = now
                except (OSError, ValueError):
                    started = now
                self.drain_deadline = min(now, started) + self.settings.drainSeconds
                self._roll("replica_draining", now, drain=True)
            if identity not in self.streams:
                return None
            maximum = self.settings.maxConnectionSeconds
            if maximum and not self.draining and now >= self.next_roll:
                aged = [item for item, started in self.streams.items() if now - started >= maximum]
                if aged:
                    self._roll("connection_age", now, identities=aged)
            if identity not in self.schedule:
                return None
            when, reason = self.schedule.get(identity, (math.inf, ""))
            urgent = self.draining and now >= self.drain_deadline - self.poll_interval - 1
            if now < when and not urgent:
                return None
            if now >= self.batch_reset:
                self.batch_sent = 0
                self.batch_reset = now + self.batch_interval
            if not urgent and self.batch_sent >= self.batch_size:
                return None
            self.batch_sent += 1
            self.schedule.pop(identity, None)
            return {
                "reason": reason,
                "revision": self.revision,
                "retryAfterSeconds": random.uniform(
                    0, min(self.settings.intervalSeconds, 1) if self.draining else self.settings.intervalSeconds
                ),
            }

    async def watch(self, api: API, namespace: str, service_name: str) -> None:
        """
        Refresh ready Pod addresses for the chart-owned event Service using existing RBAC.

        Args:
            api (API): Existing asynchronous Kubernetes adapter.
            namespace (str): Operator namespace.
            service_name (str): Administrator-configured local events Service.

        Returns:
            None: Runs until runtime shutdown cancels this task; failures expire the directory.
        """
        while True:
            try:
                service = await api.get("Service", namespace, service_name)
                if service is None or service.get("metadata", {}).get("deletionTimestamp"):
                    raise RuntimeError("event Service is absent")
                selector = service["spec"].get("selector", {})
                if not selector or not any(port.get("port") == 8091 for port in service["spec"].get("ports", [])):
                    raise RuntimeError("event Service requires a Pod selector and event port")
                result = await api.request(
                    "GET",
                    "Pod",
                    namespace,
                    query=[
                        ("labelSelector", ",".join(f"{key}={value}" for key, value in sorted(selector.items()))),
                        ("limit", "257"),
                    ],
                )
                if len(result["items"]) > 256 or result.get("metadata", {}).get("continue"):
                    raise RuntimeError("event group exceeds 256 replicas")
                members = []
                for pod in result["items"]:
                    meta, status = pod["metadata"], pod.get("status", {})
                    if meta.get("deletionTimestamp") or not any(
                        entry.get("type") == "Ready" and entry.get("status") == "True" for entry in status.get("conditions", [])
                    ):
                        continue
                    address = str(ipaddress.ip_address(status["podIP"]))
                    members.append({"uid": meta["uid"], "address": address, "port": 8091})
                self.update(members, service["metadata"].get("annotations", {}).get(TRIGGER, ""))
            except Exception:
                logger.warning("Event replica discovery failed; stale addresses will not be advertised", exc_info=True)
            await asyncio.sleep(self.settings.refreshSeconds)


def drain() -> None:
    """
    Run the chart preStop hook before SIGTERM while the event server is still serving.

    Returns:
        None: Admission closes immediately; existing streams get the configured drain window.
    """
    settings = configuration()
    from polyad.operator.lifecycle.roles import serves

    if settings.enabled and serves("EVENTS"):
        DRAIN_FILE.write_text(str(time.monotonic()))
        time.sleep(settings.drainSeconds)


if __name__ == "__main__":
    drain()
