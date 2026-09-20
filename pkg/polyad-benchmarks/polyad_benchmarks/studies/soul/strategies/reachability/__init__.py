"""
Exercise ReachabilityStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from polyad_sdk.symbiosis.reachability import Observation, ReachabilityStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy

__all__ = ("build",)


def build(policy: Policy) -> ReachabilityStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        ReachabilityStrategy: Configured component constructed before service startup.
    """
    return ReachabilityStrategy(
        "envelope",
        policy.remember,
        artifact=lambda: policy.envelope,
        observe=lambda _: Observation(
            (float(policy.read()["backlog"] + 1),), (0.0,), time.time(), policy.envelope.revision, policy.envelope.model.fingerprint
        ),
    )
