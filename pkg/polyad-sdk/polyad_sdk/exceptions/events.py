"""
Define SDK event-stream recovery signals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polyad_types.events.envelope import Event

__all__ = ("StreamInterrupted",)


class StreamInterrupted(RuntimeError):
    """
    Require rediscovery or explicit reconnection after a stream control message.
    """

    def __init__(self, event: Event) -> None:
        """
        Retain the control event without treating it as a successful checkpoint.

        Args:
            event (Event): Reset, unavailable or copulse event.
        """
        self.event = event
        super().__init__(f"Polyad event stream requires recovery: {event.event}")
