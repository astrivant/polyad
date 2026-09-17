"""
Expose authenticated graph observations without granting execution authority.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from flask import jsonify

from polyad.api.application import Routes
from polyad.api.errors import Unavailable
from polyad.auth.http import install
from polyad.auth.policy import public_demo
from polyad.events.topology import topology_snapshot
from polyad.metrics.workloads import current_observation
from polyad.operator.adapters.kubernetes import API
from polyad.operator.observability.graph_status import instance_metrics
from polyad_types.resources import BOUNDARY_KINDS

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from flask import Flask, Response

    from polyad.auth.http import Access


class ObservationAPI(API):
    """
    Enforce read-only Kubernetes access in addition to the observer's read-only RBAC.
    """

    async def request(self, method: str, kind: str, namespace: str, name: str = "", body: Any = None, **options: Any) -> Any:
        """
        Reject every mutation before credentials or HTTP transport are used.

        Args:
            method (str): Requested Kubernetes HTTP method.
            kind (str): Kubernetes resource kind.
            namespace (str): Namespace served by the observer.
            name (str): Optional resource name.
            body (Any): Request body, forbidden for read operations.
            **options (Any): Read query parameters forwarded to the common adapter.

        Returns:
            Any: A fresh Kubernetes read response.
        """
        if method != "GET" or body is not None:
            raise ValueError("observers cannot mutate Kubernetes resources")
        return await super().request(method, kind, namespace, name, **options)


async def observe(api: API, cluster: str, namespace: str, kind: str, name: str) -> dict[str, Any] | None:
    """
    Recompute a local snapshot and fence it against changes to the observed graph.

    Args:
        api (API): Read-only Kubernetes adapter.
        cluster (str): Stable identity of this observer's cluster.
        namespace (str): Fixed namespace served by this observer.
        kind (str): Graph, PolyGraph or ReplicaGroup.
        name (str): Namespaced boundary name.

    Returns:
        dict[str, Any] | None: Timestamped observation, or None for an absent graph.
    """
    if kind not in BOUNDARY_KINDS or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", name):
        raise ValueError("observations require a supported graph kind and DNS-label name")
    obj = await api.get(kind, namespace, name)
    if obj is None:
        return None
    children = await api.owned(namespace, obj["metadata"]["uid"])
    metrics = instance_metrics(obj, children)
    topology = await topology_snapshot(api, obj, children)
    current = await api.get(kind, namespace, name)
    meta = obj["metadata"]
    if current is None or any(current["metadata"].get(key) != meta.get(key) for key in ("uid", "resourceVersion")):
        raise Unavailable("graph changed during observation; retry the read")
    return {
        "cluster": cluster,
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "uid": meta["uid"],
        "generation": meta.get("generation", 1),
        "resourceVersion": meta["resourceVersion"],
        "observedAt": datetime.now(UTC).isoformat(),
        "statusObservedAt": obj.get("status", {}).get("metricsObservedAt"),
        "statusCurrent": obj.get("status", {}).get("observedGeneration") == meta.get("generation", 1)
        and current_observation(obj.get("status", {}).get("metricsObservedAt")),
        "metrics": metrics,
        "topology": topology,
    }


def build_app(
    lookup: Callable[[str, str], dict[str, Any] | None], token: str, *, access: Access | None = None, application: Flask | None = None
) -> Flask:
    """
    Build a dedicated observation API with no deployment, scaling or topology mutation routes.

    Args:
        lookup (Callable[[str, str], dict[str, Any] | None]): Fresh namespace-scoped observation callback.
        token (str): Nonempty Secret-backed read credential.
        access (Access | None): Named operator/service read credentials with shared lanes.
        application (Flask | None): Existing process application for blueprint registration.

    Returns:
        Flask: Authenticated, uncached, GET-only graph observation application.
    """
    if (
        not public_demo()
        and not (access and access.supports("observations"))
        and (not token or any(ord(char) < 33 or ord(char) > 126 for char in token))
    ):
        raise ValueError("observers require a nonempty printable bearer token without whitespace")
    app = Routes("observations", application, max_body=1024)

    install(app, "observations", token, access)

    @app.get("/v1/observations/<kind>/<name>")
    def graph(kind: str, name: str) -> tuple[Response, int]:
        try:
            result = lookup(kind, name)
        except ValueError as error:
            return jsonify(error=str(error)), 422
        except Unavailable:
            return jsonify(error="fresh graph observation unavailable"), 503
        return (jsonify(result), 200) if result is not None else (jsonify(error="graph not found"), 404)

    @app.get("/healthz")
    def health() -> Response:
        return jsonify(ready=True, mode="observer")

    @app.get("/openapi.json")
    def openapi() -> Response:
        return jsonify(
            {
                "openapi": "3.0.3",
                "info": {"title": "Polyad graph observations", "version": "v1alpha1"},
                "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
                "security": [] if public_demo() else [{"bearerAuth": []}],
                "paths": {
                    "/v1/observations/{kind}/{name}": {
                        "get": {
                            "parameters": [
                                {"name": key, "in": "path", "required": True, "schema": {"type": "string"}} for key in ("kind", "name")
                            ],
                            "responses": {
                                str(code): {"description": description}
                                for code, description in (
                                    (200, "Fresh cluster-local observation"),
                                    (401, "Invalid read credential"),
                                    (403, "Credential does not authorize observations"),
                                    (404, "Graph absent"),
                                    (422, "Invalid graph reference"),
                                    (429, "Credential rate or concurrency budget exhausted"),
                                    (503, "Fresh observation unavailable"),
                                )
                            },
                        }
                    }
                },
            }
        )

    @app.after_request
    def uncached(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        return response

    return app.finish()
