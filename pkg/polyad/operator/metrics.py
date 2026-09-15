"""Track local write pressure without doing I/O from health probes."""

from __future__ import annotations

import threading
import time

from attrs import define, field


@define
class WriteBacklog:
    """
    Count concrete API writes awaiting dispatch and awaiting transport completion.

    Attributes:
        queued (dict[int, float]): Write tokens mapped to monotonic enqueue times.
        in_flight (dict[int, float]): Write tokens mapped to monotonic dispatch times.
        sequence (int): Last allocated write token.
        lock (threading.Lock): Lock protecting gauges across operator and probe threads.
    """

    queued: dict[int, float] = field(factory=dict)
    in_flight: dict[int, float] = field(factory=dict)
    sequence: int = 0
    lock: threading.Lock = field(factory=threading.Lock, repr=False)

    def enqueue(self) -> int:
        """
        Register a mutation before waiting for its serialized dispatch slot.

        Returns:
            int: Unique token for subsequent dispatch and completion tracking.
        """
        with self.lock:
            self.sequence += 1
            self.queued[self.sequence] = time.monotonic()
            return self.sequence

    def dispatch(self, token: int) -> None:
        """
        Move an authorized request from waiting to the transport stage.

        Args:
            token (int): Write token returned by enqueue.

        Returns:
            None: No return value.
        """
        with self.lock:
            del self.queued[token]
            self.in_flight[token] = time.monotonic()

    def finish(self, token: int) -> None:
        """
        Release gauges on success, failure, or cancellation.

        Args:
            token (int): Write token returned by enqueue.

        Returns:
            None: No return value.
        """
        with self.lock:
            self.queued.pop(token, None)
            self.in_flight.pop(token, None)

    def snapshot(self) -> dict[str, int | float]:
        """
        Return consistent gauges even when Kopf runs a probe on another thread.

        Returns:
            dict[str, int | float]: Queue counts and oldest request ages in seconds.
        """
        with self.lock:
            now = time.monotonic()
            return {
                "queued": len(self.queued),
                "inFlight": len(self.in_flight),
                "total": len(self.queued) + len(self.in_flight),
                "oldestQueuedSeconds": max((now - started for started in self.queued.values()), default=0.0),
                "oldestInFlightSeconds": max((now - started for started in self.in_flight.values()), default=0.0),
            }
