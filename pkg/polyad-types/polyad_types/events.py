"""
Represent public events and configurable transport budgets without importing the operator.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

from attrs import field, frozen

if TYPE_CHECKING:
    from typing import Literal

    from polyad_types.event_models import EventAST

DEFAULT_MAX_EVENT_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 16 * 1024 * 1024


def validate_event_limit(value: int) -> int:
    """
    Reject unbounded, fractional or boolean receive budgets.

    Args:
        value (int): Maximum UTF-8 bytes in one complete serialized event.

    Returns:
        int: Validated budget between 1024 bytes and 16 MiB.
    """
    if type(value) is not int or not 1024 <= value <= MAX_EVENT_BYTES:
        raise ValueError("max event bytes must be an integer from 1024 through 16777216")
    return value


@frozen
class EventStreamSettings:
    """
    Bound event size and each replay read independently of the subscriber count.

    Attributes:
        maxEventBytes (int): Maximum complete SSE record or WebSocket JSON frame in UTF-8 bytes.
        readBatchSize (int): Maximum observations retrieved per subscriber read.
        pollIntervalSeconds (float): Delay before another empty-stream read; also bounds idle heartbeat cadence.
    """

    maxEventBytes: int = field(default=DEFAULT_MAX_EVENT_BYTES, metadata={"schema": {"minimum": 1024, "maximum": MAX_EVENT_BYTES}})
    readBatchSize: int = field(default=64, metadata={"schema": {"minimum": 1, "maximum": 256}})
    pollIntervalSeconds: float = field(default=1.0, metadata={"schema": {"minimum": 0.05, "maximum": 5}})

    def __attrs_post_init__(self) -> None:
        """
        Keep memory and polling limits finite before connecting to storage.

        Returns:
            None: Invalid settings raise ValueError.
        """
        validate_event_limit(self.maxEventBytes)
        if type(self.readBatchSize) is not int or not 1 <= self.readBatchSize <= 256:
            raise ValueError("event readBatchSize must be an integer from 1 through 256")
        if (
            type(self.pollIntervalSeconds) not in (int, float)
            or not math.isfinite(self.pollIntervalSeconds)
            or not 0.05 <= self.pollIntervalSeconds <= 5
        ):
            raise ValueError("event pollIntervalSeconds must be a finite number from 0.05 through 5")


class EventTooLarge(ValueError):
    """
    Reject an event that exceeds the selected byte budget without truncating its payload.
    """


@frozen
class Event:
    """
    Carry a transport-neutral observation while retaining dictionary-based filters and callbacks.

    Attributes:
        id (str): Stream cursor to persist after processing.
        event (str): Event type, including graph, topology, connection, reset or unavailable.
        data (dict[str, Any]): Observation payload.
    """

    id: str
    event: str
    data: dict[str, Any]

    def typed(self) -> EventAST:
        """
        Validate this envelope and decode its payload into the corresponding event syntax tree.

        Returns:
            EventAST: GraphEvent, TopologyEvent, ConnectionEvent, ControlEvent or HeartbeatEvent.
        """
        from polyad_types.event_codec import decode_event

        return decode_event({"id": self.id, "event": self.event, "data": self.data})

    def encode(self, transport: Literal["sse", "websocket"], max_bytes: int = DEFAULT_MAX_EVENT_BYTES, *, raw: str | None = None) -> str:
        """
        Serialize one bounded record, including its envelope and transport framing.

        Args:
            transport (Literal['sse', 'websocket']): Requested observation transport.
            max_bytes (int): Maximum UTF-8 bytes in the serialized record.
            raw (str | None): Already validated JSON payload, preserving existing SSE serialization.

        Returns:
            str: One complete SSE record or WebSocket text frame.
        """
        validate_event_limit(max_bytes)
        if any(char in self.id + self.event for char in "\r\n\x00"):
            raise ValueError("event cursor and name must fit one field")
        if transport == "websocket":
            value = json.dumps({"id": self.id, "event": self.event, "data": self.data}, separators=(",", ":"), allow_nan=False)
        elif transport == "sse":
            data = raw if raw is not None else json.dumps(self.data, allow_nan=False)
            prefix = f"id: {self.id}\n" if self.id else ""
            value = f"{prefix}event: {self.event}\n" + "".join(f"data: {line}\n" for line in data.splitlines()) + "\n"
        else:
            raise ValueError("event transport must be sse or websocket")
        size = len(value.encode("utf-8"))
        if size > max_bytes:
            raise EventTooLarge(f"event is {size} bytes; maximum is {max_bytes} bytes")
        return value
