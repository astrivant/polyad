"""
Schedule local graph feedback in rounds with explicit termination bounds.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import Future
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from polyad.graph import Control, Outcome, Statistics
from polyad.graph.hashing import digest
from polyad.graph.rewrites import RewriteRegistry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from polyad.balance.graph import Graph
    from polyad.graph import Work


class FeedbackGraph:
    """
    Turn a graph's completion into its next activation instead of unlocking a deadlocked cycle.
    """

    def __init__(
        self,
        work: Work,
        factory: Callable[[int], Graph],
        *,
        directory: Path,
        rounds: int,
        stop_when: Callable[[int], bool] | None = None,
        interval_seconds: float = 0.1,
        diagrams: bool = False,
        notify: Callable[[str], None] = print,
    ) -> None:
        """
        Define explicit feedback boundaries and a deterministic per-round graph factory.

        Args:
            work (Work): Enclosing identity, prerequisites and reserved capacity.
            factory (Callable[[int], Graph]): Reconstruct graph for a zero-based round number.
            directory (Path): Feedback event and diagram directory, separate from child journals.
            rounds (int): Hard limit on completed rounds, preserved across checkpoints.
            stop_when (Callable[[int], bool] | None): Optional early-stop condition evaluated at round boundaries.
            interval_seconds (float): Interruptible delay between rounds to prevent a busy loop.
            diagrams (bool): Write the explicit feedback edge as Mermaid.
            notify (Callable[[str], None]): Human-readable feedback events.
        """
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
            raise ValueError("rounds must be a finite nonnegative integer")
        if not math.isfinite(interval_seconds) or interval_seconds < 0:
            raise ValueError("feedback interval must be nonnegative")
        self.rewrites = RewriteRegistry()
        self.current_graph: Graph | None = None
        self.work, self.factory, self.directory = work, factory, directory
        self.rounds, self.interval_seconds = rounds, interval_seconds
        self.stop_when = stop_when
        self.diagrams, self.notify = diagrams, notify

    @property
    def shape_hash(self) -> str:
        """
        Describe bounded recurrence and its currently materialized body.

        Returns:
            str: Digest; future factory-produced rounds are explicitly unknown.
        """
        return digest(
            {"kind": "feedback", "round_limit": self.rounds, "body": self.current_graph.shape_hash if self.current_graph else None}
        )

    def rewrite(self, name: str) -> Future[None]:
        """
        Apply a feedback-local proposal to the currently active round boundary.

        Args:
            name (str): Registered feedback operation.

        Returns:
            Future[None]: Active coordinator acknowledgement or an inactive-boundary error.
        """
        try:
            proposal = self.rewrites.get(name)
            if self.current_graph is None or self.current_graph.scheduler is None:
                raise RuntimeError("feedback round has not started")
            return self.current_graph.scheduler.apply(proposal, label=f"feedback/{name}")
        except Exception as error:
            result: Future[None] = Future()
            result.set_exception(error)
            return result

    def run(self, control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Repeat complete graphs and propagate pause and cancellation through every nested level.

        Args:
            control (Control): Enclosing scheduler requests and progress sink.
            checkpoint (dict[str, object] | None): Saved round and optional partially completed child graph.

        Returns:
            Outcome: Finite completion or a checkpoint at the current feedback position.

        Raises:
            ValueError: Checkpoint identity or child resource allocation changed.
            InterruptedError: Parent cancellation was requested.
        """
        if checkpoint is not None and (checkpoint.get("fingerprint") != self.work.fingerprint or checkpoint.get("rounds") != self.rounds):
            raise ValueError("feedback checkpoint identity changed")
        iteration = int(str(checkpoint["round"])) if checkpoint else 0
        inner = cast("dict[str, object] | None", checkpoint.get("inner")) if checkpoint else None
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.diagrams:
            (self.directory / "feedback.mmd").write_text(
                'flowchart LR\n    round["Child graph round"] --> boundary["Checkpoint / stop boundary"]\n'
                '    boundary -. "next round" .-> round\n'
            )

        def emit(event: str) -> None:
            """
            Record each graph-level activation and completion.

            Args:
                event (str): Feedback transition.

            Returns:
                None: Event is appended with its round and timestamp.
            """
            record = {"event": event, "round": iteration, "epoch": time.time(), "work": self.work.name}
            with (self.directory / "feedback.jsonl").open("a") as stream:
                stream.write(json.dumps(record) + "\n")
            self.notify(f"[balance] {self.work.name}: {event} round={iteration}")

        def report(statistics: Statistics) -> None:
            """
            Aggregate repeated graph progress without resetting completed rounds.

            Args:
                statistics (Statistics): Latest inner-graph progress.

            Returns:
                None: Parent receives cumulative rounds and configured pause costs.
            """
            remaining = statistics.estimate.remaining_seconds
            control.report(Statistics(iteration, self.rounds, replace(self.work.statistics.estimate, remaining_seconds=remaining)))

        while iteration < self.rounds:
            if control.cancel.is_set():
                raise InterruptedError("feedback graph cancelled")
            if inner is None and self.stop_when is not None and self.stop_when(iteration):
                emit("feedback_condition_met")
                return Outcome()
            if control.pause.is_set():
                emit("feedback_paused")
                return Outcome({"fingerprint": self.work.fingerprint, "rounds": self.rounds, "round": iteration, "inner": inner})
            graph = self.factory(iteration)
            self.current_graph = graph
            if graph.work.slots > self.work.slots or graph.work.memory_bytes > self.work.memory_bytes:
                raise ValueError("feedback child exceeds parent allocation")
            emit("round_started")
            outcome = graph.run(Control(control.pause, control.cancel, report), inner)
            if outcome.checkpoint is not None:
                inner = outcome.checkpoint
                continue
            emit("round_completed")
            iteration += 1
            inner = None
            control.report(Statistics(iteration, self.rounds, self.work.statistics.estimate))
            # Cancellation is interruptible; pause is inspected immediately after this short bounded interval.
            if iteration < self.rounds:
                deadline = time.monotonic() + self.interval_seconds
                while not control.pause.is_set() and not control.cancel.is_set() and time.monotonic() < deadline:
                    control.cancel.wait(min(0.02, max(0, deadline - time.monotonic())))
        emit("feedback_round_limit_reached")
        return Outcome()
