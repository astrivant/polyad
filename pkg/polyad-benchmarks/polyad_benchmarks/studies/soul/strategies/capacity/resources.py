"""
Exercise ResourceStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk import ResourceStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy
    from polyad_sdk import Change, Environment


def build(policy: Policy) -> ResourceStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        ResourceStrategy: Configured component constructed before service startup.
    """

    def pressure(change: Change, current: Environment) -> None:
        """
        Request a smaller worker implementation under reservation or allocation pressure.

        Args:
            change (Change): Triggering resource delta.
            current (Environment): Current graph and local resource observation.

        Returns:
            None: Record intent; the supervisor checks overlap before committing.
        """
        policy.coverage["ResourceStrategy"] += 1
        if current.available and (policy.read()["memoryReserved"] or policy.read().get("resourcePressure", False)):
            policy.proposal = "compact"

    return ResourceStrategy(pressure)
