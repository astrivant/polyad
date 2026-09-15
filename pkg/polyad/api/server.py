"""
Run production HTTP intake on a thread while keeping Kubernetes I/O on the operator loop.
"""

from __future__ import annotations

import asyncio
import logging
from threading import BoundedSemaphore, Event, Lock, Thread
from typing import TYPE_CHECKING

from waitress import wasyncore
from waitress.server import create_server

from polyad.api.app import Conflict, Unavailable
from polyad.api.builder import APIBuilder
from polyad.api.limits import RateLimitPolicy
from polyad.api.store import CompositionStore

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from concurrent.futures import Future
    from typing import Any

    from polyad.operator.api import API

logger = logging.getLogger(__name__)


class CompositionServer:
    """
    Bound intake and bridge WSGI workers into the operator's serialized API adapter.
    """

    def __init__(self, api: API, namespace: str, token: str, *, host: str = "0.0.0.0", port: int = 8090) -> None:
        """
        Bind a server without installing signal handlers or starting another event loop.

        Args:
            api (API): Dedicated create-only receipt adapter with its own write backlog.
            namespace (str): Fixed namespace served by this replica.
            token (str): Bearer credential loaded from a Kubernetes Secret.
            host (str): HTTP listening address.
            port (int): HTTP listening port.
        """
        self.api = api
        self.loop = asyncio.get_running_loop()
        self.stopping = Event()
        self.slots = BoundedSemaphore(32)
        self.lock = Lock()
        self.pending: set[Future[dict[str, Any] | None]] = set()
        store = CompositionStore(api, namespace)
        app = (
            APIBuilder()
            .with_handlers(lambda value: self.invoke(store.submit(value)) or {}, lambda key, audit: self.invoke(store.lookup(key, audit)))
            .with_bearer_token(token)
            .with_rate_limits(RateLimitPolicy.from_environment(namespace))
            .build()
        )
        self.limiter = app.extensions["polyad.limiter"]
        self.sockets: wasyncore._SocketMap = {}
        self.server = create_server(
            app, map=self.sockets, host=host, port=port, threads=4, max_request_body_size=1024 * 1024, connection_limit=64
        )
        self.thread = Thread(target=self.run, name="polyad-composition-api", daemon=False)
        self.thread.start()

    def invoke(self, operation: Coroutine[Any, Any, dict[str, Any] | None]) -> dict[str, Any] | None:
        """
        Bound outstanding operations and retain timed-out writes until their acknowledgement.

        Args:
            operation (Coroutine[Any, Any, dict[str, Any] | None]): Receipt submission or audit read.

        Returns:
            dict[str, Any] | None: Durable receipt or fresh lookup result.
        """
        if self.stopping.is_set() or not self.slots.acquire(blocking=False):
            operation.close()
            raise Unavailable("composition intake is stopping or full")
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        with self.lock:
            self.pending.add(future)
        future.add_done_callback(self.finished)
        try:
            return future.result(timeout=30)
        except (Conflict, ValueError, TypeError):
            raise
        except Exception as error:
            logger.warning("Composition API operation failed or timed out: %s", type(error).__name__)
            raise Unavailable("Kubernetes acknowledgement unavailable") from error

    def finished(self, future: Future[dict[str, Any] | None]) -> None:
        """
        Release an intake slot only after the operation has actually finished.

        Args:
            future (Future[dict[str, Any] | None]): Completed event-loop operation.

        Returns:
            None: No return value.
        """
        with self.lock:
            self.pending.discard(future)
        self.slots.release()

    def run(self) -> None:
        """
        Serve until shutdown, then close HTTP workers and sockets.

        Returns:
            None: No return value.
        """
        try:
            while not self.stopping.is_set():
                wasyncore.loop(timeout=0.5, count=1, map=self.sockets)
        finally:
            self.server.task_dispatcher.shutdown(timeout=35)
            wasyncore.close_all(self.sockets)

    async def close(self) -> None:
        """
        Stop HTTP intake and join outstanding operations while the operator loop is alive.

        Returns:
            None: No return value.
        """
        self.stopping.set()
        await asyncio.to_thread(self.thread.join)
        with self.lock:
            pending = list(self.pending)
        await asyncio.gather(*(asyncio.wrap_future(future) for future in pending), return_exceptions=True)
        self.api.client.close()
        if self.limiter.enabled:
            self.limiter.storage.storage.close()
