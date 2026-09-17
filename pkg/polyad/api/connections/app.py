"""
Expose bounded, authenticated temporary-connection HTTP operations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cattrs.errors import CattrsError
from flask import g, jsonify, request

from polyad.api.application import Routes
from polyad.api.errors import Conflict, Forbidden, Unauthorized, Unavailable
from polyad.api.limits import install_limits
from polyad.auth.policy import public_demo
from polyad.compiler.passes.schema import structural_schema
from polyad.operator.coordination.pulses import PulseDeferred
from polyad_types.codec import converter
from polyad_types.requests import ConnectionRequest, ConnectionResponse, ServiceConnectionRequest

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from flask import Flask, Response

    from polyad.api.connections.store import Caller
    from polyad.api.limits import RateLimitPolicy


def build_app(
    authenticate: Callable[[str], Caller],
    submit: Callable[[ConnectionRequest, Caller], dict[str, Any]],
    lookup: Callable[[str, str, Caller], dict[str, Any] | None],
    revoke: Callable[[str, str, Caller], dict[str, Any] | None],
    *,
    services: Callable[[ServiceConnectionRequest, Caller], dict[str, Any]] | None = None,
    respond: Callable[[str, str, ConnectionResponse, Caller], dict[str, Any] | None] | None = None,
    limits: RateLimitPolicy | None = None,
    application: Flask | None = None,
) -> Flask:
    """
    Construct a service-account authenticated connection listener.

    Args:
        authenticate (Callable[[str], Caller]): Kubernetes TokenReview adapter.
        submit (Callable[[ConnectionRequest, Caller], dict[str, Any]]): Durable intake callback.
        lookup (Callable[[str, str, Caller], dict[str, Any] | None]): Authorized receipt lookup.
        revoke (Callable[[str, str, Caller], dict[str, Any] | None]): Authorized early revocation.
        services (Callable[[ServiceConnectionRequest, Caller], dict[str, Any]] | None): Exact-service negotiation callback.
        respond (Callable[[str, str, ConnectionResponse, Caller], dict[str, Any] | None] | None): Endpoint consent callback.
        limits (RateLimitPolicy | None): Optional shared HTTP request budget.
        application (Flask | None): Existing process application for blueprint registration.

    Returns:
        Flask: Separate connection API with a 64 KiB request limit.
    """
    app = Routes("connections", application, max_body=65536)
    schema = structural_schema(ConnectionRequest)
    schema["additionalProperties"] = False

    @app.before_request
    def authorize() -> None:
        if public_demo():
            g.caller = authenticate("")
            return
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise Unauthorized("a projected service-account bearer token is required")
        g.caller = authenticate(header[7:])

    if limits:
        app.extensions["polyad.limiter"] = install_limits(app, limits)

    def failure(error: Exception) -> tuple[Response, int]:
        codes = {Unauthorized: 401, Forbidden: 403, Conflict: 409, Unavailable: 503}
        return jsonify(error=str(error)), codes.get(type(error), 400)

    for error_type in (Unauthorized, Forbidden, Conflict, Unavailable, ValueError, TypeError, CattrsError):
        app.register_error_handler(error_type, failure)

    @app.errorhandler(PulseDeferred)
    def deferred(error: PulseDeferred) -> tuple[Response, int, dict[str, str]]:
        return jsonify(error=str(error)), 429, {"Retry-After": str(error.retry_after)}

    @app.post("/v1/connections")
    def create() -> tuple[Response, int]:
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("request must be a JSON object")
        for name in ("requestId", "namespace", "kind", "graph", "graphUid", "source", "target"):
            if not isinstance(value.get(name), str):
                raise ValueError(f"{name} must be a string")
        if type(value.get("ttlSeconds")) is not int or type(value.get("bidirectional", False)) is not bool:
            raise ValueError("ttlSeconds must be an integer and bidirectional must be a boolean")
        ports = value.get("ports", [])
        if not isinstance(ports, list) or any(not isinstance(port, dict) or type(port.get("port")) is not int for port in ports):
            raise ValueError("ports must be a list of destination ports with integer port numbers")
        return jsonify(submit(converter.structure(value, ConnectionRequest), g.caller)), 202

    @app.post("/v1/connections/atlas")
    def create_service_connection() -> tuple[Response, int]:
        if services is None:
            raise Unavailable("this operator cannot fulfill atlas service negotiation")
        value = request.get_json()
        if not isinstance(value, dict) or type(value.get("ttlSeconds")) is not int or type(value.get("bidirectional", False)) is not bool:
            raise ValueError("request requires integer ttlSeconds and boolean bidirectional")
        if not isinstance(value.get("requestId"), str):
            raise ValueError("requestId must be a string")
        for side in ("source", "target"):
            if not isinstance(value.get(side), dict) or not all(isinstance(item, str) for item in value[side].values()):
                raise ValueError("service endpoints require string identity fields")
        ports = value.get("ports", [])
        if not isinstance(ports, list) or any(not isinstance(port, dict) or type(port.get("port")) is not int for port in ports):
            raise ValueError("ports require integer port numbers")
        return jsonify(services(converter.structure(value, ServiceConnectionRequest), g.caller)), 202

    @app.get("/v1/connections/<namespace>/<request_id>")
    def observe(namespace: str, request_id: str) -> tuple[Response, int]:
        value = lookup(namespace, request_id, g.caller)
        return (jsonify(value), 200) if value is not None else (jsonify(error="connection not found"), 404)

    @app.delete("/v1/connections/<namespace>/<request_id>")
    def remove(namespace: str, request_id: str) -> tuple[Response, int]:
        value = revoke(namespace, request_id, g.caller)
        return (jsonify(value), 202) if value is not None else (jsonify(error="connection not found"), 404)

    @app.post("/v1/connections/<namespace>/<name>/response")
    def response(namespace: str, name: str) -> tuple[Response, int]:
        if respond is None:
            raise Unavailable("connection response handler is unavailable")
        value = request.get_json()
        if not isinstance(value, dict) or set(value) != {"uid", "decision"} or not all(isinstance(item, str) for item in value.values()):
            raise ValueError("response requires only string uid and decision fields")
        result = respond(namespace, name, converter.structure(value, ConnectionResponse), g.caller)
        return (jsonify(result), 202) if result is not None else (jsonify(error="connection not found"), 404)

    @app.after_request
    def prevent_caching(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/openapi.json")
    def openapi() -> Response:
        responses = {
            str(code): {"description": description}
            for code, description in (
                (202, "Durable intent accepted; enforcement is asynchronous"),
                (400, "Invalid request"),
                (401, "Invalid service-account token"),
                (403, "Namespace scope or connect permission denied"),
                (404, "Receipt not found"),
                (409, "Identity or idempotency conflict"),
                (413, "Body exceeds 64 KiB"),
                (429, "Request quota exhausted"),
                (503, "Kubernetes or shared intake unavailable"),
            )
        }
        parameters = [{"name": name, "in": "path", "required": True, "schema": {"type": "string"}} for name in ("namespace", "requestId")]
        return jsonify(
            {
                "openapi": "3.0.3",
                "info": {"title": "Polyad Temporary Connections API", "version": "v1alpha1"},
                "security": [] if public_demo() else [{"serviceAccount": []}],
                "components": {
                    "securitySchemes": {
                        "serviceAccount": {
                            "type": "http",
                            "scheme": "bearer",
                            "bearerFormat": "Kubernetes projected service-account JWT; audience polyad-connections",
                        }
                    },
                    "schemas": {
                        "ConnectionRequest": schema,
                        "ConnectionResponse": structural_schema(ConnectionResponse),
                        "ServiceConnectionRequest": structural_schema(ServiceConnectionRequest),
                    },
                },
                "paths": {
                    "/v1/connections/atlas": {
                        "post": {
                            "description": "Negotiate exact services at a common application boundary owned by this operator.",
                            "parameters": [
                                {
                                    "name": "X-Polyad-Cluster",
                                    "in": "header",
                                    "schema": {"type": "string"},
                                    "description": "Registered token issuer; authenticated through that cluster TokenReview API.",
                                }
                            ],
                            "requestBody": {
                                "required": True,
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ServiceConnectionRequest"}}},
                            },
                            "responses": responses,
                        }
                    },
                    "/v1/connections/{namespace}/{name}/response": {
                        "post": {
                            "parameters": [
                                {"name": key, "in": "path", "required": True, "schema": {"type": "string"}} for key in ("namespace", "name")
                            ],
                            "description": "Approve or reject as a Pod belonging to one endpoint, with approve permission on the graph.",
                            "requestBody": {
                                "required": True,
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ConnectionResponse"}}},
                            },
                            "responses": responses,
                        }
                    },
                    "/v1/connections": {
                        "post": {
                            "description": (
                                "Create an immutable TTL-bound connection on a graph instance. "
                                "Replaying the same requestId does not extend its deadline."
                            ),
                            "requestBody": {
                                "required": True,
                                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ConnectionRequest"}}},
                            },
                            "responses": responses,
                        }
                    },
                    "/v1/connections/{namespace}/{requestId}": {
                        "get": {
                            "parameters": parameters,
                            "responses": {
                                **responses,
                                "200": {"description": "Caller-owned receipt with immutable expiresAt and observed status"},
                            },
                        },
                        "delete": {
                            "parameters": parameters,
                            "description": "Request early revocation; wait for Revoked before assuming policy writes are observed.",
                            "responses": responses,
                        },
                    },
                },
            }
        )

    return app.finish()
