"""
Represent public events and configurable transport budgets without importing the operator.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any, Literal

from attrs import field, frozen

if TYPE_CHECKING:
    from polyad_types.events.models import EventAST

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
class EventRebalanceSettings:
    """
    Pace operator-requested reconnections independently of graph mutations.

    Attributes:
        enabled (bool): Enable endpoint discovery and rolling connection controls.
        routing (Literal['Service', 'Direct']): Delegate new connections to the Service/mesh or let clients choose Pod IPs.
        refreshSeconds (float): Interval for refreshing ready operator membership.
        automatic (bool): Roll existing subscribers when ready membership changes.
        batchPercent (int): Maximum percentage of the initial local subscribers scheduled per batch.
        intervalSeconds (float): Delay between batches, also used to spread their reconnects.
        cooldownSeconds (float): Minimum interval between ordinary rolls on each replica.
        drainSeconds (float): Time reserved before process termination to evacuate subscriptions.
        maxConnectionSeconds (float): Optional maximum stream age; zero disables periodic rotation.
    """

    enabled: bool = False
    routing: Literal["Service", "Direct"] = "Service"
    refreshSeconds: float = field(default=5, metadata={"schema": {"minimum": 1, "maximum": 60}})
    automatic: bool = True
    batchPercent: int = field(default=10, metadata={"schema": {"minimum": 1, "maximum": 100}})
    intervalSeconds: float = field(default=2, metadata={"schema": {"minimum": 0.1, "maximum": 30}})
    cooldownSeconds: float = field(default=60, metadata={"schema": {"minimum": 1, "maximum": 3600}})
    drainSeconds: float = field(default=20, metadata={"schema": {"minimum": 5, "maximum": 300}})
    maxConnectionSeconds: float = field(default=0, metadata={"schema": {"minimum": 0, "maximum": 86400}})

    def __attrs_post_init__(self) -> None:
        """
        Reject unsafe pacing values before starting listeners.

        Returns:
            None: Invalid settings raise ValueError.
        """
        if type(self.enabled) is not bool or type(self.automatic) is not bool or self.routing not in {"Service", "Direct"}:
            raise ValueError("rebalance requires boolean switches and Service or Direct routing")
        if type(self.batchPercent) is not int or not 1 <= self.batchPercent <= 100:
            raise ValueError("rebalance batchPercent must be an integer from 1 through 100")
        for name, minimum, maximum in (
            ("refreshSeconds", 1, 60),
            ("intervalSeconds", 0.1, 30),
            ("cooldownSeconds", 1, 3600),
            ("drainSeconds", 5, 300),
            ("maxConnectionSeconds", 0, 86400),
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(f"rebalance {name} must be finite and between {minimum} and {maximum}")
        if 0 < self.maxConnectionSeconds < self.cooldownSeconds:
            raise ValueError("maxConnectionSeconds must be zero or at least cooldownSeconds")


@frozen
class Event:
    """
    Carry a transport-neutral observation while retaining dictionary-based filters and callbacks.

    Attributes:
        id (str): Stream cursor to persist after processing.
        event (str): Event type, including graph, topology, connection, reset, unavailable or copulse.
        data (dict[str, Any]): Observation payload.
    """

    id: str
    event: str
    data: dict[str, Any]

    def typed(self) -> EventAST:
        """
        Validate this envelope and decode its payload into the corresponding event syntax tree.

        Returns:
            EventAST: GraphEvent, TopologyEvent, ConnectionEvent, ControlEvent, CopulseEvent or HeartbeatEvent.
        """
        from polyad_types.events.codec import decode_event

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
