"""
Host bounded event subscribers separately from composition HTTP workers.
"""

from __future__ import annotations

import asyncio
from threading import Event, Thread
from typing import TYPE_CHECKING, TypeVar

from flask import abort, jsonify, request
from waitress import wasyncore
from waitress.server import create_server

from polyad.api.limits import RateLimitPolicy
from polyad.events.builder import EventAPIBuilder
from polyad.operator.health import lifecycle
from polyad.operator.pressure import pressure

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

    from flask import Response

    from polyad.events.store import EventStore


T = TypeVar("T")


class EventServer:
    """
    Bridge dedicated streaming workers to the operator's asynchronous shared cache.
    """

    def __init__(
        self,
        store: EventStore,
        namespace: str,
        token: str,
        *,
        port: int = 8091,
        connections: int = 16,
        clusters: dict[str, EventStore] | None = None,
    ) -> None:
        """
        Start the event listener with independent workers and subscriber credentials.

        Args:
            store (EventStore): Shared observation stream.
            namespace (str): Namespace served by this operator.
            token (str): Subscriber-only bearer credential.
            port (int): Dedicated HTTP port.
            connections (int): Maximum active streams on this replica.
            clusters (dict[str, EventStore] | None): Registered cluster streams held in root storage.
        """
        self.loop = asyncio.get_running_loop()
        self.stopping = Event()
        policy = RateLimitPolicy.from_environment(namespace + ":events")

        def selected() -> EventStore:
            cluster = request.args.get("cluster")
            if cluster is None:
                return store
            if cluster not in (clusters or {}):
                abort(404, "cluster is not registered")
            assert clusters is not None
            return clusters[cluster]

        app = (
            EventAPIBuilder(stopping=self.stopping, max_connections=connections, limits=policy)
            .with_handlers(lambda cursor: self.invoke(selected().cursor(cursor)), lambda cursor: self.invoke(selected().read(cursor)))
            .with_topology_handler(lambda kind, name, uid, node: self.invoke(selected().topology(kind, name, uid, node)))
            .with_bearer_token(token)
            .build()
        )

        @app.before_request
        def retiring() -> tuple[Response, int] | None:
            if lifecycle.replacement.is_set() or lifecycle.draining.is_set():
                return jsonify(error="replica is retiring; reconnect to a healthy replica"), 503
            return None

        self.limiter = app.extensions["polyad.limiter"]
        self.sockets: wasyncore._SocketMap = {}
        self.server = create_server(
            pressure.wrap(app),
            map=self.sockets,
            host="0.0.0.0",
            port=port,
            threads=connections + 2,
            connection_limit=connections + 8,
            channel_timeout=30,
            max_request_body_size=1024,
            outbuf_high_watermark=65536,
        )
        self.thread = Thread(target=self.run, name="polyad-events-api", daemon=False)
        self.thread.start()

    def invoke(self, operation: Coroutine[Any, Any, T]) -> T:
        """
        Bound each cache operation without blocking the scheduler loop.

        Args:
            operation (Coroutine[Any, Any, T]): Asynchronous observation read.

        Returns:
            T: Read result.
        """
        if self.stopping.is_set():
            operation.close()
            raise RuntimeError("events server is stopping")
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        try:
            return future.result(timeout=7)
        except Exception:
            future.cancel()
            raise

    def run(self) -> None:
        """
        Serve streaming connections until the shutdown signal is set.

        Returns:
            None: No return value.
        """
        try:
            while not self.stopping.is_set():
                wasyncore.loop(timeout=0.5, count=1, map=self.sockets)
        finally:
            wasyncore.close_all(self.sockets)
            self.server.task_dispatcher.shutdown(timeout=10)

    async def close(self) -> None:
        """
        End streams and join workers before closing shared cache connections.

        Returns:
            None: No return value.
        """
        self.stopping.set()
        await asyncio.to_thread(self.thread.join)
        if self.limiter.enabled:
            self.limiter.storage.storage.close()
