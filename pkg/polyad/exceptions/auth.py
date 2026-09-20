"""
Define shared credential-lane admission failures.
"""

from __future__ import annotations

__all__ = ("LaneFull",)


class LaneFull(Exception):
    """
    Reject a request before dispatch when its shared lane has no remaining budget.
    """

    def __init__(self, retry_after: int) -> None:
        """
        Carry a bounded retry hint without identifying secret credentials.

        Args:
            retry_after (int): Seconds before the caller should attempt admission again.
        """
        super().__init__("credential lane capacity exhausted")
        self.retry_after = retry_after
