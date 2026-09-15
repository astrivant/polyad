"""
Describe measurable workloads that can cooperate with checkpoint requests.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from typing import Protocol


@dataclass(frozen=True)
class Estimate:
    """
    Describe remaining execution and pause costs; None means unknown, not zero.

    Attributes:
        remaining_seconds (float | None): Expected remaining execution time.
        uncertainty_seconds (float): Conservative margin used in preemption decisions.
        checkpoint_seconds (float | None): Expected time to reach a safe boundary and save state.
        resume_seconds (float | None): Expected restoration cost.
    """

    remaining_seconds: float | None = None
    uncertainty_seconds: float = 0
    checkpoint_seconds: float | None = None
    resume_seconds: float | None = None

    def __post_init__(self) -> None:
        """
        Reject nonfinite and negative estimates.

        Returns:
            None: Invalid values raise before the record can be used.
        """
        for value in (self.remaining_seconds, self.uncertainty_seconds, self.checkpoint_seconds, self.resume_seconds):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("estimates must be finite and nonnegative")


@dataclass(frozen=True)
class Statistics:
    """
    Report cumulative progress across resumes with optional updated cost estimates.

    Attributes:
        completed (int): Units completed, including those from a previous checkpoint.
        total (int | None): Expected total units, if known.
        estimate (Estimate): Workload-supplied remaining time and checkpoint costs.
    """

    completed: int = 0
    total: int | None = None
    estimate: Estimate = field(default_factory=Estimate)

    def __post_init__(self) -> None:
        """
        Reject inconsistent progress counters.

        Returns:
            None: Invalid values raise before the record can be used.
        """
        if self.completed < 0 or (self.total is not None and self.total < self.completed):
            raise ValueError("progress requires 0 <= completed <= total")


@dataclass(frozen=True)
class Work:
    """
    Declare workload identity, dependencies and reserved resources.

    Attributes:
        name (str): Stable filesystem-safe identifier.
        fingerprint (str): Input and implementation identity used to validate checkpoints.
        requires (tuple[str, ...]): Work that must complete first.
        slots (int): Reserved execution slots, including nested workers.
        memory_bytes (int): Reserved memory, retained until a paused workload returns.
        resumable (bool): Whether the workload can return a durable checkpoint.
        statistics (Statistics): Initial progress and estimates.
    """

    name: str
    fingerprint: str
    requires: tuple[str, ...] = ()
    slots: int = 1
    memory_bytes: int = 0
    resumable: bool = False
    statistics: Statistics = field(default_factory=Statistics)


@dataclass(frozen=True)
class Outcome:
    """
    Distinguish completion from a cooperative pause at a recoverable boundary.

    Attributes:
        checkpoint (dict[str, object] | None): JSON state for resumption, or None for completion.
    """

    checkpoint: dict[str, object] | None = None


@dataclass(frozen=True)
class Control:
    """
    Provide thread-safe cooperative control and progress reporting to a workload.

    Attributes:
        pause (Event): Request to checkpoint at the next safe boundary.
        cancel (Event): Request to stop and clean up all owned children.
        report (Callable[[Statistics], None]): Publish cumulative statistics to the coordinator.
    """

    pause: Event
    cancel: Event
    report: Callable[[Statistics], None]


class Workload(Protocol):
    """
    Require a description and cooperative execution from schedulable work.
    """

    @property
    def work(self) -> Work:
        """
        Return immutable identity, dependencies, resource needs and initial estimates.

        Returns:
            Work: Description used by scheduling and checkpoint verification.
        """
        ...

    def run(self, control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Execute until completion, cancellation or a requested recoverable pause.

        Args:
            control (Control): Progress sink and cooperative requests.
            checkpoint (dict[str, object] | None): Previously verified state, if resuming.

        Returns:
            Outcome: Completion or JSON checkpoint after stopping all workload-owned children.
        """
        ...
