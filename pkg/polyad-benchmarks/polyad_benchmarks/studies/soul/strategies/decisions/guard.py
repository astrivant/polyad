"""
Exercise DecisionGuardStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk import DecisionGuardStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy


def build(policy: Policy) -> DecisionGuardStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        DecisionGuardStrategy: Configured component constructed before service startup.
    """
    return DecisionGuardStrategy("decision", policy.remember, allowed=("Applied",))
