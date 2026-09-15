"""
Run a small heartbeat pipeline and export each dependency-graph rewrite.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from polyad.balance import BreadthFirst, Scheduler
from polyad.graph import Outcome, Rewrite, ShutdownContract, Statistics, Work
from polyad.graph.gates import AND, NOT, OR, Signal

if TYPE_CHECKING:
    from collections.abc import Callable

    from polyad.graph import Control


@dataclass
class Heartbeat:
    """
    Do no useful computation while reporting one healthy tick each second.

    Attributes:
        work (Work): Identity and prerequisites.
        after_tick (Callable[[], None] | None): Optional rewrite callback after the heartbeat.
    """

    work: Work
    after_tick: Callable[[], None] | None = None

    def run(self, control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Wait cooperatively for one second and publish one healthy tick.

        Args:
            control (Control): Cancellation and progress channel.
            checkpoint (dict[str, object] | None): Unused because this example finishes each tick.

        Returns:
            Outcome: Completion after reporting health.
        """
        if control.cancel.wait(1):
            raise InterruptedError("heartbeat cancelled")
        print(f"{self.work.name}: healthy (1s)", flush=True)
        control.report(Statistics(1, 1))
        if self.after_tick is not None:
            self.after_tick()
        return Outcome()


def main() -> int:
    """
    Run the example in a fresh output directory.

    Returns:
        int: Zero after the pipeline completes.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(f".cache/balance/example-{time.time_ns()}"))
    options = parser.parse_args()
    healthy = Event()
    scheduler: Scheduler

    def rewrite() -> None:
        """
        Expand a chain to a fork-join graph while its root is active.

        Returns:
            None: Each acknowledged rewrite has a plot and an event.
        """
        healthy.set()
        scheduler.rewrites.register(
            "expand",
            Rewrite(
                additions=(
                    Heartbeat(Work("left", "v1", requires=("root",))),
                    Heartbeat(Work("right", "v1", requires=("root",))),
                    Heartbeat(Work("join", "v1", requires=("left",))),
                    Heartbeat(Work("disabled", "v1", requires=("root",))),
                )
            ),
        )
        scheduler.rewrites.register("join-branches", Rewrite(links=(("join", ("left", "right")),)))
        scheduler.rewrite("expand").result(timeout=30)
        scheduler.rewrite("join-branches").result(timeout=30)

    scheduler = Scheduler(
        [Heartbeat(Work("root", "v1"), rewrite)],
        slots=2,
        directory=options.output,
        policy=BreadthFirst(),
        diagrams=True,
        plots=True,
        routes={
            "left": AND(Signal("healthy"), NOT(Signal("maintenance"))),
            "right": OR(Signal("healthy"), Signal("override")),
            "disabled": Signal("maintenance"),
        },
        facts=lambda: {"healthy": healthy.is_set(), "maintenance": False, "override": False},
        shutdown=ShutdownContract(after_seconds=30, grace_seconds=2),
    )
    print(scheduler.run())
    print(f"Final shape hash: {scheduler.shape_hash}")
    print(f"Plots and rewrite journal: {options.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
