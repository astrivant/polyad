"""
Build a read-only telemetry API independent of composition and event workers.
"""

from __future__ import annotations

import json

from apispec import APISpec
from attrs import evolve, frozen
from flask import Flask, Response, request
from prometheus_client import CONTENT_TYPE_LATEST

from polyad.metrics.store import MetricsStore
from polyad.operator.health import lifecycle


@frozen
class MetricsAPIBuilder:
    """
    Compose an HTTP API around a cached metrics source.

    Attributes:
        store (MetricsStore | None): Snapshot source populated by the operator loop.
    """

    store: MetricsStore | None = None

    def with_store(self, store: MetricsStore) -> MetricsAPIBuilder:
        """
        Bind a snapshot source without mutating this builder.

        Args:
            store (MetricsStore): Thread-safe metrics publication store.

        Returns:
            MetricsAPIBuilder: Builder with the supplied source.
        """
        return evolve(self, store=store)

    def build(self) -> Flask:
        """
        Expose Prometheus, JSON and OpenAPI endpoints on a dedicated service.

        Returns:
            Flask: Read-only application with no scrape-time external I/O.
        """
        if self.store is None:
            raise ValueError("metrics API requires a snapshot store")
        store = self.store
        app = Flask(__name__)
        spec = APISpec(title="Polyad metrics API", version="v1alpha1", openapi_version="3.0.3")
        for path, media in (("/metrics", "text/plain"), ("/v1/metrics", "application/json")):
            spec.path(
                path=path,
                operations={
                    "get": {
                        "responses": {
                            "200": {
                                "description": "Cached scheduler observations",
                                "content": {media: {"schema": {"type": "string" if path == "/metrics" else "object"}}},
                            },
                            "503": {"description": "Snapshot unavailable or replica retiring"},
                        }
                    }
                },
            )

        @app.get("/openapi.json")
        def openapi() -> Response:
            return Response(json.dumps(spec.to_dict()), content_type="application/json")

        @app.get("/metrics")
        @app.get("/v1/metrics")
        def metrics() -> Response:
            sample = store.read()
            if sample is None or lifecycle.replacement.is_set() or lifecycle.draining.is_set():
                return Response('{"error":"metrics snapshot unavailable"}', status=503, content_type="application/json")
            prometheus = request.path == "/metrics"
            return Response(sample[0] if prometheus else sample[1], content_type=CONTENT_TYPE_LATEST if prometheus else "application/json")

        @app.after_request
        def uncached(response: Response) -> Response:
            response.headers["Cache-Control"] = "no-store"
            return response

        return app
