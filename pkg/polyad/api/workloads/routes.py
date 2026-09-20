"""
Register workload commands on the shared composition API blueprint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import jsonify, request

from polyad.api.http.errors import Unavailable
from polyad.operator.observability.tracing import identify_request
from polyad_types.api.requests import ActivationRequest, identity
from polyad_types.api.throughput import ThroughputSample
from polyad_types.serialization import converter

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from flask import Response

    from polyad.api.http.application import Routes
    from polyad_types.api.adaptation import AdaptationReport
    from polyad_types.api.service_level import ServiceLevelReport

__all__ = ("register_routes",)


def register_routes(
    app: Routes,
    activate: Callable[[ActivationRequest], dict[str, Any]] | None,
    activation_lookup: Callable[[str], dict[str, Any] | None] | None,
    activation_stop: Callable[[str], dict[str, Any] | None] | None,
    throughput: Callable[[ThroughputSample], dict[str, Any]] | None,
    adaptation: Callable[[AdaptationReport], dict[str, Any]] | None = None,
    service_level: Callable[[ServiceLevelReport], dict[str, Any]] | None = None,
) -> None:
    """
    Attach activation and throughput routes without creating another application.

    Args:
        app (Routes): Composition blueprint carrying authentication and error handling.
        activate (Callable[[ActivationRequest], dict[str, Any]] | None): Durable activation submission.
        activation_lookup (Callable[[str], dict[str, Any] | None] | None): Activation status lookup.
        activation_stop (Callable[[str], dict[str, Any] | None] | None): Activation stop handler.
        throughput (Callable[[ThroughputSample], dict[str, Any]] | None): Authorized throughput intake.
        adaptation (Callable[[AdaptationReport], dict[str, Any]] | None): Authorized SDK strategy lifecycle intake.
        service_level (Callable[[ServiceLevelReport], dict[str, Any]] | None): Authorized service-level observation intake.

    Returns:
        None: Routes are attached to the existing blueprint.
    """

    @app.post("/v1/activations")
    def activation_submit() -> tuple[Response, int]:
        value = converter.structure(request.get_json(), ActivationRequest)
        identify_request(value.requestId)
        if activate is None:
            raise Unavailable("activation service is not configured")
        return jsonify(activate(value)), 202

    @app.get("/v1/activations/<request_id>")
    def activation_status(request_id: str) -> tuple[Response, int]:
        if activation_lookup is None:
            raise Unavailable("activation service is not configured")
        request_id = identity(request_id)
        identify_request(request_id)
        value = activation_lookup(request_id)
        return (jsonify(value), 200) if value else (jsonify(error="activation not found"), 404)

    @app.post("/v1/activations/<request_id>/stop")
    def activation_cancel(request_id: str) -> tuple[Response, int]:
        if activation_stop is None:
            raise Unavailable("activation service is not configured")
        request_id = identity(request_id)
        identify_request(request_id)
        value = activation_stop(request_id)
        return (jsonify(value), 202) if value else (jsonify(error="activation not found"), 404)

    @app.post("/v1/throughput")
    def report() -> tuple[Response, int]:
        if throughput is None:
            raise Unavailable("throughput service is not configured")
        body = request.get_json()

        # Reject JSON booleans and coercible strings before the general model converter sees counters.
        if not isinstance(body, dict) or type(body.get("generation")) is not int:
            raise ValueError("throughput generation must be an integer")
        if any(type(body.get(name)) not in (int, float) for name in ("offeredPerSecond", "completedPerSecond")):
            raise ValueError("throughput rates must be numbers")
        traffic = body.get("traffic", [])
        if not isinstance(traffic, list) or any(
            not isinstance(item, dict)
            or type(item.get("generation")) is not int
            or any(type(item.get(name)) not in (int, float) for name in ("completedPerSecond", "headroomPerSecond"))
            for item in traffic
        ):
            raise ValueError("traffic observations require integer generations and numeric rates")
        return jsonify(throughput(converter.structure(body, ThroughputSample))), 202

    @app.post("/v1/adaptations")
    def adapt() -> tuple[Response, int]:
        if adaptation is None:
            raise Unavailable("adaptation status service is not configured")
        from polyad_types.api.adaptation import AdaptationReport

        return jsonify(adaptation(converter.structure(request.get_json(), AdaptationReport))), 202

    @app.post("/v1/service-level")
    def service() -> tuple[Response, int]:
        if service_level is None:
            raise Unavailable("service-level observation service is not configured")
        from polyad_types.api.service_level import ServiceLevelReport

        return jsonify(service_level(converter.structure(request.get_json(), ServiceLevelReport))), 202
