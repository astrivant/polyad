"""
Register authenticated Server-Sent Events routes on the shared HTTP application.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from threading import BoundedSemaphore, Event
from typing import TYPE_CHECKING, Any

from attrs import evolve, field, frozen
from flask import Response, g, jsonify, request, stream_with_context

from polyad.api.application import Routes
from polyad.api.limits import RateLimitPolicy, install_limits
from polyad.auth.http import Access, install
from polyad.auth.policy import public_demo
from polyad.events.store import CursorExpired, TopologyReplaced
from polyad.events.visibility import permitted_observation
from polyad_types.resources import BOUNDARY_KINDS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Self

    from flask import Flask


@frozen
class EventAPIBuilder:
    """
    Configure read-only event routes and bounded streaming admission.

    Attributes:
        resolve (Callable[[str | None], str] | None): Cursor validation callback.
        read (Callable[[str], list[tuple[str, str]]] | None): Bounded observation reader.
        token (str): Subscriber-only bearer credential.
        stopping (Event): Server shutdown signal.
        max_connections (int): Maximum simultaneous streams on this replica.
        limits (RateLimitPolicy | None): Shared connection-request rate limit.
        snapshot (Callable[[str, str, str | None, str | None], dict[str, Any]] | None): Current topology reader.
        access (Access | None): Named subscriber credentials and shared lanes.
    """

    resolve: Callable[[str | None], str] | None = None
    read: Callable[[str], list[tuple[str, str]]] | None = None
    token: str = field(default="", repr=False)
    stopping: Event = field(factory=Event)
    max_connections: int = 16
    limits: RateLimitPolicy | None = None
    snapshot: Callable[[str, str, str | None, str | None], dict[str, Any]] | None = None
    access: Access | None = None

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

    def with_topology_handler(self, snapshot: Callable[[str, str, str | None, str | None], dict[str, Any]]) -> Self:
        """
        Bind the current topology and neighbor snapshot reader.

        Args:
            snapshot (Callable[[str, str, str | None, str | None], dict[str, Any]]): Namespace-scoped snapshot callback.

        Returns:
            Self: Configured builder copy.
        """
        return evolve(self, snapshot=snapshot)

    def build(self, application: Flask | None = None) -> Flask:
        """
        Construct bounded streaming routes and their OpenAPI endpoint.

        Args:
            application (Flask | None): Shared application, or None for standalone use.

        Returns:
            Flask: Application containing the event blueprint.
        """
        if (
            (not self.token and not (self.access and self.access.supports("events")) and not public_demo())
            or self.resolve is None
            or self.read is None
            or not 1 <= self.max_connections <= 128
        ):
            raise ValueError("event API requires a token, handlers and 1 through 128 connection slots")
        resolve, read = self.resolve, self.read
        app = Routes("events", application, max_body=1024)
        slots = BoundedSemaphore(self.max_connections)

        authenticated = install(app, "events", self.token, self.access)

        if self.limits:
            app.extensions["polyad.limiter"] = install_limits(app, self.limits)

        @app.get("/v1/graphs/<kind>/<name>/topology")
        def topology(kind: str, name: str) -> tuple[Response, int] | Response:
            if kind not in BOUNDARY_KINDS or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", name):
                return jsonify(error="invalid graph identity"), 400
            if self.snapshot is None or self.stopping.is_set():
                return jsonify(error="topology reader unavailable"), 503
            try:
                snapshot = self.snapshot(kind, name, request.args.get("uid"), request.args.get("node"))
                key = getattr(g, "polyad_key", None)
                if key is not None and not permitted_observation(snapshot["graph"], snapshot.get("ancestry", []), key.graphs):
                    return jsonify(error="graph snapshot or node not found"), 404
                response = jsonify(snapshot)
                if len(response.get_data()) > 4 * 1024 * 1024:
                    return jsonify(error="topology selection exceeds 4 MiB"), 503
                response.headers["Cache-Control"] = "no-store"
                return response
            except TopologyReplaced as error:
                return jsonify(error=str(error)), 409
            except KeyError:
                return jsonify(error="graph snapshot or node not found"), 404
            except Exception:
                return jsonify(error="topology observation unavailable or stale"), 503

        @app.get("/v1/events")
        def events() -> Response | tuple[Response, int]:
            # Stream slots protect shared HTTP worker capacity even in demo mode.
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
                            payload = json.loads(data)
                            key = getattr(g, "polyad_key", None)
                            identity_scope = payload.get("graph", payload) if payload.get("type") == "connection" else payload
                            if key is not None and not permitted_observation(identity_scope, payload.get("ancestry", []), key.graphs):
                                continue
                            event_type = payload["type"] if payload.get("type") in {"topology", "connection"} else "graph"
                            yield f"id: {identity}\nevent: {event_type}\ndata: {data}\n\n"
                        if batch:
                            yield ": heartbeat\n\n"
                    except CursorExpired:
                        yield 'event: reset\ndata: {"reason":"cursor expired; refresh graph status"}\n\n'
                        return
                    except Exception:
                        yield 'event: unavailable\ndata: {"reason":"reconnect with Last-Event-ID"}\n\n'
                        return

            response = Response(
                stream_with_context(stream()),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
            response.call_on_close(slots.release)
            return response

        @app.get("/openapi.json")
        def openapi() -> Response:
            schema: dict[str, Any] = {
                "openapi": "3.0.3",
                "info": {"title": "Polyad Events API", "version": "v1alpha1"},
                "security": [{"bearerAuth": []}] if authenticated else [],
                "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
                "paths": {
                    "/v1/events": {
                        "get": {
                            "description": (
                                "Application graph observations, topology changes and connection-consent proposals with bounded replay. "
                                "The reserved operator graph, internal definitions and their descendants are excluded. "
                                "Read a topology snapshot first, then subscribe with its cursor as Last-Event-ID."
                            ),
                            "parameters": [
                                {"name": "Last-Event-ID", "in": "header", "schema": {"type": "string"}},
                                {"name": "cluster", "in": "query", "schema": {"type": "string"}},
                            ],
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
                                        (403, "Credential does not authorize this API"),
                                        (410, "Replay window expired"),
                                        (429, "Connection request limit exceeded"),
                                        (503, "Cache unavailable or subscriber capacity full"),
                                    )
                                },
                            },
                        }
                    },
                    "/v1/graphs/{kind}/{name}/topology": {
                        "get": {
                            "description": (
                                "Current desired neighbors and observed executions. Optional node selects incoming/outgoing neighbors "
                                "and dependencies. Snapshots include a structural revision and an atomic stream cursor; "
                                "observations older than 30 seconds are unavailable. No workload templates or credentials are returned."
                            ),
                            "parameters": [
                                {
                                    "name": "kind",
                                    "in": "path",
                                    "required": True,
                                    "schema": {"type": "string", "enum": sorted(BOUNDARY_KINDS)},
                                },
                                {"name": "name", "in": "path", "required": True, "schema": {"type": "string"}},
                                {"name": "uid", "in": "query", "schema": {"type": "string"}, "description": "Expected graph UID."},
                                {"name": "cluster", "in": "query", "schema": {"type": "string"}},
                                {
                                    "name": "node",
                                    "in": "query",
                                    "schema": {"type": "string"},
                                    "description": "Logical node or replica ordinal.",
                                },
                            ],
                            "responses": {
                                "200": {
                                    "description": "Graph topology or selected node's neighbors, with revision, observedAt and cursor.",
                                    "content": {"application/json": {"schema": {"type": "object"}}},
                                },
                                **{
                                    str(code): {"description": description}
                                    for code, description in (
                                        (400, "Invalid graph identity"),
                                        (401, "Unauthorized"),
                                        (403, "Credential does not authorize this API"),
                                        (404, "Graph snapshot or node not found"),
                                        (409, "Graph UID changed"),
                                        (429, "Request limit exceeded"),
                                        (503, "Snapshot unavailable or stale"),
                                    )
                                },
                            },
                        }
                    },
                },
            }
            return Response(json.dumps(schema), mimetype="application/json")

        return app.finish()
