"""
Expose graph placement and execution identities inside managed workload containers.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING

from polyad_types.resources import GROUP

if TYPE_CHECKING:
    from typing import Any

    from polyad_types.graphs.topology import Node

POD_FIELDS = {
    "POLYAD_POD_NAME": "metadata.name",
    "POLYAD_POD_UID": "metadata.uid",
    "POLYAD_POD_NAMESPACE": "metadata.namespace",
    "POLYAD_POD_IP": "status.podIP",
    "POLYAD_POD_IPS": "status.podIPs",
    "POLYAD_HOST_IP": "status.hostIP",
    "POLYAD_HOST_IPS": "status.hostIPs",
    "POLYAD_KUBERNETES_NODE_NAME": "spec.nodeName",
    "POLYAD_SERVICE_ACCOUNT_NAME": "spec.serviceAccountName",
}
RESOURCE_FIELDS = {
    "POLYAD_CPU_REQUEST_MILLICORES": ("requests.cpu", "1m"),
    "POLYAD_CPU_LIMIT_MILLICORES": ("limits.cpu", "1m"),
    "POLYAD_MEMORY_REQUEST_BYTES": ("requests.memory", "1"),
    "POLYAD_MEMORY_LIMIT_BYTES": ("limits.memory", "1"),
}


def pod_environment() -> list[dict[str, Any]]:
    """
    Describe local Kubernetes context, resolved by each container's own kubelet.

    Returns:
        list[dict[str, Any]]: Downward API identity, placement and resource environment selectors.
    """
    fields = [{"name": name, "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": path}}} for name, path in POD_FIELDS.items()]
    fields.extend(
        {"name": name, "valueFrom": {"resourceFieldRef": {"resource": resource, "divisor": divisor}}}
        for name, (resource, divisor) in RESOURCE_FIELDS.items()
    )
    return fields


def inject_environment(pod: dict[str, Any], values: dict[str, str]) -> None:
    """
    Prepend authoritative identity fields while preserving unrelated workload variables.

    Args:
        pod (dict[str, Any]): Mutable Pod template with a spec.
        values (dict[str, str]): Compiler-supplied literal environment values.

    Returns:
        None: All declared regular and init containers are updated in place.
    """
    managed: list[dict[str, Any]] = [{"name": name, "value": value.replace("$", "$$")} for name, value in values.items()]
    managed.extend(pod_environment())
    names = set(values) | POD_FIELDS.keys() | RESOURCE_FIELDS.keys()
    for group in ("containers", "initContainers"):
        for container in pod["spec"].get(group) or []:
            container["env"] = copy.deepcopy(managed) + [item for item in container.get("env") or [] if item["name"] not in names]


def workload_identity(
    ancestors: list[dict[str, Any]], node: Node, definition: dict[str, Any], resource_name: str, endpoints: dict[str, str]
) -> dict[str, str]:
    """
    Compile immutable identities from a refreshed root-to-containing-graph chain.

    Args:
        ancestors (list[dict[str, Any]]): Root first, containing graph last, with verified owner UIDs.
        node (Node): Logical workload vertex in the containing graph.
        definition (dict[str, Any]): Reusable workload definition and its current incarnation.
        resource_name (str): Generated native Job, Deployment or StatefulSet name.
        endpoints (dict[str, str]): Enabled operator URLs, keyed by API, EVENTS, METRICS or CONNECTIONS.

    Returns:
        dict[str, str]: Stable environment contract; unavailable optional identities are empty.
    """
    graph, root = ancestors[-1], ancestors[0]
    meta, source = graph["metadata"], definition["metadata"]
    ancestry = [{"kind": item["kind"], **{key: item["metadata"][key] for key in ("namespace", "name", "uid")}} for item in ancestors]
    annotations = {}
    for item in ancestors:
        annotations.update(item["metadata"].get("annotations", {}))
    graph_annotations = meta.get("annotations", {})
    path = graph_annotations.get(f"{GROUP}/node-path", graph_annotations.get(f"{GROUP}/object-id"))
    if not path:
        path = "/".join(
            [root["metadata"]["name"]]
            + [item["metadata"].get("labels", {}).get(f"{GROUP}/node", item["metadata"]["name"]) for item in ancestors[1:]]
        )
    values = {
        "POLYAD_GRAPH_NAME": meta["name"],
        "POLYAD_GRAPH_KIND": graph["kind"],
        "POLYAD_GRAPH_UID": meta["uid"],
        "POLYAD_GRAPH_NAMESPACE": meta["namespace"],
        "POLYAD_ROOT_GRAPH_NAME": root["metadata"]["name"],
        "POLYAD_ROOT_GRAPH_KIND": root["kind"],
        "POLYAD_ROOT_GRAPH_UID": root["metadata"]["uid"],
        "POLYAD_GRAPH_ANCESTRY": json.dumps(ancestry, separators=(",", ":")),
        "POLYAD_NODE_NAME": node.name,
        "POLYAD_NODE_ID": node.id or node.name,
        "POLYAD_NODE_PATH": f"{path}/{node.id or node.name}",
        "POLYAD_RUNTIME_NODE_NAME": node.name,
        "POLYAD_DEFINITION_NAME": source["name"],
        "POLYAD_DEFINITION_KIND": definition["kind"],
        "POLYAD_DEFINITION_UID": source["uid"],
        "POLYAD_DEFINITION_GENERATION": str(source.get("generation", 1)),
        "POLYAD_RESOURCE_NAME": resource_name,
        "POLYAD_RESOURCE_KIND": definition["spec"].get("controller", "Deployment") if node.kind == "Daemon" else "Job",
        "POLYAD_REQUEST_ID": annotations.get(f"{GROUP}/request-id", ""),
        "POLYAD_COMPOSITION_UID": annotations.get(f"{GROUP}/composition-uid", ""),
        "POLYAD_ACTIVATION_ID": annotations.get(f"{GROUP}/activation-id", ""),
        "POLYAD_ACTIVATION_UID": annotations.get(f"{GROUP}/activation-uid", ""),
    }
    values.update({f"POLYAD_{name}_URL": endpoints.get(name, "") for name in ("API", "EVENTS", "METRICS", "CONNECTIONS")})
    return values
