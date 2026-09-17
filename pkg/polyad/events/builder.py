"""
Register authenticated event subscriptions on the shared HTTP application.
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
from polyad.api.errors import Forbidden, Unavailable
from polyad.api.limits import RateLimitPolicy, install_limits
from polyad.auth.http import Access, install
from polyad.auth.policy import public_demo
from polyad.events.store import CursorExpired, TopologyReplaced
from polyad.events.visibility import permitted_observation
from polyad_types.auth import APIKey, GraphAccess
from polyad_types.events import Event as Observation
from polyad_types.events import EventStreamSettings, EventTooLarge, event_schema
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
        authorize_stream (Callable[[APIKey | None, str | None], None] | None): Reject unsupported subscriptions before opening them.
        permits (Callable[[APIKey, dict[str, Any]], bool] | None): Fresh operator-mode and home-graph observation check.
        discover (Callable[[APIKey, GraphAccess | None, int, int], dict[str, Any]] | None): Live authorized service directory.
        websockets (bool): Enable WebSocket subscriptions alongside Server-Sent Events.
        settings (EventStreamSettings): Event serialization and replay budgets advertised to subscribers.
    """

    resolve: Callable[[str | None], str] | None = None
    read: Callable[[str], list[tuple[str, str]]] | None = None
    token: str = field(default="", repr=False)
    stopping: Event = field(factory=Event)
    max_connections: int = 16
    limits: RateLimitPolicy | None = None
    snapshot: Callable[[str, str, str | None, str | None], dict[str, Any]] | None = None
    access: Access | None = None
    authorize_stream: Callable[[APIKey | None, str | None], None] | None = None
    permits: Callable[[APIKey, dict[str, Any]], bool] | None = None
    discover: Callable[[APIKey, GraphAccess | None, int, int], dict[str, Any]] | None = None
    websockets: bool = False
    settings: EventStreamSettings = field(factory=EventStreamSettings)

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
            (
                not self.token
                and not (self.access and any(self.access.supports(scope) for scope in ("events", "topology", "discovery")))
                and not public_demo()
            )
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

        @app.get("/v1/discovery")
        def discovery() -> tuple[Response, int] | Response:
            key = getattr(g, "polyad_key", None)
            if key is None:
                return jsonify(error="discovery requires a named credential with graph grants and a home graph"), 403
            if self.discover is None or self.stopping.is_set():
                return jsonify(error="this operator cannot fulfill discovery requests"), 503
            if set(request.args) - {"graph", "kind", "namespace", "cluster", "uid", "offset", "limit"}:
                return jsonify(error="unsupported discovery parameter"), 400
            try:
                target = None
                if request.args.get("graph"):
                    target = GraphAccess(
                        request.args["graph"],
                        request.args.get("namespace", ""),
                        kind=request.args.get("kind", "Graph"),
                        cluster=request.args.get("cluster", ""),
                        uid=request.args.get("uid", ""),
                    )
                result = self.discover(key, target, int(request.args.get("offset", "0")), int(request.args.get("limit", "100")))
                response = jsonify(result)
                if len(response.get_data()) > 4 * 1024 * 1024:
                    return jsonify(error="discovery result exceeds 4 MiB; select a smaller graph"), 503
                response.headers["Cache-Control"] = "no-store"
                return response
            except Forbidden as error:
                return jsonify(error=str(error)), 403
            except KeyError:
                return jsonify(error="graph is absent or replaced"), 404
            except ValueError as error:
                return jsonify(error=str(error)), 400
            except Unavailable as error:
                return jsonify(error=str(error)), 503
            except Exception:
                return jsonify(error="discovery state or its parent operator is unavailable"), 503

        @app.get("/v1/graphs/<kind>/<name>/topology")
        def topology(kind: str, name: str) -> tuple[Response, int] | Response:
            if kind not in BOUNDARY_KINDS or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", name):
                return jsonify(error="invalid graph identity"), 400
            if self.snapshot is None or self.stopping.is_set():
                return jsonify(error="topology reader unavailable"), 503
            try:
                if self.authorize_stream is not None:
                    self.authorize_stream(getattr(g, "polyad_key", None), request.args.get("cluster"))
                snapshot = self.snapshot(kind, name, request.args.get("uid"), request.args.get("node"))
                key = getattr(g, "polyad_key", None)
                if key is not None and not permitted_observation(snapshot["graph"], snapshot.get("ancestry", []), key.graphs):
                    return jsonify(error="graph snapshot or node not found"), 404
                if key is not None and self.permits is not None and not self.permits(key, snapshot["graph"]):
                    return jsonify(error="graph exceeds the operator access mode or credential home scope"), 403
                response = jsonify(snapshot)
                if len(response.get_data()) > 4 * 1024 * 1024:
                    return jsonify(error="topology selection exceeds 4 MiB"), 503
                response.headers["Cache-Control"] = "no-store"
                return response
            except Forbidden as error:
                return jsonify(error=str(error)), 403
            except TopologyReplaced as error:
                return jsonify(error=str(error)), 409
            except KeyError:
                return jsonify(error="graph snapshot or node not found"), 404
            except Exception:
                return jsonify(error="topology observation unavailable or stale"), 503

        @app.get("/v1/events/config")
        def configuration() -> Response:
            return jsonify(
                maxEventBytes=self.settings.maxEventBytes,
                readBatchSize=self.settings.readBatchSize,
                pollIntervalSeconds=self.settings.pollIntervalSeconds,
            )

        @app.get("/v1/events/schema")
        def schema_document() -> Response:
            response = jsonify(event_schema())
            response.mimetype = "application/schema+json"
            return response

        @app.get("/v1/events/ws")
        @app.get("/v1/events")
        def events() -> Response | tuple[Response, int]:
            websocket = request.path == "/v1/events/ws"
            if websocket:
                if not self.websockets:
                    return jsonify(error="WebSocket subscriptions are disabled"), 404
                if not request.environ.get("polyad.websocket"):
                    return jsonify(error="a WebSocket upgrade is required"), 426
                if request.headers.get("Origin"):
                    return jsonify(error="use a service client with bearer headers; browser origins are unsupported"), 403
            if self.authorize_stream is not None:
                try:
                    self.authorize_stream(getattr(g, "polyad_key", None), request.args.get("cluster"))
                except Forbidden as error:
                    return jsonify(error=str(error)), 403
                except Exception:
                    return jsonify(error="this operator cannot fulfill the requested event subscription"), 503
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
                disconnected = request.environ.get("polyad.disconnected")

                def frame(event: str, data: dict[str, Any], identity: str = "", raw: str | None = None) -> str:
                    return Observation(identity, event, data).encode(
                        "websocket" if websocket else "sse", self.settings.maxEventBytes, raw=raw
                    )

                heartbeat = frame("heartbeat", {}) if websocket else ": heartbeat\n\n"
                yield heartbeat if websocket else "retry: 3000\n\n"
                while not self.stopping.is_set() and not (disconnected and disconnected.is_set()):
                    try:
                        batch = read(cursor)
                        if not batch:
                            yield heartbeat
                        for identity, data in batch:
                            cursor = identity
                            payload = json.loads(data)
                            key = getattr(g, "polyad_key", None)
                            identity_scope = payload.get("graph", payload) if payload.get("type") == "connection" else payload
                            if key is not None and not permitted_observation(identity_scope, payload.get("ancestry", []), key.graphs):
                                continue
                            if key is not None and self.permits is not None:
                                scope = identity_scope
                                if scope.get("kind") not in BOUNDARY_KINDS:
                                    scope = next(iter(payload.get("ancestry", [])), {})
                                if not scope or not self.permits(key, scope):
                                    continue
                            event_type = payload["type"] if payload.get("type") in {"topology", "connection"} else "graph"
                            yield frame(event_type, payload, identity, data)
                        if batch:
                            yield heartbeat
                    except EventTooLarge:
                        yield frame("reset", {"reason": "event exceeds configured maxEventBytes; refresh topology and replay cursor"})
                        return
                    except CursorExpired:
                        yield frame("reset", {"reason": "cursor expired; refresh graph status"})
                        return
                    except Exception:
                        yield frame("unavailable", {"reason": "reconnect with Last-Event-ID"})
                        return

            response = Response(
                stream_with_context(stream()),
                mimetype="application/x-polyad-events" if websocket else "text/event-stream",
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
                    "/v1/discovery": {
                        "get": {
                            "description": "Fresh service directory requiring a named key, home graph and explicit graph grants.",
                            "parameters": [
                                {"name": name, "in": "query", "schema": {"type": "string"}}
                                for name in ("graph", "namespace", "kind", "cluster", "uid", "offset", "limit")
                            ],
                            "responses": {
                                str(code): {"description": message}
                                for code, message in (
                                    (200, "Authorized graph roots or services with replay cursors"),
                                    (400, "Invalid selection"),
                                    (401, "Invalid credential"),
                                    (403, "Scope or access mode denied"),
                                    (404, "Graph absent or replaced"),
                                    (503, "This operator cannot fulfill discovery"),
                                )
                            },
                        }
                    },
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
            for path, description in (
                ("/v1/events/config", "EventStreamSettings: maximum serialized event bytes, replay batch size and idle polling interval."),
                ("/v1/events/schema", "Draft 2020-12 JSON Schema for supported transport-neutral event ASTs."),
            ):
                schema["paths"][path] = {"get": {"responses": {"200": {"description": description}}}}
            schema["x-polyad-event-schema"] = "/v1/events/schema"
            if self.websockets:
                schema["paths"]["/v1/events/ws"] = {
                    "get": {
                        "description": (
                            "Optional WebSocket subscription on the events listener. Uses the same bearer header, "
                            "Last-Event-ID, cluster selection, graph permissions and shared subscriber budget as SSE. "
                            "Each text frame is a JSON object with id, event and data; heartbeat frames carry no cursor. "
                            "Client data frames are rejected; mutations continue through their authenticated HTTP APIs."
                        ),
                        "parameters": schema["paths"]["/v1/events"]["get"]["parameters"],
                        "responses": {
                            **{key: value for key, value in schema["paths"]["/v1/events"]["get"]["responses"].items() if key != "200"},
                            "101": {"description": "WebSocket event subscription accepted"},
                            "426": {"description": "WebSocket upgrade required"},
                        },
                    }
                }
            return Response(json.dumps(schema), mimetype="application/json")

        return app.finish()
