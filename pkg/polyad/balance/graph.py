"""
Compose dynamic schedulers as resource-bounded, checkpointable workloads.
"""

import json
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

from polyad.balance.policy import ShortestRemaining
from polyad.balance.scheduler import Scheduler, State
from polyad.graph import Control, Estimate, Finalizer, Outcome, ShutdownContract, Statistics, Work, Workload
from polyad.graph.gates import Gate
from polyad.graph.hashing import shape_hash
from polyad.graph.rewrites import RewriteRegistry


class Graph:
    """
    Reserve a parent allocation around a separately balanced dynamic child graph.
    """

    def __init__(
        self,
        work: Work,
        units: Sequence[Workload],
        *,
        directory: Path,
        policy: ShortestRemaining | None = None,
        diagrams: bool = False,
        plots: bool = False,
        routes: Mapping[str, Gate] | None = None,
        facts: Callable[[], Mapping[str, bool]] | None = None,
        finalizers: tuple[Finalizer, ...] = (),
        resolve: Callable[[Work], Workload] | None = None,
        notify: Callable[[str], None] = print,
    ) -> None:
        """
        Bind child implementations to a parent workload boundary.

        Args:
            work (Work): Parent identity, prerequisites and entire child resource allocation.
            units (Sequence[Workload]): Initial child graph, including nested Graph instances.
            directory (Path): Unique child scheduler journal directory.
            policy (ShortestRemaining | None): Independent balancing policy inside this graph.
            diagrams (bool): Export child graph snapshots as well as parent snapshots.
            plots (bool): Export a PNG at graph creation and each rewrite.
            routes (Mapping[str, Gate] | None): Admission rules for child units.
            facts (Callable[[], Mapping[str, bool]] | None): Current routing observations.
            finalizers (tuple[Finalizer, ...]): Cleanup acknowledgements required before this graph releases its boundary.
            resolve (Callable[[Work], Workload] | None): Reconstruct dynamically added children after a process restart.
            notify (Callable[[str], None]): Event sink, prefixed with the containing graph name.
        """
        self.plots, self.routes, self.facts = plots, routes, facts
        self.rewrites = RewriteRegistry()
        self.finalizers = finalizers
        self.work = work
        self.units = tuple(units)
        self._initial_names = {unit.work.name for unit in self.units}
        self.directory, self.policy, self.diagrams = directory, policy, diagrams
        self.resolve, self.notify = resolve, notify
        self.scheduler: Scheduler | None = None

    @property
    def shape_hash(self) -> str:
        """
        Hash the current child graph or its initial unstarted structure.

        Returns:
            str: Digest incorporating every currently represented descendant boundary.
        """
        if self.scheduler is not None:
            return self.scheduler.shape_hash
        return shape_hash(self, tuple((unit.work, unit) for unit in self.units), self.routes or {})

    def rewrite(self, name: str) -> Future[None]:
        """
        Dispatch a local registered rewrite to the active boundary.

        Args:
            name (str): Registered operation name.

        Returns:
            Future[None]: Coordinator acknowledgement.
        """
        if self.scheduler is None:
            result: Future[None] = Future()
            result.set_exception(RuntimeError("graph has not started"))
            return result
        return self.scheduler.rewrite(name)

    def _restore(self, scheduler: Scheduler, checkpoint: dict[str, object]) -> None:
        """
        Restore graph membership, dependencies and child states without rerunning completed children.

        Args:
            scheduler (Scheduler): Fresh scheduler with validated initial implementations.
            checkpoint (dict[str, object]): Verified enclosing checkpoint payload.

        Returns:
            None: Matching child implementations receive their saved graph state.

        Raises:
            ValueError: Identity, graph membership or checkpoint lifecycle is incompatible.
        """
        if checkpoint.get("version") != 1 or checkpoint.get("fingerprint") != self.work.fingerprint:
            raise ValueError("graph checkpoint identity changed")
        records = cast(list[dict[str, object]], checkpoint["members"])
        names: set[str] = set()
        descriptions: dict[str, Work] = {}
        for record in records:
            description = cast(dict[str, object], record["work"])
            name = str(description["name"])
            if name in names:
                raise ValueError("duplicate graph checkpoint member")
            names.add(name)
            stats = cast(dict[str, object], record["statistics"])
            statistics = Statistics(
                int(str(stats["completed"])),
                None if stats["total"] is None else int(str(stats["total"])),
                Estimate(
                    remaining_seconds=cast(float | None, cast(dict[str, object], stats["estimate"])["remaining_seconds"]),
                    uncertainty_seconds=float(str(cast(dict[str, object], stats["estimate"])["uncertainty_seconds"])),
                    checkpoint_seconds=cast(float | None, cast(dict[str, object], stats["estimate"])["checkpoint_seconds"]),
                    resume_seconds=cast(float | None, cast(dict[str, object], stats["estimate"])["resume_seconds"]),
                ),
            )
            work = Work(
                name=name,
                fingerprint=str(description["fingerprint"]),
                requires=tuple(cast(list[str], description["requires"])),
                slots=int(str(description["slots"])),
                memory_bytes=int(str(description["memory_bytes"])),
                resumable=bool(description["resumable"]),
                statistics=statistics,
            )
            descriptions[name] = work
        removed = set(cast(list[str], checkpoint.get("removed", [])))
        if (set(scheduler.states) - names) - removed or removed & names:
            raise ValueError("initial graph contains work absent from checkpoint without a removal record")
        restored: dict[str, State] = {}
        scheduler._validate(descriptions)
        for name, work in descriptions.items():
            existing = scheduler.states.get(name)
            if existing is not None and existing.work.fingerprint == work.fingerprint:
                unit = existing.unit
            else:
                if self.resolve is None:
                    raise ValueError(f"graph requires a resolver for restored dynamic child: {name}")
                unit = self.resolve(work)
            if (unit.work.name, unit.work.fingerprint, unit.work.slots, unit.work.memory_bytes, unit.work.resumable) != (
                work.name,
                work.fingerprint,
                work.slots,
                work.memory_bytes,
                work.resumable,
            ):
                raise ValueError(f"child implementation or allocation changed: {name}")
            restored[name] = State(unit, work, work.statistics, time.monotonic())
        scheduler.states = restored
        for record in records:
            name = str(cast(dict[str, object], record["work"])["name"])
            state = scheduler.states[name]
            work = descriptions[name]
            if (state.work.fingerprint, state.work.slots, state.work.memory_bytes, state.work.resumable) != (
                work.fingerprint,
                work.slots,
                work.memory_bytes,
                work.resumable,
            ):
                raise ValueError(f"child implementation or allocation changed: {name}")
            status = str(record["status"])
            payload = record["checkpoint"]
            if status not in {"pending", "paused", "completed", "skipped"} or (status == "paused" and not isinstance(payload, dict)):
                raise ValueError("graph checkpoint contains non-quiescent work")
            state.work, state.statistics, state.status = work, work.statistics, status
            state.checkpoint = cast(dict[str, object] | None, payload)
        completed = {name for name, state in scheduler.states.items() if state.status == "completed"}
        if any(not set(scheduler.states[name].work.requires) <= completed for name in completed):
            raise ValueError("completed graph member has unfinished prerequisites")

    def run(self, control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Balance child work, propagate control and return only after all children are quiescent.

        Args:
            control (Control): Parent scheduler pause, cancellation and progress interface.
            checkpoint (dict[str, object] | None): Saved child graph and completed work.

        Returns:
            Outcome: Completion or a graph checkpoint with explicit child resume payloads.
        """
        scheduler = Scheduler(
            self.units,
            slots=self.work.slots,
            memory_bytes=self.work.memory_bytes,
            directory=self.directory,
            policy=self.policy,
            shutdown=ShutdownContract(finalizers=self.finalizers),
            diagrams=self.diagrams,
            plots=self.plots,
            rewrites=self.rewrites,
            routes=self.routes,
            facts=self.facts,
            # The enclosing graph checkpoint is authoritative; standalone child snapshots are not auto-loaded.
            notify=lambda message: self.notify(f"[{self.work.name}] {message}"),
        )
        if checkpoint is not None:
            self._restore(scheduler, checkpoint)
            scheduler.restore = True
            scheduler._publish_shape()
        self.scheduler = scheduler

        def report(statistics: Statistics) -> None:
            """
            Publish child completion counts while retaining graph-level pause cost estimates.

            Args:
                statistics (Statistics): Child graph aggregate, with a serial remaining-work estimate.

            Returns:
                None: Parent receives current graph progress and overhead estimates.
            """
            estimate = replace(
                statistics.estimate,
                checkpoint_seconds=self.work.statistics.estimate.checkpoint_seconds,
                resume_seconds=self.work.statistics.estimate.resume_seconds,
            )
            control.report(replace(statistics, estimate=estimate))

        statuses = scheduler.run(Control(control.pause, control.cancel, report))
        self.units = tuple(state.unit for state in scheduler.states.values())
        if all(status in {"completed", "skipped"} for status in statuses.values()):
            return Outcome()
        payload: dict[str, object] = {
            "version": 1,
            "fingerprint": self.work.fingerprint,
            "removed": sorted(self._initial_names - scheduler.states.keys()),
            "members": [
                {
                    "work": asdict(state.work),
                    "statistics": asdict(state.statistics),
                    "status": state.status,
                    "checkpoint": state.checkpoint,
                }
                for state in scheduler.states.values()
            ],
        }
        # Fail before returning if a child supplied non-JSON state.
        json.dumps(payload, allow_nan=False)
        return Outcome(payload)
