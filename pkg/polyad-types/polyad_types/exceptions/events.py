"""
Define shared event validation failures for producers and consumers.
"""

from __future__ import annotations

__all__ = ("EventTooLarge",)


class EventTooLarge(ValueError):
    """
    Reject an event that exceeds the selected byte budget without truncating its payload.
    """
