"""
Exercise PeerAvailabilityStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk import PeerAvailabilityStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy


def build(policy: Policy) -> PeerAvailabilityStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        PeerAvailabilityStrategy: Configured component constructed before service startup.
    """
    return PeerAvailabilityStrategy("peer", policy.remember, usable=lambda _: bool(policy.read()["healthy"]))
