"""
Exercise FreshnessStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk import FreshnessStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy


def build(policy: Policy) -> FreshnessStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        FreshnessStrategy: Configured component constructed before service startup.
    """
    return FreshnessStrategy("fresh", policy.remember)
