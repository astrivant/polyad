"""
Define control-flow signals that wait for refreshed lifecycle observations.
"""

from __future__ import annotations

__all__ = ("Pending",)


class Pending(Exception):
    """
    Require a later refreshed observation before proceeding.
    """

    def __init__(self, message: str, *, phase: str = "Reconciling") -> None:
        """
        Carry the lifecycle phase while waiting for another observation.

        Args:
            message (str): Explanation of the blocking observation.
            phase (str): Graph phase to publish while the attempt remains pending.
        """
        super().__init__(message)
        self.phase = phase
