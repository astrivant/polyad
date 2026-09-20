"""
Exercise DecisionStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk import DecisionStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy
    from polyad_sdk import Change, Environment

__all__ = ("build",)


def build(policy: Policy) -> DecisionStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        DecisionStrategy: Configured component constructed before service startup.
    """

    def remember(change: Change, current: Environment) -> None:
        """
        Distinguish the parent's recommendation from its committed decision.

        Args:
            change (Change): Triggering public decision change.
            current (Environment): Current decision with expiry applied.

        Returns:
            None: Retain the observed phase for the mutation loop.
        """
        policy.coverage["DecisionStrategy"] += 1
        policy.last_decision = str(current.decision["phase"]) if current.decision else None

    return DecisionStrategy(remember)
