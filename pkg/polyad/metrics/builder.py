"""
Register read-only telemetry routes on the shared HTTP application.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apispec import APISpec
from attrs import evolve, frozen
from flask import Response, request
from prometheus_client import CONTENT_TYPE_LATEST

from polyad.api.application import Routes
from polyad.auth.http import Access, install
from polyad.metrics.store import MetricsStore
from polyad.metrics.workloads import workload_metric
from polyad.operator.health import lifecycle
from polyad.operator.pressure import demand

if TYPE_CHECKING:
    from flask import Flask


@frozen
class MetricsAPIBuilder:
    """
    Compose an HTTP API around a cached metrics source.

    Attributes:
        store (MetricsStore | None): Snapshot source populated by the operator loop.
        token (str | None): Optional dedicated read-only bearer credential.
        access (Access | None): Named read credentials with shared lane limits.
    """

    store: MetricsStore | None = None
    token: str | None = None
    access: Access | None = None

    def with_store(self, store: MetricsStore) -> MetricsAPIBuilder:
        """
        Bind a snapshot source without mutating this builder.

        Args:
            store (MetricsStore): Thread-safe metrics publication store.

        Returns:
            MetricsAPIBuilder: Builder with the supplied source.
        """
        return evolve(self, store=store)

    def with_bearer_token(self, token: str) -> MetricsAPIBuilder:
        """
        Require a dedicated read-only credential for every metrics route.

        Args:
            token (str): Nonempty printable bearer credential without whitespace.

        Returns:
            MetricsAPIBuilder: Builder with authentication enabled.
        """
        if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("metrics authentication requires a nonempty printable bearer token without whitespace")
        return evolve(self, token=token)

    def build(self, application: Flask | None = None) -> Flask:
        """
        Expose Prometheus, JSON and OpenAPI endpoints on a dedicated service.

        Args:
            application (Flask | None): Shared application, or None for standalone use.

        Returns:
            Flask: Cached telemetry application with optional shared credential admission.
        """
        if self.store is None:
            raise ValueError("metrics API requires a snapshot store")
        if self.token is not None:
            self.with_bearer_token(self.token)
        store = self.store
        app = Routes("metrics", application, max_body=1024)
        spec = APISpec(title="Polyad metrics API", version="v1alpha1", openapi_version="3.0.3")
        authenticated = install(app, "metrics", self.token, self.access)
        if authenticated:
            spec.components.security_scheme("bearerAuth", {"type": "http", "scheme": "bearer"})
            spec.options["security"] = [{"bearerAuth": []}]

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

        spec.path(
            path="/v1/workloads/{kind}/{name}/{metric}",
            operations={
                "get": {
                    "parameters": [
                        {"name": key, "in": "path", "required": True, "schema": {"type": "string"}} for key in ("kind", "name", "metric")
                    ]
                    + [{"name": key, "in": "query", "schema": {"type": "string"}} for key in ("node", "cluster")],
                    "responses": {
                        "200": {
                            "description": "Fresh workload metric; reusable definitions aggregate all observed uses",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["value", "fresh"],
                                        "properties": {"value": {"type": "number"}, "fresh": {"type": "boolean"}},
                                    }
                                }
                            },
                        },
                        "404": {"description": "Unknown object, node or metric"},
                        "503": {"description": "Stale, unavailable or retiring"},
                    },
                }
            },
        )

        @app.get("/v1/workloads/<kind>/<name>/<metric>")
        def workload(kind: str, name: str, metric: str) -> Response:
            sample = store.read()
            if sample is None or lifecycle.replacement.is_set() or lifecycle.draining.is_set():
                return Response('{"error":"metrics snapshot unavailable"}', status=503, content_type="application/json")
            try:
                snapshot = json.loads(sample[1])
                cluster = request.args.get("cluster")
                if cluster is not None:
                    snapshot = snapshot.get("clusters", {})[cluster]
                value = workload_metric(snapshot, kind, name, metric, request.args.get("node"))
                if cluster is not None:
                    value["cluster"] = cluster
            except (KeyError, ValueError) as error:
                return Response(
                    json.dumps({"error": str(error)}), status=404 if isinstance(error, KeyError) else 503, content_type="application/json"
                )
            return Response(json.dumps(value), content_type="application/json")

        for path in ("/v1/postgresql/connections", "/v1/dragonfly/connections", "/v1/components/{component}/{metric}"):
            spec.path(
                path=path,
                operations={
                    "get": {
                        "parameters": [
                            {"name": key, "in": "path", "required": True, "schema": {"type": "string"}}
                            for key in ("component", "metric")
                            if "{" + key + "}" in path
                        ],
                        "responses": {
                            "200": {"description": "Fresh global demand", "content": {"application/json": {"schema": {"type": "object"}}}},
                            "404": {"description": "Unknown component metric"},
                            "503": {"description": "Sample unavailable, disabled or stale"},
                        },
                    }
                },
            )

        @app.get("/v1/postgresql/connections")
        @app.get("/v1/dragonfly/connections")
        @app.get("/v1/components/<component>/<metric>")
        def capacity(component: str | None = None, metric: str | None = None) -> Response:
            sample = store.read()
            if sample is None or lifecycle.replacement.is_set() or lifecycle.draining.is_set():
                return Response('{"error":"metrics snapshot unavailable"}', status=503, content_type="application/json")
            snapshot = json.loads(sample[1])
            try:
                if component is not None and metric is not None:
                    value = demand(snapshot, component, metric)
                else:
                    backend = request.path.split("/")[2]
                    observation = snapshot.get(backend, {})
                    if not observation.get("enabled") or not observation.get("fresh"):
                        raise ValueError(f"{backend} connection observation unavailable")
                    value = observation["connections"]
            except (KeyError, ValueError) as error:
                return Response(
                    json.dumps({"error": str(error)}), status=404 if isinstance(error, KeyError) else 503, content_type="application/json"
                )
            return Response(json.dumps({"value": value, "fresh": True}), content_type="application/json")

        @app.get("/openapi.json")
        def openapi() -> Response:
            document = spec.to_dict()
            if authenticated:
                for item in document["paths"].values():
                    item["get"]["responses"]["401"] = {"description": "Missing or invalid metrics bearer credential"}
                    item["get"]["responses"]["403"] = {"description": "Credential does not authorize metrics"}
                    item["get"]["responses"]["429"] = {"description": "Credential rate or concurrency budget exhausted"}
            return Response(json.dumps(document), content_type="application/json")

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

        return app.finish()
