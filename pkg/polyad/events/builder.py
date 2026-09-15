"""
Build an authenticated, separately hosted Server-Sent Events API.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from threading import BoundedSemaphore, Event
from typing import TYPE_CHECKING

from attrs import evolve, field, frozen
from flask import Flask, Response, jsonify, request

from polyad.api.limits import RateLimitPolicy, install_limits
from polyad.events.store import CursorExpired

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any, Self


@frozen
class EventAPIBuilder:
    """
    Compose read-only event transport independently of composition intake.

    Attributes:
        resolve (Callable[[str | None], str] | None): Cursor validation callback.
        read (Callable[[str], list[tuple[str, str]]] | None): Bounded observation reader.
        token (str): Subscriber-only bearer credential.
        stopping (Event): Server shutdown signal.
        max_connections (int): Maximum simultaneous streams on this replica.
        limits (RateLimitPolicy | None): Shared connection-request rate limit.
    """

    resolve: Callable[[str | None], str] | None = None
    read: Callable[[str], list[tuple[str, str]]] | None = None
    token: str = field(default="", repr=False)
    stopping: Event = field(factory=Event)
    max_connections: int = 16
    limits: RateLimitPolicy | None = None

    def with_handlers(self, resolve: Callable[[str | None], str], read: Callable[[str], list[tuple[str, str]]]) -> Self:
        """
        Bind replay callbacks.

        Args:
            resolve (Callable[[str | None], str]): Cursor validation callback.
            read (Callable[[str], list[tuple[str, str]]]): Observation batch reader.

        Returns:
            Self: Configured builder copy.
        """
        return evolve(self, resolve=resolve, read=read)

    def with_bearer_token(self, token: str) -> Self:
        """
        Set the subscriber credential.

        Args:
            token (str): Required bearer token.

        Returns:
            Self: Configured builder copy.
        """
        return evolve(self, token=token)

    def build(self) -> Flask:
        """
        Construct bounded streaming routes and their OpenAPI endpoint.

        Returns:
            Flask: Standalone event application.
        """
        if not self.token or self.resolve is None or self.read is None or not 1 <= self.max_connections <= 128:
            raise ValueError("event API requires a token, handlers and 1 through 128 connection slots")
        resolve, read = self.resolve, self.read
        app = Flask(__name__)
        slots = BoundedSemaphore(self.max_connections)

        @app.before_request
        def authorize() -> tuple[Response, int] | None:
            if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), f"Bearer {self.token}".encode()):
                return jsonify(error="unauthorized"), 401
            return None

        if self.limits:
            app.extensions["polyad.limiter"] = install_limits(app, self.limits)

        @app.get("/v1/events")
        def events() -> Response | tuple[Response, int]:
            if self.stopping.is_set() or not slots.acquire(blocking=False):
                return jsonify(error="event subscriber capacity exhausted"), 503
            try:
                cursor = resolve(request.headers.get("Last-Event-ID"))
            except CursorExpired as error:
                slots.release()
                return jsonify(error=str(error)), 410
            except ValueError as error:
                slots.release()
                return jsonify(error=str(error)), 400
            except Exception:
                slots.release()
                return jsonify(error="event cache unavailable"), 503

            def stream() -> Iterator[str]:
                nonlocal cursor
                yield "retry: 3000\n\n"
                while not self.stopping.is_set():
                    try:
                        batch = read(cursor)
                        if not batch:
                            yield ": heartbeat\n\n"
                        for identity, data in batch:
                            cursor = identity
                            yield f"id: {identity}\nevent: graph\ndata: {data}\n\n"
                    except CursorExpired:
                        yield 'event: reset\ndata: {"reason":"cursor expired; refresh graph status"}\n\n'
                        return
                    except Exception:
                        yield 'event: unavailable\ndata: {"reason":"reconnect with Last-Event-ID"}\n\n'
                        return

            response = Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
            response.call_on_close(slots.release)
            return response

        @app.get("/openapi.json")
        def openapi() -> Response:
            schema: dict[str, Any] = {
                "openapi": "3.0.3",
                "info": {"title": "Polyad Events API", "version": "v1alpha1"},
                "security": [{"bearerAuth": []}],
                "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
                "paths": {
                    "/v1/events": {
                        "get": {
                            "description": "Namespace observations with bounded, at-least-once replay; reconnect with Last-Event-ID.",
                            "parameters": [{"name": "Last-Event-ID", "in": "header", "schema": {"type": "string"}}],
                            "responses": {
                                "200": {
                                    "description": "Graph observations with IDs and audit references.",
                                    "content": {"text/event-stream": {"schema": {"type": "string"}}},
                                },
                                **{
                                    str(code): {"description": description}
                                    for code, description in (
                                        (400, "Invalid cursor"),
                                        (401, "Unauthorized"),
                                        (410, "Replay window expired"),
                                        (429, "Connection request limit exceeded"),
                                        (503, "Cache unavailable or subscriber capacity full"),
                                    )
                                },
                            },
                        }
                    }
                },
            }
            return Response(json.dumps(schema), mimetype="application/json")

        return app
