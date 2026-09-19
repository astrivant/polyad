"""
Run adaptive application processes with bounded queues and owned worker generations.
"""

from __future__ import annotations

import multiprocessing as mp
import signal
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nature
import soul
from polyad_benchmarks.studies.soul.runtime.measurements import ResourceLoop, service_level
from polyad_benchmarks.studies.soul.runtime.policy import Policy
from polyad_sdk import container_metrics

if TYPE_CHECKING:
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from typing import Any

PROFILES = {
    "interactive": soul.INTERACTIVE,
    "batch": soul.BATCH,
    "compact": soul.Profile("compact", 1, 2, 1.0),
}


@dataclass
class Worker:
    """
    Track one child and the exact batch it owns until results are verified.

    Attributes:
        process (BaseProcess): Owned child process.
        pipe (Connection): Duplex job and result channel.
        profile (str): Approved profile defining batch size and worker count.
        ready (bool): Whether startup was acknowledged.
        retiring (bool): Whether new assignments have stopped.
        jobs (tuple[tuple[int, int], ...]): Accepted identities and input values.
        stopping (bool): Whether the shutdown sentinel has been sent.
    """

    process: BaseProcess
    pipe: Connection
    profile: str
    ready: bool = False
    retiring: bool = False
    jobs: tuple[tuple[int, int], ...] = ()
    stopping: bool = False


class Service:
    """
    Keep business work draining while strategies change admission and worker plans.
    """

    def __init__(self, name: str, capability: str, pipe: Connection, adaptive: bool, config: dict[str, Any]) -> None:
        """
        Allocate process ownership and construct the complete policy before startup.

        Args:
            name (str): Stable service name.
            capability (str): Square, increment or fused function required by the parent.
            pipe (Connection): Parent command and result channel.
            adaptive (bool): Apply local profile proposals or retain the fixed profile.
            config (dict[str, Any]): Work timing, resource loop and service objectives.
        """
        self.name, self.capability, self.pipe, self.adaptive = name, capability, pipe, adaptive
        self.delay = config["workSeconds"]
        self.service_level_policy = config["serviceLevel"]
        self.resource_loop = ResourceLoop.from_config(config["resourceLoop"])
        self.resource_assigned = self.resource_loop.assigned
        self.resource_used = 0
        self.context = mp.get_context("spawn")
        self.children: list[Worker] = []
        self.owned: list[Worker] = []
        self.pending: deque[tuple[int, int]] = deque()
        self.profile = "interactive"
        self.candidate: str | None = None
        self.settings: dict[str, Any] = {}
        self.started = time.monotonic()
        self.last_change = self.started
        self.last_report = 0.0
        self.completed = 0
        self.accepted: set[int] = set()
        self.accepted_at: dict[int, float] = {}
        self.latencies: list[float] = []
        self.last_completed = 0
        self.last_sample_time = self.started
        self.candidate_started: float | None = None
        self.stopping = False
        self.phase = "starting"
        self.policy = Policy(name, self.read)

    def event(self, event: str, **data: Any) -> None:
        """
        Send lifecycle evidence to the parent alongside ordinary result messages.

        Args:
            event (str): Lifecycle or guard intervention name.
            **data (Any): JSON-compatible evidence without job payloads.

        Returns:
            None: A timestamped service event is sent.
        """
        self.pipe.send(("event", {"event": event, "time": time.monotonic(), "service": self.name, "phase": self.phase, **data}))

    def read(self) -> dict[str, Any]:
        """
        Combine actual queue/process state with explicitly injected environment inputs.

        Returns:
            dict[str, Any]: Current policy inputs, including prospective overlap cost.
        """
        target = self.candidate or self.policy.proposal if hasattr(self, "policy") else "interactive"
        additional = PROFILES[target].workers if target != self.profile and self.candidate is None else 0
        return {
            "backlog": len(self.pending) + sum(len(child.jobs) for child in self.children),
            "pids": [child.process.pid for child in self.children if child.ready and not child.retiring and child.profile == self.profile],
            "profile": self.profile,
            "projectedWorkers": len(self.children) + additional,
            "memoryReserved": self.settings.get("memoryReserved", 0),
            "resourceAssignedBytes": self.resource_assigned,
            "resourceModeledUsageBytes": self.resource_used,
            "resourceAvailableBytes": max(0, self.resource_assigned - self.resource_used),
            "resourcePressure": self.resource_used > self.resource_assigned,
            "healthy": self.settings.get("healthy", True),
            "stale": self.settings.get("stale", False),
            "decision": self.settings.get("decision", "Applied"),
            "permission": self.settings.get("permission", "Active"),
        }

    def spawn(self, profile: str) -> None:
        """
        Start one worker under already checked overlap and capability constraints.

        Args:
            profile (str): Approved worker implementation and batching policy.

        Returns:
            None: The child is retained for readiness, work tracking and cleanup.
        """
        parent, child = self.context.Pipe()
        compute = {"square": nature.square, "increment": nature.increment, "square-plus-one": nature.fused}[self.capability]
        process = self.context.Process(
            target=soul.worker,
            args=(child, PROFILES[profile], soul.Settings(work_seconds=self.delay), compute),
            name=f"{self.name}-{profile}",
        )
        try:
            process.start()
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        record = Worker(process, parent, profile)
        self.children.append(record)
        self.owned.append(record)
        self.event("worker_started", worker=process.pid, profile=profile, live=len(self.children))

    def receive(self) -> None:
        """
        Apply control changes and accept only uniquely identified bounded jobs.

        Returns:
            None: Controls update policy inputs; accepted work stays in the local ledger.
        """
        for _ in range(64):
            if not self.pipe.poll():
                break
            kind, payload = self.pipe.recv()
            if kind == "stop":
                self.stopping = True
            elif kind == "configure":
                self.settings = payload
                self.phase = payload["phase"]
            elif kind == "job":
                identity, value, offered_at = payload
                if identity in self.accepted or self.read()["backlog"] >= 24 or self.stopping:
                    raise RuntimeError("parent exceeded the unique-job or outstanding-work contract")
                self.accepted.add(identity)
                self.accepted_at[identity] = offered_at
                self.pending.append((identity, value))
            else:
                raise ValueError(f"unsupported control: {kind}")

    def collect(self) -> None:
        """
        Verify child results and join retired workers before spending their capacity.

        Returns:
            None: Verified results return to the parent; reaped workers release reservations.
        """
        for child in list(self.children):
            if child.pipe.poll() and not child.stopping:
                kind, payload = child.pipe.recv()
                if kind == "ready":
                    child.ready = True
                elif kind == "done":
                    compute = {"square": nature.square, "increment": nature.increment, "square-plus-one": nature.fused}[self.capability]
                    expected = [(identity, compute(value)) for identity, value in child.jobs]
                    if payload != expected:
                        raise RuntimeError("worker returned the wrong identities or capability results")
                    for identity, value in payload:
                        self.pipe.send(("result", (identity, value)))
                        self.completed += 1
                        self.latencies.append(time.monotonic() - self.accepted_at.pop(identity))
                    child.jobs = ()
                else:
                    raise RuntimeError("unexpected worker response")
            if child.retiring and child.ready and not child.jobs and not child.stopping:
                child.pipe.send(None)
                child.stopping = True
            if child.process.exitcode is not None:
                child.process.join()
                if not child.stopping or child.process.exitcode != 0:
                    raise RuntimeError("worker exited without draining")
                child.pipe.close()
                self.children.remove(child)
                self.event("worker_joined", worker=child.process.pid)

    def mutate(self) -> None:
        """
        Apply growth, compact replacement and recovery while preserving accepted work.

        Returns:
            None: Guards defer unsafe proposals; ready replacements commit before retirement.
        """
        if self.stopping:
            if not self.pending:
                for child in self.children:
                    child.retiring = True
            return
        proposal = self.policy.proposal if self.adaptive else "interactive"
        if (
            self.profile == "compact"
            and not self.read()["memoryReserved"]
            and not self.read()["resourcePressure"]
            and self.read()["backlog"] <= 2
        ):
            self.policy.proposal = proposal = "interactive"
        if (self.read()["memoryReserved"] or self.read()["resourcePressure"]) and self.adaptive:
            proposal = "compact"
            # Release surplus old workers before reserving a compact replacement.
            active = [child for child in self.children if not child.retiring and child.profile == self.profile]
            for child in active[1:]:
                child.retiring = True
        if self.candidate is not None:
            ready = [child for child in self.children if child.profile == self.candidate and child.ready and not child.retiring]
            if len(ready) == PROFILES[self.candidate].workers and self.policy.permits("fresh", "decision", "workers", "memory"):
                previous = self.profile
                self.profile, self.candidate = self.candidate, None
                self.candidate_started = None
                for child in self.children:
                    if child.profile != self.profile:
                        child.retiring = True
                self.last_change = time.monotonic()
                self.event("profile_committed", before=previous, after=self.profile, workers=len(ready), cheeger=soul.cheeger(len(ready)))
            return
        if proposal == self.profile or time.monotonic() - self.last_change < 0.2:
            return
        if not self.policy.permits("fresh", "decision", "workers", "memory"):
            return
        count = PROFILES[proposal].workers
        if len(self.children) + count > 4:
            return
        self.candidate = proposal
        self.candidate_started = time.monotonic()
        self.event("profile_proposed", profile=proposal, overlap=len(self.children) + count)
        for _ in range(count):
            self.spawn(proposal)

    def dispatch(self) -> None:
        """
        Drain already accepted jobs even when fresh admission is temporarily blocked.

        Returns:
            None: Idle workers receive bounded batches under the committed profile.
        """
        for child in self.children:
            if not self.pending or not child.ready or child.retiring or child.jobs or child.profile != self.profile:
                continue
            child.jobs = tuple(self.pending.popleft() for _ in range(min(len(self.pending), PROFILES[self.profile].batch)))
            child.pipe.send(child.jobs)

    def reconcile_resources(self) -> None:
        """
        Advance the local VPA analogue from measured workers and accepted backlog.

        Returns:
            None: Current modeled allocation and usage become policy inputs.
        """
        now = time.monotonic()
        assigned, used, changed = self.resource_loop.observe(now, workers=len(self.children), backlog=self.read()["backlog"])
        self.resource_assigned, self.resource_used = assigned, used
        if changed:
            self.event("resource_allocation_changed", assignedMemoryBytes=assigned, modeledUsageBytes=used)

    def report(self) -> None:
        """
        Report measurements and application admission decisions to the router.

        Returns:
            None: Include actual process identities, backlog, guard outcomes and coverage.
        """
        data = self.read()
        admitted = bool(self.policy.route_names) and self.policy.permits("fresh", "peer", "permission", "envelope")
        now = time.monotonic()
        elapsed = max(1e-9, now - self.last_sample_time)
        completed_per_second = (self.completed - self.last_completed) / elapsed
        adapting_seconds = now - self.candidate_started if self.candidate_started is not None else None
        objective = service_level(
            serving=admitted and not self.stopping,
            eligible=self.completed,
            successful=self.completed,
            latencies=self.latencies,
            completed_per_second=completed_per_second,
            adapting_seconds=adapting_seconds,
            policy=self.service_level_policy,
        )
        cgroup = container_metrics()
        self.pipe.send(
            (
                "sample",
                {
                    "time": time.monotonic(),
                    "service": self.name,
                    "phase": self.phase,
                    "backlog": data["backlog"],
                    "completed": self.completed,
                    "profile": self.profile,
                    "workers": len(self.children),
                    "pids": [child.process.pid for child in self.children],
                    "children": [
                        {"pid": child.process.pid, "profile": child.profile, "ready": child.ready, "retiring": child.retiring}
                        for child in self.children
                    ],
                    "readyWorkers": len(data["pids"]),
                    "admit": admitted and not self.stopping,
                    "guards": {name: result.state for name, result in self.policy.assessments.items()},
                    "coverage": dict(self.policy.coverage),
                    "deltas": self.policy.changes,
                    "serviceLevel": objective,
                    "adaptationInProgress": self.candidate is not None,
                    "resourceAssignedBytes": data["resourceAssignedBytes"],
                    "resourceModeledUsageBytes": data["resourceModeledUsageBytes"],
                    "resourceAvailableBytes": data["resourceAvailableBytes"],
                    "cgroup": {
                        "cpuUsageUsec": cgroup.cpu_usage_usec,
                        "cpuLimitMillicores": cgroup.cpu_limit_millicores,
                        "memoryUsageBytes": cgroup.memory_usage_bytes,
                        "memoryLimitBytes": cgroup.memory_limit_bytes,
                        "memoryAvailableBytes": cgroup.memory_available_bytes,
                    },
                },
            )
        )
        self.last_completed, self.last_sample_time = self.completed, now

    def run(self) -> None:
        """
        Run observation, adaptation, dispatch and draining on one service loop.

        Returns:
            None: A stop command drained every accepted job and joined all workers.
        """
        self.spawn("interactive")
        while not self.stopping or self.children:
            self.receive()
            self.collect()
            self.reconcile_resources()
            self.policy.update()
            self.mutate()
            self.dispatch()
            if time.monotonic() - self.last_report >= 0.1:
                self.report()
                self.last_report = time.monotonic()
            time.sleep(0.01)
        self.pipe.send(("stopped", {"completed": self.completed, "allJoined": all(c.process.exitcode == 0 for c in self.owned)}))

    def close(self) -> None:
        """
        Release every worker on errors as well as normal shutdown.

        Returns:
            None: All owned worker processes have been reaped and pipes closed.
        """
        for child in self.owned:
            if child.process.is_alive():
                child.process.terminate()
        for child in self.owned:
            child.process.join(3)
            if child.process.is_alive():
                child.process.kill()
                child.process.join(3)
            child.pipe.close()
        self.pipe.close()


def serve(name: str, capability: str, pipe: Connection, adaptive: bool, config: dict[str, Any]) -> None:
    """
    Own one service and clean up its descendants on interruption or failure.

    Args:
        name (str): Service identity.
        capability (str): Required processing function.
        pipe (Connection): Parent control channel.
        adaptive (bool): Enable admitted worker-profile changes.
        config (dict[str, Any]): Work timing, resource loop and service objectives.

    Returns:
        None: Service shutdown and descendant cleanup finished.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, soul.interrupt)
    service = Service(name, capability, pipe, adaptive, config)
    try:
        service.run()
    finally:
        service.close()
