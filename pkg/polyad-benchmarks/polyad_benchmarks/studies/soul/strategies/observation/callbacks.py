"""
Exercise CallbackStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk import CallbackStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy
    from polyad_sdk import Change, Environment

__all__ = ("build",)


def build(policy: Policy) -> CallbackStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        CallbackStrategy: Configured component constructed before service startup.
    """

    def observe(change: Change, current: Environment) -> None:
        """
        Count meaningful deltas delivered through the observation pipeline.

        Args:
            change (Change): Baseline or differences since the previous delivery.
            current (Environment): Current observations with freshness applied.

        Returns:
            None: Update counters without starting application work.
        """
        policy.coverage["CallbackStrategy"] += 1
        policy.coverage["ObserveStrategy"] += 1
        policy.changes += len(change.deltas)

    return CallbackStrategy(observe)
