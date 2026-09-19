"""
Exercise ContainerBudgetStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk import ContainerBudgetStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy


def build(policy: Policy) -> ContainerBudgetStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        ContainerBudgetStrategy: Configured component constructed before service startup.
    """
    return ContainerBudgetStrategy(
        "memory",
        policy.remember,
        resource="memory",
        maximum=4096,
        used=lambda: float(policy.read()["projectedWorkers"] * 512 + policy.read()["memoryReserved"]),
    )
