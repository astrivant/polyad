"""
Exercise ThresholdStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk import ThresholdStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy
    from polyad_sdk import Change, Environment


def build(policy: Policy) -> ThresholdStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        ThresholdStrategy: Configured component constructed before service startup.
    """

    def propose(profile: str, change: Change, current: Environment) -> None:
        """
        Select batching under load while preserving memory-pressure precedence.

        Args:
            profile (str): Profile chosen by the SDK hysteresis thresholds.
            change (Change): Evidence triggering the proposal.
            current (Environment): Current observed environment.

        Returns:
            None: The service loop applies this intent only after guard checks.
        """
        policy.coverage["ThresholdStrategy"] += 1
        policy.proposal = "compact" if policy.read()["memoryReserved"] else profile

    return ThresholdStrategy(
        "backlog",
        low=2,
        high=8,
        idle="interactive",
        busy="batch",
        active=lambda: "batch" if policy.read()["profile"] == "batch" else "interactive",
        propose=propose,
    )
