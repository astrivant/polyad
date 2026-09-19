"""
Expose immutable graph composition requests through a small authenticated Flask API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cattrs.errors import CattrsError
from flask import jsonify, request
from werkzeug.exceptions import HTTPException

from polyad.api.composition.openapi import openapi_document
from polyad.api.http.application import Routes
from polyad.api.http.errors import Conflict as Conflict
from polyad.api.http.errors import Forbidden
from polyad.api.http.errors import Unavailable as Unavailable
from polyad.api.http.limits import install_limits
from polyad.api.workloads.routes import register_routes
from polyad.auth.http import install
from polyad.auth.policy import public_demo
from polyad.compiler.passes.composition import compile_composition
from polyad.operator.observability.tracing import identify_request
from polyad_types.api.requests import CompositionRequest, identity
from polyad_types.serialization import converter

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from flask import Flask, Response

    from polyad.api.http.limits import RateLimitPolicy
    from polyad.auth.http import Access
    from polyad_types.api.adaptation import AdaptationReport
    from polyad_types.api.requests import ActivationRequest
    from polyad_types.api.service_level import ServiceLevelReport
    from polyad_types.api.throughput import ThroughputSample


def _build_app(
    submit: Callable[[CompositionRequest], dict[str, Any]],
    lookup: Callable[[str, bool], dict[str, Any] | None],
    *,
    token: str,
    title: str,
    version: str,
    rate_limits: RateLimitPolicy | None = None,
    activate: Callable[[ActivationRequest], dict[str, Any]] | None = None,
    activation_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    activation_stop: Callable[[str], dict[str, Any] | None] | None = None,
    access: Access | None = None,
    throughput: Callable[[ThroughputSample], dict[str, Any]] | None = None,
    adaptation: Callable[[AdaptationReport], dict[str, Any]] | None = None,
    service_level: Callable[[ServiceLevelReport], dict[str, Any]] | None = None,
    application: Flask | None = None,
) -> Flask:
    """
    Create an injectable WSGI app for request compilation, submission and audit lookup.

    Args:
        submit (Callable[[CompositionRequest], dict[str, Any]]): Durable create-if-absent receipt callback.
        lookup (Callable[[str, bool], dict[str, Any] | None]): Receipt or manifest-audit callback.
        token (str): Namespace-scoped bearer credential, supplied through a Secret.
        title (str): Service title for the OpenAPI document.
        version (str): API contract version for the OpenAPI document.
        rate_limits (RateLimitPolicy | None): Optional shared namespace and shard quota.
        activate (Callable[[ActivationRequest], dict[str, Any]] | None): Durable pulse submission handler.
        activation_lookup (Callable[[str], dict[str, Any] | None] | None): Pulse status handler.
        activation_stop (Callable[[str], dict[str, Any] | None] | None): Durable pulse stop handler.
        access (Access | None): Named service/operator credentials and shared request lanes.
        throughput (Callable[[ThroughputSample], dict[str, Any]] | None): Authorized aggregate throughput intake.
        adaptation (Callable[[AdaptationReport], dict[str, Any]] | None): Authorized SDK strategy lifecycle intake.
        service_level (Callable[[ServiceLevelReport], dict[str, Any]] | None): Authorized Daemon objective observations.
        application (Flask | None): Existing process application for blueprint registration.

    Returns:
        Flask: Configured app suitable for a production WSGI server.
    """
    if not token and not (access and access.supports("composition")) and not public_demo():
        raise ValueError("the composition API requires a bearer token")
    app = Routes("composition", application)

    authenticated = install(app, "composition", token, access)

    if rate_limits is not None:
        app.extensions["polyad.limiter"] = install_limits(app, rate_limits)

    @app.errorhandler(Conflict)
    def conflict(error: Conflict) -> tuple[Response, int]:
        return jsonify(error=str(error)), 409

    @app.errorhandler(Forbidden)
    def forbidden(error: Forbidden) -> tuple[Response, int]:
        return jsonify(error=str(error)), 403

    @app.errorhandler(Unavailable)
    def unavailable(error: Unavailable) -> tuple[Response, int]:
        return jsonify(error=str(error), retry="reuse the same requestId"), 503

    @app.errorhandler(CattrsError)
    @app.errorhandler(ValueError)
    @app.errorhandler(TypeError)
    @app.errorhandler(KeyError)
    def invalid(error: Exception) -> tuple[Response, int]:
        return jsonify(error=str(error)), 422

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException) -> tuple[Response, int]:
        return jsonify(error=error.description), error.code or 500

    @app.post("/v1/compositions")
    def compose() -> tuple[Response, int]:
        value = converter.structure(request.get_json(), CompositionRequest)
        identify_request(value.requestId)
        compile_composition(value, "preview")
        result = submit(value)
        return jsonify(result), 202

    @app.get("/v1/compositions/<request_id>")
    def status(request_id: str) -> tuple[Response, int]:
        request_id = identity(request_id)
        identify_request(request_id)
        result = lookup(request_id, False)
        return (jsonify(result), 200) if result is not None else (jsonify(error="composition not found"), 404)

    @app.get("/v1/compositions/<request_id>/resources")
    def resources(request_id: str) -> tuple[Response, int]:
        request_id = identity(request_id)
        identify_request(request_id)
        result = lookup(request_id, True)
        return (jsonify(result), 200) if result is not None else (jsonify(error="composition not found"), 404)

    register_routes(app, activate, activation_lookup, activation_stop, throughput, adaptation, service_level)

    schema = openapi_document(title, version)
    if not authenticated:
        schema["security"] = []
    if access and access.supports("composition"):
        for path in schema["paths"].values():
            for operation in path.values():
                if isinstance(operation, dict) and "responses" in operation:
                    operation["responses"]["403"] = {"description": "Credential does not authorize this operation"}
    app.extensions["polyad.openapi"] = schema

    @app.get("/openapi.json")
    def openapi() -> Response:
        return jsonify(schema)

    return app.finish()


def create_app(
    submit: Callable[[CompositionRequest], dict[str, Any]], lookup: Callable[[str, bool], dict[str, Any] | None], *, token: str
) -> Flask:
    """
    Build an API application using the fluent service builder.

    Args:
        submit (Callable[[CompositionRequest], dict[str, Any]]): Durable receipt submission handler.
        lookup (Callable[[str, bool], dict[str, Any] | None]): Status and audit read handler.
        token (str): Namespace-scoped bearer credential.

    Returns:
        Flask: Authenticated composition application.
    """
    from polyad.api.composition.builder import APIBuilder

    return APIBuilder().with_handlers(submit, lookup).with_bearer_token(token).build()
