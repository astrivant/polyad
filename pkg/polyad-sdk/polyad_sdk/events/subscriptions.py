"""
Dispatch resumable observations to explicit application callbacks.
"""

from __future__ import annotations

import hashlib
import random
import re
import ssl
from collections import OrderedDict
from threading import Event as StopEvent
from typing import TYPE_CHECKING

from polyad_sdk.transport.http import APIError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import Literal

    from polyad_sdk.events.filters import Filter
    from polyad_sdk.events.source import EventSource
    from polyad_types.events.envelope import Event

__all__ = (
    "StreamInterrupted",
    "Subscription",
)


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


class Subscription:
    """
    Run ordered hooks on the caller's thread with explicit checkpoint and retry control.
    """

    def __init__(
        self,
        client: EventSource,
        *,
        cluster: str | None = None,
        cursor: str | None = None,
        history: int = 1024,
        transport: Literal["sse", "websocket"] = "sse",
        rebalance: bool = False,
        heartbeats: bool = False,
    ) -> None:
        """
        Bind one authorized stream and a bounded in-process callback replay history.

        Args:
            client (EventSource): Client configured for the event service.
            cluster (str | None): Registered cluster stream; each stream needs its own subscription.
            cursor (str | None): Last completely handled event ID, restored by the application.
            history (int): Maximum remembered event-handler successes; not durable exactly-once delivery.
            transport (Literal['sse', 'websocket']): Operator event transport, retaining the same callbacks and cursors.
            rebalance (bool): Discover authoritative membership and automatically resume after bounded transport resets.
            heartbeats (bool): Emit SSE heartbeats for application refresh scheduling.
        """
        if type(history) is not int or not 1 <= history <= 65536:
            raise ValueError("subscription history must be an integer from 1 through 65536")
        if transport not in {"sse", "websocket"}:
            raise ValueError("event transport must be sse or websocket")
        if type(heartbeats) is not bool:
            raise ValueError("heartbeats must be a boolean")
        self.heartbeats = heartbeats
        self.transport = transport
        self.client, self.cluster, self.cursor, self.history = client, cluster, cursor, history
        self._hooks: list[tuple[Filter, Callable[[Event], None]]] = []
        self._handled: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._running = False
        if type(rebalance) is not bool:
            raise ValueError("rebalance must be a boolean")
        self.rebalance = rebalance
        self._stopped = StopEvent()
        self._rotation = random.randrange(2**32)

    def on(self, match: Filter, callback: Callable[[Event], None]) -> Subscription:
        """
        Register a callback before starting the stream.

        Args:
            match (Filter): Application-owned predicate over authorized observations.
            callback (Callable[[Event], None]): Synchronous handler; exceptions stop consumption without advancing the cursor.

        Returns:
            Subscription: This subscription, allowing chained registrations.
        """
        if self._running:
            raise RuntimeError("register hooks before running the subscription")
        self._hooks.append((match, callback))
        return self

    def dispatch(self, event: Event) -> None:
        """
        Invoke matching hooks and checkpoint only after all handlers succeed.

        Args:
            event (Event): One decoded observation; controls interrupt consumption.

        Returns:
            None: Replayed successful hooks are skipped while retained in this instance's history.
        """
        if event.event in {"reset", "unavailable", "copulse"}:
            raise StreamInterrupted(event)

        # Replay may revisit callbacks completed before a different callback
        # failed. Deduplicate each event/hook pair, not the whole event at once.
        for index, (match, callback) in enumerate(self._hooks):
            identity = event.id, index
            if (event.id and identity in self._handled) or not match(event):
                continue
            callback(event)
            if event.id:
                self._handled[identity] = None
                while len(self._handled) > self.history:
                    self._handled.popitem(last=False)
        if event.id:
            self.cursor = event.id

    def run(self) -> None:
        """
        Consume with natural callback backpressure and close the stream on errors or cancellation.

        Returns:
            None: Ordinary EOF returns; managed subscriptions reconnect until stopped. Callback and replay failures always propagate.
        """
        if self._running:
            raise RuntimeError("a subscription cannot run concurrently")
        self._running = True
        stream: Iterator[Event] | None = None
        try:
            if self.rebalance:
                self._resume()
                return
            stream = self.client.events(
                last_event_id=self.cursor,
                cluster=self.cluster,
                transport=self.transport,
                stop_event=self._stopped,
                heartbeats=self.heartbeats,
            )
            for event in stream:
                if self._stopped.is_set():
                    break
                self.dispatch(event)
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
            self._running = False

    def stop(self) -> None:
        """
        Stop automatic reconnection and interrupt any reconnect delay.

        Returns:
            None: An active read finishes within the client's configured transport timeout.
        """
        self._stopped.set()

    def _retryable(self, error: Exception) -> bool:
        if isinstance(error, ssl.SSLCertVerificationError) or isinstance(getattr(error, "reason", None), ssl.SSLCertVerificationError):
            return False
        if isinstance(error, APIError):
            return error.status in {429, 502, 503, 504}
        if isinstance(error, OSError):
            return True
        if self.transport == "websocket":
            from websockets.exceptions import ConnectionClosedError

            return isinstance(error, ConnectionClosedError) and all(
                close is None or close.code not in {1008, 1009} for close in (error.sent, error.rcvd)
            )
        return False

    def _resume(self) -> None:
        from polyad_sdk.transport.routing import addresses

        # Rotate replicas while resuming the committed cursor. Jittered backoff
        # spreads reconnect attempts across subscribers after an outage.
        backoff = 1.0
        while not self._stopped.is_set():
            stream: Iterator[Event] | None = None
            delay = random.uniform(backoff / 2, backoff)
            try:
                try:
                    directory = self.client.event_endpoints(cluster=self.cluster)
                    if directory.get("replicas") == 0:
                        raise APIError(503, {"error": "no ready event replicas"})
                    targets = addresses(directory)
                    if self.cursor is None:
                        cursor = directory.get("cursor")
                        if not isinstance(cursor, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,19})-(?:0|[1-9][0-9]{0,19})", cursor):
                            raise ValueError("event discovery requires an initial replay cursor")
                        self.cursor = cursor
                    target = targets[self._rotation % len(targets)] if targets else None
                    self._rotation += 1
                    stream = self.client.events(
                        last_event_id=self.cursor,
                        cluster=self.cluster,
                        transport=self.transport,
                        endpoint=target,
                        stop_event=self._stopped,
                        heartbeats=self.heartbeats,
                    )
                except Exception as error:
                    if not self._retryable(error):
                        raise
                if stream is not None:
                    while not self._stopped.is_set():
                        try:
                            event = next(stream)
                        except StopIteration:
                            break
                        except Exception as error:
                            if not self._retryable(error):
                                raise
                            break
                        if event.event == "copulse":
                            event.typed()  # Reject malformed controls before using their delay.
                            delay = max(0.1, float(event.data["retryAfterSeconds"]))
                            break
                        if event.event == "unavailable":
                            break

                        # Callback exceptions and expired replay always escape, even if they resemble transport errors.
                        self.dispatch(event)
                        backoff = 1.0
            finally:
                close = getattr(stream, "close", None)
                if close:
                    close()
            self._stopped.wait(delay)
            backoff = min(30, backoff * 2)

    def request_id(self, event: Event, action: str) -> str:
        """
        Derive a stable mutation ID for retrying one hook action after event replay.

        Args:
            event (Event): Event with a durable stream ID.
            action (str): Application-chosen action identity, including peer identity where needed.

        Returns:
            str: Portable request ID; applications must keep the same payload when replaying it.
        """
        if not event.id or not action:
            raise ValueError("stable action IDs require an event cursor and action identity")

        # Stable scoped IDs let an event-driven action retry without creating a
        # distinct request merely because delivery replayed.
        value = "\0".join((self.client.url, self.cluster or "", event.id, action))
        return "event-" + hashlib.sha256(value.encode()).hexdigest()
