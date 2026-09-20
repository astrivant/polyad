"""
Compare fixed and adaptive populations under identical finite producer schedules.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from abc import ABC, abstractmethod
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nature
from polyad_benchmarks.studies.soul.runtime.measurements import service_level
from polyad_benchmarks.studies.soul.runtime.service import serve

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from typing import Any

__all__ = (
    "Member",
    "PopulationMonitor",
    "produce",
)


def produce(pipe: Connection, count: int, rate: float, offset: int) -> None:
    """
    Offer uniquely identified real jobs at a bounded rate through an IPC pipe.

    Args:
        pipe (Connection): Parent-facing work channel.
        count (int): Number of finite arrivals.
        rate (float): Requested jobs per second before pipe backpressure.
        offset (int): First unique identity within this run.

    Returns:
        None: All arrivals and an end marker have been sent.
    """
    started = time.monotonic()
    try:
        for index in range(count):
            time.sleep(max(0, started + index / rate - time.monotonic()))
            identity = offset + index
            pipe.send((identity, identity + 1, time.monotonic()))
        pipe.send(None)
    finally:
        pipe.close()


@dataclass
class Member:
    """
    Retain a service incarnation and its outstanding work until a verified exit.

    Attributes:
        process (BaseProcess): Owned application service process.
        pipe (Connection): Service command and response channel.
        placement (nature.Placement): Required capability and declared cost.
        outstanding (set[int]): Jobs currently executing this stage.
        sample (dict[str, Any]): Most recent application report.
        stopped (bool): Whether the service acknowledged complete draining.
    """

    process: BaseProcess
    pipe: Connection
    placement: nature.Placement
    outstanding: set[int] = field(default_factory=set)
    sample: dict[str, Any] = field(default_factory=dict)
    stopped: bool = False


class PopulationMonitor(ABC):
    """
    Route actual jobs and preserve provenance for all local process decisions.
    """

    def __init__(self, study: str, config: dict[str, Any], adaptive: bool) -> None:
        """
        Construct an isolated trial without launching any process.

        Args:
            study (str): Soul or Nature population policy.
            config (dict[str, Any]): Prepared timing, load and budget recipe.
            adaptive (bool): Enable policy changes or retain the initial composition.
        """
        self.study, self.config, self.adaptive = study, config, adaptive
        self.context = mp.get_context("spawn")
        self.active: dict[str, Member] = {}
        self.owned: list[Member] = []
        self.producers: list[BaseProcess] = []
        self.events: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []
        self.frames: list[dict[str, Any]] = []
        self.phases: list[dict[str, Any]] = []
        self.jobs: dict[int, dict[str, Any]] = {}
        self.pending: deque[tuple[int, int, tuple[str, ...], int]] = deque()
        self.routes: tuple[tuple[str, ...], ...] = ()
        self.plan: nature.Plan | None = None
        self.phase = "starting"
        self.completed = 0
        self.rejected = 0
        self.offered = 0
        self.peak_services = 0
        self.started = time.monotonic()
        self.last_frame = 0.0
        self.settings = nature.Settings(service_limit=6, overlap_limit=10, candidate_limit=10000)

    def event(self, event: str, **data: Any) -> None:
        """
        Record parent decisions using the same clock as service measurements.

        Args:
            event (str): Decision or lifecycle name.
            **data (Any): Structured, JSON-compatible evidence.

        Returns:
            None: Append one event to this trial's retained evidence.
        """
        self.events.append({"event": event, "time": time.monotonic(), "phase": self.phase, **data})

    @abstractmethod
    def select(self, output: str) -> nature.Plan:
        """
        Choose the study's service composition without starting any process.

        Args:
            output (str): Required result label for the next load phase.

        Returns:
            nature.Plan: Selected service functions, routes and declared cost.
        """

    def population(self, output: str) -> None:
        """
        Select capabilities, prepare replacements, commit routes and retire exclusions.

        Args:
            output (str): Squared or enriched result required in the next phase.

        Returns:
            None: Compatible ready routes are committed before any producer starts.
        """

        # The fixed baseline cannot claim success by silently adopting a newly required capability.
        if self.active and not self.adaptive:
            compatible = self.plan is not None and self.plan.routes[0][-1].capability.output == output
            self.routes = tuple(tuple(place.name for place in route) for route in self.plan.routes) if compatible and self.plan else ()
            if not compatible:
                self.event("contract_unmet", required=output, reason="fixed capabilities cannot produce this result")
            return
        plan = self.select(output)
        old = self.active.copy()
        upcoming: dict[str, Member] = {}
        for name, placement in plan.placements.items():
            if name in old and old[name].placement == placement:
                upcoming[name] = old[name]
                self.event("service_survived", service=name, pid=old[name].process.pid)
                continue
            parent, child = self.context.Pipe()
            process = self.context.Process(target=serve, args=(name, placement.capability.name, child, self.adaptive, self.config))
            try:
                process.start()
            except BaseException:
                parent.close()
                raise
            finally:
                child.close()
            member = Member(process, parent, placement)
            self.owned.append(member)
            upcoming[name] = member
            self.event(
                "service_replaced" if name in old else "service_started",
                service=name,
                pid=process.pid,
                capability=placement.capability.name,
            )
        self.peak_services = max(self.peak_services, sum(member.process.is_alive() for member in self.owned))

        # Readiness is the commit barrier: route to replacements before retiring the old population.
        self.wait(lambda: all(member.sample.get("readyWorkers", 0) > 0 for member in upcoming.values()))
        self.active, self.plan = upcoming, plan
        self.routes = tuple(tuple(place.name for place in route) for route in plan.routes)
        self.event("composition_committed", routes=self.routes, cheeger=plan.expansion, cost=plan.cost)
        for name, member in old.items():
            if upcoming.get(name) is not member:
                self.retire(member)

    def receive(self) -> None:
        """
        Collect service state and verify each job before forwarding or completing it.

        Returns:
            None: No result releases an identity before its expected route stage matches.
        """
        for member in self.owned:
            if member.stopped:
                continue
            for _ in range(128):
                if not member.pipe.poll():
                    break
                kind, payload = member.pipe.recv()
                if kind == "sample":
                    member.sample = payload
                    self.samples.append({**payload, "pid": member.process.pid})
                elif kind == "event":
                    self.events.append(payload)
                elif kind == "result":
                    identity, value = payload
                    if identity not in member.outstanding:
                        raise RuntimeError("duplicate or unowned stage completion")
                    member.outstanding.remove(identity)
                    job = self.jobs[identity]
                    route, stage = job["route"], job["stage"]
                    if route[stage] != member.placement.name:
                        raise RuntimeError("completion came from the wrong service")
                    if stage + 1 < len(route):
                        self.pending.append((identity, value, route, stage + 1))
                    else:
                        expected = (identity + 1) ** 2 + (job["output"] == "enriched")
                        if value != expected or "finished" in job:
                            raise RuntimeError("final result violated the required output contract")
                        job["finished"] = time.monotonic()
                        self.completed += 1
                elif kind == "stopped":
                    if member.outstanding or not payload["allJoined"]:
                        raise RuntimeError("service stopped with unfinished work or live children")
                    member.stopped = True
                    break
                else:
                    raise RuntimeError("unknown service message")
            if member.process.exitcode is not None and not member.stopped:
                raise RuntimeError(f"{member.placement.name} exited before acknowledging cleanup")

    def wait(self, predicate: Callable[[], bool]) -> None:
        """
        Service result pipes while waiting for a finite lifecycle barrier.

        Args:
            predicate (Callable[[], bool]): Bounded readiness or drain condition.

        Returns:
            None: The condition passed before the configured deadline.
        """
        deadline = time.monotonic() + self.config["timeoutSeconds"]
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError("process study lifecycle deadline exceeded")
            self.receive()
            time.sleep(0.01)

    def retire(self, member: Member) -> None:
        """
        Stop one drained service and verify its entire worker tree has exited.

        Args:
            member (Member): Excluded or finally retired service incarnation.

        Returns:
            None: Service and its descendants have been joined.
        """
        member.pipe.send(("stop", None))
        self.wait(lambda: member.stopped)
        member.process.join(5)
        if member.process.exitcode != 0:
            raise RuntimeError("service did not exit cleanly")
        member.pipe.close()
        self.event("service_joined", service=member.placement.name, pid=member.process.pid)

    def configure(self, elapsed: float, duration: float) -> None:
        """
        Deliver reproducible external disturbances independently of policy decisions.

        Args:
            elapsed (float): Seconds since this load phase began.
            duration (float): Producer's scheduled phase duration.

        Returns:
            None: Service-specific controls demonstrate concurrent constraint responses.
        """
        for index, member in enumerate(self.active.values()):
            changing = self.phase == "constraints" and elapsed < duration
            data = {
                "phase": self.phase,
                "memoryReserved": (4096 if elapsed < duration * 0.25 else 3072) if changing and index == 1 else 0,
                "stale": changing and index == 2 and elapsed < duration * 0.6,
                "healthy": not (changing and index == 3 and int(elapsed * 4) % 2 == 0),
                "decision": "Stabilizing" if self.phase in {"surge", "constraints"} and index == 4 and elapsed < 0.8 else "Applied",
                "permission": "Expired"
                if changing and index == 5 and elapsed < duration * 0.5
                else "Pending"
                if self.phase == "surge" and index == 5 and elapsed < 0.8
                else "Active",
            }
            member.pipe.send(("configure", data))

    def dispatch(self) -> None:
        """
        Distribute future jobs while retaining route and ownership for accepted stages.

        Returns:
            None: Ineligible destinations keep work in the bounded parent queue.
        """
        for _ in range(len(self.pending)):
            identity, value, route, stage = self.pending.popleft()
            if not route:
                if self.adaptive and self.study == "soul":
                    eligible = [path for path in self.routes if self.active[path[0]].sample.get("admit")]
                    if eligible:
                        route = min(eligible, key=lambda path: len(self.active[path[0]].outstanding))
                else:
                    # Fixed skew makes spare capacity useful when rerouting is enabled.
                    index = 0 if self.study == "soul" and identity % 2 == 0 else identity % len(self.routes)
                    route = self.routes[index]
            member = self.active[route[stage]] if route else None
            if member is None or not member.sample.get("admit") or len(member.outstanding) >= 24:
                self.pending.append((identity, value, route if stage else (), stage))
                continue
            self.jobs[identity].update(route=route, stage=stage)
            member.outstanding.add(identity)
            member.pipe.send(("job", (identity, value, self.jobs[identity]["sent"])))

    def frame(self) -> None:
        """
        Capture the actual committed process graph and measured queue sizes.

        Returns:
            None: Append one timestamped frame for topology and time-series figures.
        """

        # In-flight jobs are not terminal failures; the frame uses only settled outcomes so far.
        terminal = self.completed + self.rejected
        latencies = [job["finished"] - job["sent"] for job in self.jobs.values() if "finished" in job]
        elapsed = max(1e-9, time.monotonic() - self.started)
        objective = service_level(
            serving=bool(self.routes),
            eligible=terminal,
            successful=self.completed,
            latencies=latencies,
            completed_per_second=self.completed / elapsed,
            adapting_seconds=None,
            policy=self.config["serviceLevel"],
        )
        self.frames.append(
            {
                "time": time.monotonic(),
                "phase": self.phase,
                "completed": self.completed,
                "pending": len(self.pending),
                "routes": self.routes,
                "serviceLevel": objective,
                "services": {
                    name: {
                        **member.sample,
                        "pid": member.process.pid,
                        "capability": member.placement.capability.name,
                        "outstanding": len(member.outstanding),
                    }
                    for name, member in self.active.items()
                },
            }
        )

    def load(self, phase: dict[str, Any]) -> None:
        """
        Offer one finite environmental phase and verify every accepted job.

        Args:
            phase (dict[str, Any]): Name, duration, rate and required output.

        Returns:
            None: Every offered identity is completed correctly or explicitly rejected.
        """
        self.phase = phase["name"]
        output = phase["output"] if self.study == "nature" else "squared"
        self.population(output)
        count = round(phase["seconds"] * phase["rate"])
        incoming, outgoing = self.context.Pipe(duplex=False)
        generator = self.context.Process(target=produce, args=(outgoing, count, phase["rate"], self.offered))
        generator.start()
        outgoing.close()
        self.producers.append(generator)
        start, done, end = time.monotonic(), False, self.completed + self.rejected + count
        self.event("phase_started", required=output, count=count, rate=phase["rate"])
        deadline, configured = start + self.config["timeoutSeconds"], 0.0
        try:
            while not done or self.completed + self.rejected < end:
                now = time.monotonic()
                if now >= deadline:
                    raise TimeoutError(f"{self.phase} did not settle its offered jobs")
                if now - configured >= 0.1:
                    self.configure(now - start, phase["seconds"])
                    configured = now
                self.receive()
                for _ in range(64):
                    if done or len(self.pending) >= 128 or not incoming.poll():
                        break
                    job = incoming.recv()
                    if job is None:
                        done = True
                        break
                    identity, value, sent = job
                    self.offered += 1
                    if not self.routes:
                        self.rejected += 1
                        continue
                    self.jobs[identity] = {"sent": sent, "output": output, "phase": self.phase}
                    self.pending.append((identity, value, (), 0))
                self.dispatch()
                if now - self.last_frame >= 0.1:
                    self.frame()
                    self.last_frame = now
                time.sleep(0.005)
            generator.join(5)
            if generator.exitcode != 0:
                raise RuntimeError("producer did not complete cleanly")
        finally:
            incoming.close()
        self.configure(phase["seconds"] + 1, phase["seconds"])
        self.wait(
            lambda: all(
                member.sample.get("profile") == "interactive" and member.sample.get("workers") == 1 and member.sample.get("admit")
                for member in self.active.values()
            )
        )
        self.frame()
        self.phases.append({"name": self.phase, "start": start, "finish": time.monotonic(), "offered": count, "output": output})
        self.event("phase_drained", completed=self.completed, rejected=self.rejected)

    def run(self) -> dict[str, Any]:
        """
        Exercise all phases and return measurements after complete process cleanup.

        Returns:
            dict[str, Any]: Raw evidence, performance, strategy coverage and cleanup proof.
        """
        for phase in self.config["phases"]:
            self.load(phase)
        for member in list(self.active.values()):
            self.retire(member)
        self.active.clear()
        coverage: Counter[str] = Counter()
        for member in self.owned:
            coverage.update(member.sample.get("coverage", {}))
        latencies = [job["finished"] - job["sent"] for job in self.jobs.values()]
        if self.offered != self.completed + self.rejected or any(member.process.exitcode != 0 for member in self.owned):
            raise RuntimeError("incomplete or unjoined process study")
        return {
            "mode": "adaptive" if self.adaptive else "fixed",
            "started": self.started,
            "offered": self.offered,
            "completed": self.completed,
            "rejected": self.rejected,
            "meanLatencySeconds": sum(latencies) / len(latencies) if latencies else None,
            "elapsedSeconds": time.monotonic() - self.started,
            "peakServices": self.peak_services,
            "allJoined": True,
            "coverage": dict(coverage),
            "serviceLevelPolicy": self.config["serviceLevel"],
            "resourceLoopPolicy": self.config["resourceLoop"],
            "events": self.events,
            "samples": self.samples,
            "frames": self.frames,
            "phases": self.phases,
            "jobs": self.jobs,
        }

    def close(self) -> None:
        """
        Terminate failed service trees through their cleanup handlers and join producers.

        Returns:
            None: No parent-owned process remains alive.
        """
        for process in [*self.producers, *(member.process for member in self.owned)]:
            if process.is_alive():
                process.terminate()
        for process in [*self.producers, *(member.process for member in self.owned)]:
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(3)
        for member in self.owned:
            member.pipe.close()
