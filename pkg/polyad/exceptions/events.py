"""
Define graph-incarnation and event-replay recovery failures.
"""

from __future__ import annotations

__all__ = ("CursorExpired", "TopologyReplaced")


class TopologyReplaced(ValueError):
    """
    Reject a snapshot belonging to a replacement graph incarnation.
    """


class CursorExpired(ValueError):
    """
    Require a fresh Kubernetes/API snapshot after the bounded replay window expires.
    """
