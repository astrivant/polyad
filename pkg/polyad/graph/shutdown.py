"""
Define observable shutdown conditions and a cooperative termination grace period.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ShutdownState:
    """
    Describe coordinator-observed progress for shutdown decisions.

    Attributes:
        elapsed_seconds (float): Time since this scheduler activation began.
        completed_operations (int): Child operations that completed normally.
        total_operations (int): Current graph membership, including dynamic additions.
        progress_units (int): Sum of child-reported cumulative completed units.
    """

    elapsed_seconds: float
    completed_operations: int
    total_operations: int
    progress_units: int


@dataclass(frozen=True)
class Finalizer:
    """
    Require an idempotent cleanup acknowledgement before releasing a graph boundary.

    Attributes:
        name (str): Stable cleanup identity in the termination journal.
        finish (Callable[[ShutdownState], bool]): Return True only when cleanup has completed durably.
    """

    name: str
    finish: Callable[[ShutdownState], bool]


@dataclass(frozen=True)
class ShutdownContract:
    """
    Stop admitting work when a condition holds, then checkpoint or cancel cooperatively.

    Attributes:
        when (Callable[[ShutdownState], bool] | None): Application-owned observable stop condition.
        after_seconds (float | None): Optional activation runtime limit.
        grace_seconds (float): Time allowed for active work to finish or checkpoint before cancellation.
        checkpoint (bool): Ask resumable units to checkpoint during the grace period.
        finalizers (tuple[Finalizer, ...]): Cleanup gates that must acknowledge completion before the boundary releases.
        finalizer_retry_seconds (float): Delay between pending cleanup attempts.
    """

    when: Callable[[ShutdownState], bool] | None = None
    after_seconds: float | None = None
    grace_seconds: float = 30
    checkpoint: bool = True
    finalizers: tuple[Finalizer, ...] = ()
    finalizer_retry_seconds: float = 1

    def __post_init__(self) -> None:
        """
        Validate grace and runtime bounds.

        Returns:
            None: Invalid numeric bounds raise before scheduling starts.
        """
        for value in (self.after_seconds, self.grace_seconds, self.finalizer_retry_seconds):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("shutdown durations must be finite and nonnegative")
        names = [finalizer.name for finalizer in self.finalizers]
        if len(names) != len(set(names)) or any(not name for name in names):
            raise ValueError("finalizers require unique nonempty names")

    def reason(self, state: ShutdownState, requested: bool = False) -> str | None:
        """
        Evaluate manual, runtime and application conditions deterministically.

        Args:
            state (ShutdownState): Current coordinator observation.
            requested (bool): Explicit operator shutdown request.

        Returns:
            str | None: First satisfied condition, or None while work should continue.
        """
        if requested:
            return "operator request"
        if self.after_seconds is not None and state.elapsed_seconds >= self.after_seconds:
            return "runtime limit reached"
        if self.when is not None and self.when(state):
            return "application shutdown condition met"
        return None
