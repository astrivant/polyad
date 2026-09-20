"""
Exercise TopologyStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk import TopologyStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy
    from polyad_sdk import Change, Environment

__all__ = ("build",)


def build(policy: Policy) -> TopologyStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        TopologyStrategy: Configured component constructed before service startup.
    """

    def remember(change: Change, current: Environment) -> None:
        """
        Withdraw stale neighbors and retain currently available worker names.

        Args:
            change (Change): Triggering topology or connection change.
            current (Environment): Fresh candidate-worker view.

        Returns:
            None: Application admission uses the selected neighbors.
        """
        policy.coverage["TopologyStrategy"] += 1
        policy.route_names = tuple(item["node"]["name"] for item in current.candidates)

    return TopologyStrategy(remember)
