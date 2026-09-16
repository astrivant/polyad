"""
Share per-key request rates and expiring concurrency leases across operator replicas.
"""

from __future__ import annotations

import hashlib
import time
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

if TYPE_CHECKING:
    from polyad_types.auth import APIKey

LEASE_SECONDS = 120
ACQUIRE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1])
local window = math.floor(now / 60)
local count = 0
if tonumber(redis.call('HGET', KEYS[1], 'window')) == window then
  count = tonumber(redis.call('HGET', KEYS[1], 'count')) or 0
end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if count >= tonumber(ARGV[1]) then return {0, 60 - now % 60} end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return {0, 1} end
redis.call('HSET', KEYS[1], 'window', window, 'count', count + 1)
redis.call('EXPIRE', KEYS[1], 120)
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[4]), ARGV[3])
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4]) * 2)
return {1, 0}
"""
RENEW = """
local now = tonumber(redis.call('TIME')[1])
local deadline = tonumber(redis.call('ZSCORE', KEYS[1], ARGV[1]))
if not deadline or deadline <= now then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]) * 2)
return 1
"""


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


class Permit:
    """
    Track one renewable in-flight request without retaining a bearer token.
    """

    def __init__(self, lane: str) -> None:
        """
        Create an unpredictable membership identity for a concurrency lease.

        Args:
            lane (str): Redis address derived from namespace, group and key name.
        """
        self.lane, self.identity = lane, str(uuid4())
        self.deadline = time.monotonic() + LEASE_SECONDS
        self.lost = Event()

    def check(self) -> None:
        """
        Stop streaming or outbound reads once shared concurrency ownership is uncertain.

        Returns:
            None: Raises before continuing work after lease expiry or renewal failure.
        """
        if self.lost.is_set() or time.monotonic() >= self.deadline:
            raise RuntimeError("credential lane lease unavailable")


class Lanes:
    """
    Own a bounded Redis transport and one renewer for all local request permits.
    """

    def __init__(self, url: str, namespace: str) -> None:
        """
        Use shared storage without local quota fallback or automatic write retries.

        Args:
            url (str): Redis-compatible shared cache URL.
            namespace (str): Root namespace shared by the HA operator replicas.
        """
        self.namespace = namespace
        self.client = Redis.from_url(
            url,
            socket_connect_timeout=2,
            socket_timeout=2,
            max_connections=32,
            retry=Retry(NoBackoff(), 0),
        )
        self.lock = Lock()
        self.active: dict[str, Permit] = {}
        self.stopping = Event()
        self.thread = Thread(target=self.renew, name="polyad-credential-lanes", daemon=True)
        self.thread.start()

    def acquire(self, group: str, key: APIKey) -> Permit:
        """
        Atomically reserve both budgets; bidirectional traffic uses one combined lane.

        Args:
            group (str): Services or operators credential category.
            key (APIKey): Stable identity and limits, independent of the current token value.

        Returns:
            Permit: A tracked concurrency permit, released after response consumption.
        """
        digest = hashlib.sha256(f"{self.namespace}/{group}/{key.name}".encode()).hexdigest()
        permit = Permit(f"polyad:auth:{{{digest}}}")
        result = cast(
            "list[int]",
            self.client.eval(
                ACQUIRE,
                2,
                permit.lane + ":rate",
                permit.lane + ":active",
                str(key.requestsPerMinute),
                str(key.maxConcurrentRequests),
                permit.identity,
                str(LEASE_SECONDS),
            ),
        )
        if not result[0]:
            raise LaneFull(int(result[1]))
        with self.lock:
            self.active[permit.identity] = permit
        return permit

    def release(self, permit: Permit) -> None:
        """
        Release concurrency once; a cache failure leaves only an expiring lease.

        Args:
            permit (Permit): Previously admitted request.

        Returns:
            None: Rate consumption remains charged through the fixed window.
        """
        with self.lock:
            if self.active.pop(permit.identity, None) is None:
                return
            permit.lost.set()
        try:
            self.client.zrem(permit.lane + ":active", permit.identity)
        except Exception:
            pass  # Expiration reclaims abandoned permits; never mask a completed request.

    def renew(self) -> None:
        """
        Renew live streams and mark permits unusable when shared ownership is lost.

        Returns:
            None: Runs until the owning server shuts down.
        """
        while not self.stopping.wait(10):
            with self.lock:
                active = list(self.active.values())
            started = time.monotonic()
            try:
                with self.client.pipeline(transaction=False) as pipeline:
                    for permit in active:
                        pipeline.eval(RENEW, 1, permit.lane + ":active", permit.identity, str(LEASE_SECONDS))
                    results = pipeline.execute()
                for permit, renewed in zip(active, results, strict=True):
                    if renewed:
                        permit.deadline = started + LEASE_SECONDS
                    else:
                        permit.lost.set()
            except Exception:
                for permit in active:
                    permit.lost.set()

    def close(self) -> None:
        """
        Join the renewer and release remaining permits before closing its transport.

        Returns:
            None: No active permit is renewed after shutdown.
        """
        self.stopping.set()
        self.thread.join()
        with self.lock:
            active = list(self.active.values())
        for permit in active:
            self.release(permit)
        self.client.close()
