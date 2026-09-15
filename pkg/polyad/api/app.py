"""
Expose immutable graph composition requests through a small authenticated Flask API.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from cattrs.errors import CattrsError
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from polyad.api.limits import install_limits
from polyad.api.openapi import openapi_document
from polyad.compiler.composition import CompositionRequest, compile_composition, identity
from polyad.graph.topology import converter

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from flask import Response

    from polyad.api.limits import RateLimitPolicy


class Conflict(ValueError):
    """
    Reject a request ID that already identifies different or deleting intent.
    """


class Unavailable(RuntimeError):
    """
    Report an uncertain submission without encouraging a new request identity.
    """


def _build_app(
    submit: Callable[[CompositionRequest], dict[str, Any]],
    lookup: Callable[[str, bool], dict[str, Any] | None],
    *,
    token: str,
    title: str,
    version: str,
    rate_limits: RateLimitPolicy | None = None,
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

    Returns:
        Flask: Configured app suitable for a production WSGI server.
    """
    if not token:
        raise ValueError("the composition API requires a bearer token")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

    @app.before_request
    def authorize() -> tuple[Response, int] | None:
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            return jsonify(error="unauthorized"), 401
        return None

    if rate_limits is not None:
        app.extensions["polyad.limiter"] = install_limits(app, rate_limits)

    @app.errorhandler(Conflict)
    def conflict(error: Conflict) -> tuple[Response, int]:
        return jsonify(error=str(error)), 409

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
        compile_composition(value, "preview")
        result = submit(value)
        return jsonify(result), 202

    @app.get("/v1/compositions/<request_id>")
    def status(request_id: str) -> tuple[Response, int]:
        result = lookup(identity(request_id), False)
        return (jsonify(result), 200) if result is not None else (jsonify(error="composition not found"), 404)

    @app.get("/v1/compositions/<request_id>/resources")
    def resources(request_id: str) -> tuple[Response, int]:
        result = lookup(identity(request_id), True)
        return (jsonify(result), 200) if result is not None else (jsonify(error="composition not found"), 404)

    schema = openapi_document(title, version)
    app.extensions["polyad.openapi"] = schema

    @app.get("/openapi.json")
    def openapi() -> Response:
        return jsonify(schema)

    return app


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
    from polyad.api.builder import APIBuilder

    return APIBuilder().with_handlers(submit, lookup).with_bearer_token(token).build()
