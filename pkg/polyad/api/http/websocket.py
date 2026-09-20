"""
Serve optional WebSocket events and existing HTTP routes through one Flask application.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING, cast

from hypercorn.asyncio import serve
from hypercorn.config import Config
from hypercorn.middleware import AsyncioWSGIMiddleware

from polyad.operator.observability.pressure import pressure

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from typing import Any

    from flask import Flask
    from hypercorn.config import Sockets
    from hypercorn.typing import ASGIFramework

logger = logging.getLogger(__name__)


class _ListenerConfig(Config):
    _listeners: Sockets | None = None

    def create_sockets(self) -> Sockets:
        if self._listeners is None:
            self._listeners = super().create_sockets()
        return self._listeners


class WebSocketServer:
    """
    Replace the WSGI-only transport when enabled, retaining one bounded worker pool.
    """

    def __init__(self, app: Flask, host: str, ports: dict[str, int], streams: int, stopping: Event) -> None:
        """
        Bind every enabled API listener before starting the shared serving thread.

        Args:
            app (Flask): Existing process application, including its authentication and streaming cleanup.
            host (str): Listener address.
            ports (dict[str, int]): API family to port, including at most one ephemeral test port.
            streams (int): Shared SSE/WebSocket subscription budget.
            stopping (Event): Operator shutdown signal.
        """
        self.app, self.stopping = pressure.wrap(app), stopping
        self.workers, self.capacity, self.active = streams + 8, streams + 64, 0
        self.config = _ListenerConfig()
        address = f"[{host}]" if ":" in host and not host.startswith("[") else host
        self.config.bind = [f"{address}:{port}" for port in ports.values()]
        self.config.graceful_timeout = 35
        self.config.keep_alive_timeout = 30
        self.config.read_timeout = 30
        self.config.websocket_max_message_size = 1024
        self.config.websocket_ping_interval = 20
        self.config.max_app_queue_size = 2
        self.config.accesslog = None
        self.sockets = self.config.create_sockets()
        self.effective_listen = [sock.getsockname()[:2] for sock in self.sockets.insecure_sockets]

    def run(self) -> None:
        """
        Own one transport event loop and a fixed worker pool until graceful shutdown.

        Returns:
            None: Listener descriptors and WSGI workers are closed before returning.
        """

        async def shutdown() -> None:
            while not self.stopping.is_set():
                await asyncio.sleep(0.1)

        async def start() -> None:
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(self.workers, thread_name_prefix="polyad-http-worker"))
            await serve(cast("ASGIFramework", self), self.config, shutdown_trigger=shutdown, mode="asgi")

        try:
            asyncio.run(start())
        finally:
            for sock in self.sockets.insecure_sockets:
                sock.close()

    async def __call__(self, scope: dict[str, Any], receive: Callable[[], Awaitable[Any]], send: Callable[[Any], Awaitable[None]]) -> None:
        """
        Translate WebSocket framing while keeping Flask authorization and response ownership.

        Args:
            scope (dict[str, Any]): ASGI connection scope with the trusted socket identity.
            receive (Callable[[], Awaitable[Any]]): Inbound protocol messages.
            send (Callable[[Any], Awaitable[None]]): Backpressured protocol writer.

        Returns:
            None: A subscription ends on disconnect, shutdown, policy failure or a bounded send timeout.
        """
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        websocket = scope["type"] == "websocket"
        if websocket:
            await receive()  # Initial websocket.connect, before accepting or rejecting.
        if self.active >= self.capacity:
            prefix = "websocket.http.response" if websocket else "http.response"
            await send({"type": f"{prefix}.start", "status": 503, "headers": []})
            await send({"type": f"{prefix}.body", "body": b"subscriber transport capacity exhausted"})
            return
        self.active += 1
        disconnected = Event()
        accepted = False
        watcher: asyncio.Task[None] | None = None

        async def watch() -> None:
            while True:
                message = await receive()
                if message["type"] in {"http.disconnect", "websocket.disconnect"}:
                    disconnected.set()
                    return
                if websocket and message["type"] == "websocket.receive":
                    disconnected.set()
                    await asyncio.wait_for(send({"type": "websocket.close", "code": 1008, "reason": "read-only subscription"}), 10)
                    return

        async def body() -> Any:
            nonlocal watcher
            message = {"type": "http.request", "body": b""} if websocket else await receive()
            if message["type"] == "http.disconnect":
                disconnected.set()
            elif not message.get("more_body", False):
                watcher = asyncio.create_task(watch())
            return message

        async def output(message: Any) -> None:
            nonlocal accepted
            if disconnected.is_set():
                raise ConnectionAbortedError("event client disconnected")
            if not websocket:
                await asyncio.wait_for(send(message), 10)
            elif message["type"] == "http.response.start":
                accepted = message["status"] == 200
                if accepted:
                    await asyncio.wait_for(send({"type": "websocket.accept"}), 10)
                else:
                    await asyncio.wait_for(send({**message, "type": "websocket.http.response.start"}), 10)
            elif not accepted:
                await asyncio.wait_for(send({**message, "type": "websocket.http.response.body"}), 10)
            elif message.get("body"):
                await asyncio.wait_for(send({"type": "websocket.send", "text": message["body"].decode("utf-8")}), 10)

        def application(environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
            # Hypercorn supplies an integer; the existing socket router uses string ports.
            environ["SERVER_PORT"] = str(environ["SERVER_PORT"])
            environ["polyad.disconnected"] = disconnected
            if websocket:
                environ["polyad.websocket"] = True
            response = self.app(environ, start_response)
            try:
                iterator = iter(response)

                # The adapter emits headers on its first chunk, including bodyless HEAD/204 responses.
                yield next(iterator, b"")
                yield from iterator
            finally:
                response.close()

        http_scope = dict(scope)
        if websocket:
            http_scope.update(type="http", method="GET", scheme="https" if scope.get("scheme") == "wss" else "http")
            http_scope["headers"] = [(key, value) for key, value in scope["headers"] if key not in {b"upgrade", b"connection"}]

            # Only the explicit subscription route may be upgraded, even with valid credentials.
            if scope["path"] != "/v1/events/ws":
                http_scope["path"] = "/__polyad_invalid_websocket_route__"
        code = 1000
        try:
            await AsyncioWSGIMiddleware(application, max_body_size=1024 * 1024)(http_scope, body, output)  # type: ignore[arg-type]
        except (OSError, asyncio.CancelledError):
            disconnected.set()
        except Exception:
            if not websocket:
                raise
            code = 1011
            logger.warning("WebSocket subscription closed after an authorization or stream failure", exc_info=True)
        finally:
            self.active -= 1
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if accepted and not disconnected.is_set():
                try:
                    await asyncio.wait_for(send({"type": "websocket.close", "code": code}), 10)
                except OSError:
                    pass
            disconnected.set()
