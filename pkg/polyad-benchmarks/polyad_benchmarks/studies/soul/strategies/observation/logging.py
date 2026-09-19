"""
Exercise ObserveStrategy against real worker state and controlled environmental inputs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from polyad_sdk import ObserveStrategy

if TYPE_CHECKING:
    from polyad_benchmarks.studies.soul.runtime.policy import Policy


def build(policy: Policy) -> ObserveStrategy:
    """
    Bind this strategy to one service's observation and intent state.

    Args:
        policy (Policy): Service-local observations, counters and pending intent.

    Returns:
        ObserveStrategy: Configured component constructed before service startup.
    """
    return ObserveStrategy(logging.getLogger("polyad.study"))
