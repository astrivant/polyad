"""
Build a read-only telemetry API independent of composition and event workers.
"""

from __future__ import annotations

import hmac
import json

from apispec import APISpec
from attrs import evolve, frozen
from flask import Flask, Response, request
from prometheus_client import CONTENT_TYPE_LATEST

from polyad.metrics.store import MetricsStore
from polyad.metrics.workloads import workload_metric
from polyad.operator.health import lifecycle
from polyad.operator.pressure import demand


@frozen
class MetricsAPIBuilder:
    """
    Compose an HTTP API around a cached metrics source.

    Attributes:
        store (MetricsStore | None): Snapshot source populated by the operator loop.
        token (str | None): Optional dedicated read-only bearer credential.
    """

    store: MetricsStore | None = None
    token: str | None = None

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

    def build(self) -> Flask:
        """
        Expose Prometheus, JSON and OpenAPI endpoints on a dedicated service.

        Returns:
            Flask: Read-only application with no scrape-time external I/O.
        """
        if self.store is None:
            raise ValueError("metrics API requires a snapshot store")
        if self.token is not None:
            self.with_bearer_token(self.token)
        store = self.store
        app = Flask(__name__)
        spec = APISpec(title="Polyad metrics API", version="v1alpha1", openapi_version="3.0.3")
        if self.token is not None:
            spec.components.security_scheme("bearerAuth", {"type": "http", "scheme": "bearer"})
            spec.options["security"] = [{"bearerAuth": []}]

        @app.before_request
        def authenticate() -> Response | None:
            if self.token is not None and not hmac.compare_digest(
                request.headers.get("Authorization", "").encode(), f"Bearer {self.token}".encode()
            ):
                return Response(
                    '{"error":"unauthorized"}', status=401, content_type="application/json", headers={"WWW-Authenticate": "Bearer"}
                )
            return None

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

        for path in ("/v1/postgresql/connections", "/v1/components/{component}/{metric}"):
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
                    postgres = snapshot.get("postgresql", {})
                    if not postgres.get("enabled") or not postgres.get("fresh"):
                        raise ValueError("PostgreSQL connection observation unavailable")
                    value = postgres["connections"]
            except (KeyError, ValueError) as error:
                return Response(
                    json.dumps({"error": str(error)}), status=404 if isinstance(error, KeyError) else 503, content_type="application/json"
                )
            return Response(json.dumps({"value": value, "fresh": True}), content_type="application/json")

        @app.get("/openapi.json")
        def openapi() -> Response:
            document = spec.to_dict()
            if self.token is not None:
                for item in document["paths"].values():
                    item["get"]["responses"]["401"] = {"description": "Missing or invalid metrics bearer credential"}
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

        return app
