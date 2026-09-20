"""
Exercise ConnectionPermissionStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk import ConnectionPermissionStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy

__all__ = ("build",)


def build(policy: Policy) -> ConnectionPermissionStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        ConnectionPermissionStrategy: Configured component constructed before service startup.
    """
    return ConnectionPermissionStrategy("permission", policy.remember, receipt=lambda: policy.receipt_uid)
