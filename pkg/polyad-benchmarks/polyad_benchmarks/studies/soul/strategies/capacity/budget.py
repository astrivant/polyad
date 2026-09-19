"""
Exercise ResourceBudgetStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk import ResourceBudgetStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy


def build(policy: Policy) -> ResourceBudgetStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        ResourceBudgetStrategy: Configured component constructed before service startup.
    """
    return ResourceBudgetStrategy("workers", policy.remember, metric="projectedWorkers", maximum=4)
