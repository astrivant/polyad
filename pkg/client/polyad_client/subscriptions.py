"""
Dispatch resumable observations to explicit application callbacks.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from polyad_client.client import Client
    from polyad_client.filters import Filter
    from polyad_types.events import Event


class StreamInterrupted(RuntimeError):
    """
    Require rediscovery or explicit reconnection after a stream control message.
    """

    def __init__(self, event: Event) -> None:
        """
        Retain the control event without treating it as a successful checkpoint.

        Args:
            event (Event): Reset or unavailable event.
        """
        self.event = event
        super().__init__(f"Polyad event stream requires recovery: {event.event}")


class Subscription:
    """
    Run ordered hooks on the caller's thread with explicit checkpoint and retry control.
    """

    def __init__(self, client: Client, *, cluster: str | None = None, cursor: str | None = None, history: int = 1024) -> None:
        """
        Bind one authorized stream and a bounded in-process callback replay history.

        Args:
            client (Client): Client configured for the event service.
            cluster (str | None): Registered cluster stream; each stream needs its own subscription.
            cursor (str | None): Last completely handled event ID, restored by the application.
            history (int): Maximum remembered event-handler successes; not durable exactly-once delivery.
        """
        if type(history) is not int or not 1 <= history <= 65536:
            raise ValueError("subscription history must be an integer from 1 through 65536")
        self.client, self.cluster, self.cursor, self.history = client, cluster, cursor, history
        self._hooks: list[tuple[Filter, Callable[[Event], None]]] = []
        self._handled: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._running = False

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
        if event.event in {"reset", "unavailable"}:
            raise StreamInterrupted(event)
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
            None: EOF returns; timeouts, callback failures and reset controls propagate to the application.
        """
        if self._running:
            raise RuntimeError("a subscription cannot run concurrently")
        self._running = True
        stream: Iterator[Event] | None = None
        try:
            stream = self.client.events(last_event_id=self.cursor, cluster=self.cluster)
            for event in stream:
                self.dispatch(event)
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
            self._running = False

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
        value = "\0".join((self.client.url, self.cluster or "", event.id, action))
        return "event-" + hashlib.sha256(value.encode()).hexdigest()
