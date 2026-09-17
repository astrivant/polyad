"""
Represent an observation from Polyad's server-sent event stream.
"""

from __future__ import annotations

from typing import Any

from attrs import frozen


@frozen
class Event:
    """
    Carry an SSE cursor, event type and decoded JSON observation.

    Attributes:
        id (str): Stream cursor to persist after processing.
        event (str): Event type, including graph, topology, connection, reset or unavailable.
        data (dict[str, Any]): Observation payload.
    """

    id: str
    event: str
    data: dict[str, Any]
