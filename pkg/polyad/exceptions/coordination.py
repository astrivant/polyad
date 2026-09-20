"""
Define ownership and cooldown signals for distributed coordination.
"""

from __future__ import annotations

import math

__all__ = ("NotOwner", "PulseDeferred")


class NotOwner(Exception):
    """
    Stop a pass when this replica cannot prove shard ownership.
    """


class PulseDeferred(RuntimeError):
    """
    Retain desired work until its shared cooldown window permits another decision.
    """

    def __init__(self, seconds: float) -> None:
        """
        Report a bounded delay without retaining a mutation payload.

        Args:
            seconds (float): Remaining shared cooldown window.
        """
        self.retry_after = max(1, math.ceil(seconds))
        super().__init__("administrator pulse cooldown is active; retry from fresh state")
