"""
Share bounded HTTP intake and shutdown across domain-specific operator APIs.
"""

from __future__ import annotations

import asyncio
import logging
from threading import BoundedSemaphore, Event, Lock, Thread
from typing import TYPE_CHECKING, TypeVar

from flask import jsonify
from waitress import wasyncore
from waitress.server import create_server

from polyad.api.activations import ActivationStore
from polyad.api.builder import APIBuilder
from polyad.api.connections.app import build_app as connection_app
from polyad.api.connections.store import ConnectionStore
from polyad.api.errors import RequestError, Unavailable
from polyad.api.limits import RateLimitPolicy
from polyad.api.store import CompositionStore
from polyad.operator.health import lifecycle

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from concurrent.futures import Future
    from typing import Any

    from flask import Flask, Response

    from polyad.api.connections.store import ConnectionSettings
    from polyad.operator.api import API

logger = logging.getLogger(__name__)
T = TypeVar("T")


class APIServer:
    """
    Bridge bounded WSGI workers into the operator loop and retain uncertain writes.
    """

    def __init__(self, api: API) -> None:
        """
        Initialize shared transport bookkeeping before assembling a domain application.

        Args:
            api (API): Dedicated intake adapter with its own write backlog.
        """
        self.api = api
        self.loop = asyncio.get_running_loop()
        self.stopping = Event()
        self.slots = BoundedSemaphore(32)
        self.lock = Lock()
        self.pending: set[Future[Any]] = set()

    def start(self, app: Flask, *, host: str, port: int, name: str) -> None:
        """
        Serve an authenticated application with common worker and shutdown limits.

        Args:
            app (Flask): Domain routes, authentication, body limit and shared rate limiter.
            host (str): HTTP listening address.
            port (int): HTTP listening port.
            name (str): Domain name for the listener thread.

        Returns:
            None: HTTP workers begin serving without installing signal handlers.
        """

        @app.before_request
        def retiring() -> tuple[Response, int] | None:
            if lifecycle.replacement.is_set() or lifecycle.draining.is_set():
                return jsonify(error="replica is retiring; reconnect to a healthy replica"), 503
            return None

        self.limiter = app.extensions["polyad.limiter"]
        self.sockets: wasyncore._SocketMap = {}
        self.server = create_server(
            app,
            map=self.sockets,
            host=host,
            port=port,
            threads=4,
            max_request_body_size=app.config["MAX_CONTENT_LENGTH"],
            connection_limit=64,
        )
        self.thread = Thread(target=self.run, name=f"polyad-{name}-api", daemon=False)
        self.thread.start()

    def invoke(self, operation: Coroutine[Any, Any, T]) -> T:
        """
        Bound outstanding operations and retain timed-out writes until acknowledgement.

        Args:
            operation (Coroutine[Any, Any, T]): Domain authentication, receipt submission or audit read.

        Returns:
            T: Acknowledged result, preserving expected client-facing errors.
        """
        if self.stopping.is_set() or not self.slots.acquire(blocking=False):
            operation.close()
            raise Unavailable("API intake is stopping or full")
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        with self.lock:
            self.pending.add(future)
        future.add_done_callback(self.finished)
        try:
            return future.result(timeout=30)
        except (RequestError, ValueError, TypeError):
            raise
        except Exception as error:
            logger.warning("API operation failed or timed out: %s", type(error).__name__)
            raise Unavailable("Kubernetes acknowledgement unavailable") from error

    def finished(self, future: Future[Any]) -> None:
        """
        Release an intake slot only after the operation has actually finished.

        Args:
            future (Future[Any]): Completed event-loop operation.

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


class CompositionServer(APIServer):
    """
    Configure composition and activation routes with their existing bearer credential.
    """

    def __init__(self, api: API, namespace: str, token: str, *, host: str = "0.0.0.0", port: int = 8090) -> None:
        """
        Assemble namespace-scoped composition intake on the shared HTTP server.

        Args:
            api (API): Dedicated receipt adapter with its own write backlog.
            namespace (str): Fixed namespace served by this replica.
            token (str): Bearer credential loaded from a Kubernetes Secret.
            host (str): HTTP listening address.
            port (int): HTTP listening port.
        """
        super().__init__(api)
        store = CompositionStore(api, namespace)
        activations = ActivationStore(api, namespace)
        app = (
            APIBuilder()
            .with_handlers(lambda value: self.invoke(store.submit(value)) or {}, lambda key, audit: self.invoke(store.lookup(key, audit)))
            .with_activation_handlers(
                lambda value: self.invoke(activations.submit(value)) or {},
                lambda key: self.invoke(activations.lookup(key)),
                lambda key: self.invoke(activations.stop(key)),
            )
            .with_bearer_token(token)
            .with_rate_limits(RateLimitPolicy.from_environment(namespace))
            .build()
        )
        self.start(app, host=host, port=port, name="composition")


class ConnectionServer(APIServer):
    """
    Configure temporary connection routes with verified service-account authentication.
    """

    def __init__(self, api: API, settings: ConnectionSettings, *, host: str = "0.0.0.0", port: int = 8093) -> None:
        """
        Assemble scoped temporary connection intake on the shared HTTP server.

        Args:
            api (API): Dedicated intake and authentication transport.
            settings (ConnectionSettings): Endpoint namespace and TTL policy.
            host (str): HTTP listening address.
            port (int): HTTP listening port.
        """
        super().__init__(api)
        store = ConnectionStore(api, settings)
        app = connection_app(
            lambda token: self.invoke(store.authenticate(token)),
            lambda value, caller: self.invoke(store.submit(value, caller)),
            lambda namespace, identity, caller: self.invoke(store.lookup(namespace, identity, caller)),
            lambda namespace, identity, caller: self.invoke(store.revoke(namespace, identity, caller)),
            limits=RateLimitPolicy.from_environment(settings.operator_namespace + ":connections"),
        )
        self.start(app, host=host, port=port, name="connections")
