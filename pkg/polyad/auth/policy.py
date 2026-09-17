"""
Resolve endpoint permissions and inject only declared Kubernetes Secret references.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from polyad_types.auth import Authentication
from polyad_types.codec import from_dict

if TYPE_CHECKING:
    from typing import Any


LISTENERS = {
    "composition": {"composition", "activations", "throughput"},
    "events": {"events", "topology", "discovery"},
    "metrics": {"metrics"},
    "observations": {"observations"},
}


def public_demo() -> bool:
    """
    Read the explicit demonstration override without changing production defaults.

    Returns:
        bool: Whether all HTTP credential and request-lane checks are disabled.
    """
    return os.environ.get("POLYAD_AUTH_MODE", "Required") == "Disabled"


def endpoint_scope(listener: str, path: str) -> str:
    """
    Separate permissions for routes hosted by the same Flask listener.

    Args:
        listener (str): Logical composition, events, metrics or observation listener.
        path (str): Request path without query parameters.

    Returns:
        str: Capability required by this route.
    """
    if listener == "composition":
        if path.startswith("/v1/activations"):
            return "activations"
        if path.startswith("/v1/throughput"):
            return "throughput"
    if listener == "events" and path.startswith("/v1/graphs/"):
        return "topology"
    if listener == "events" and path == "/v1/discovery":
        return "discovery"
    return listener


def inject_credentials(pod: dict[str, Any], definition: dict[str, Any], cluster: str) -> None:
    """
    Assign namespace-local Secret references without reading or caching their contents.

    Args:
        pod (dict[str, Any]): Mutable compiled Pod template.
        definition (dict[str, Any]): Referenced Workload or Daemon definition.
        cluster (str): Actual execution cluster identity.

    Returns:
        None: Selected containers receive valueFrom entries, never literal credentials.
    """
    filename = os.environ.get("POLYAD_WORKLOAD_CREDENTIALS_FILE")
    if not filename:
        return
    registry = from_dict(json.loads(Path(filename).read_text()), Authentication)
    local_cluster = os.environ.get("POLYAD_CLUSTER_NAME", "")
    meta = definition["metadata"]
    containers = pod["spec"]["containers"]
    names = {container["name"] for container in containers}
    for group in (registry.services, registry.operators):
        for key in group:
            for assignment in key.workloads:
                if (
                    assignment.kind != definition["kind"]
                    or assignment.name != meta["name"]
                    or assignment.namespace != meta["namespace"]
                    or (assignment.cluster or local_cluster) != cluster
                ):
                    continue
                if set(assignment.containers) - names:
                    raise ValueError("credential assignment names an absent container")
                for container in containers:
                    if assignment.containers and container["name"] not in assignment.containers:
                        continue
                    environment = container.setdefault("env", [])
                    desired = {
                        "name": assignment.env,
                        "valueFrom": {"secretKeyRef": {"name": assignment.secret or key.existingSecret, "key": key.secretKey}},
                    }
                    existing = next((entry for entry in environment if entry.get("name") == assignment.env), None)
                    if existing is not None and existing != desired:
                        raise ValueError("credential assignment conflicts with an existing environment variable")
                    if existing is None:
                        environment.append(desired)
