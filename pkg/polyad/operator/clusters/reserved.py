"""
Observe externally managed operator workloads and services inside the reserved hierarchy.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from polyad.events.visibility import INTERNAL
from polyad.operator.observability.graph_status import observed
from polyad.operator.policies.rule_state import check_live_rules
from polyad_types.resources import GROUP
from polyad_types.topology import topology

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad.operator.reconciliation.controller import Controller

DEPLOYMENT = f"{GROUP}/observed-operator-deployment"
RESOURCES = f"{GROUP}/observed-local-services"
KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Service", "Dragonfly", "Cluster"})


def validate_bindings(bindings: dict[str, Any]) -> None:
    """
    Restrict service observations to named, non-secret infrastructure resources.

    Args:
        bindings (dict[str, Any]): Native resource identities indexed by graph node.

    Returns:
        None: Invalid identities raise ValueError before Kubernetes is contacted.
    """
    if not isinstance(bindings, dict) or not 1 <= len(bindings) <= 256:
        raise ValueError("service observations require between one and 256 bindings")
    for target in bindings.values():
        if (
            not isinstance(target, dict)
            or set(target) != {"kind", "namespace", "name"}
            or not isinstance(target["kind"], str)
            or target["kind"] not in KINDS
        ):
            raise ValueError("unsupported service observation target")
        for field, maximum in (("namespace", 63), ("name", 253)):
            value = target[field]
            pattern = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?" if field == "namespace" else r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?"
            if not isinstance(value, str) or not 1 <= len(value) <= maximum or not re.fullmatch(pattern, value):
                raise ValueError(f"invalid service observation {field}")


async def service_members(api: API, obj: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Refresh every declared local service without acquiring native lifecycle ownership.

    Args:
        api (API): Cluster reader with narrowly scoped observation permissions.
        obj (dict[str, Any]): Internal observation Graph, including its named bindings.

    Returns:
        list[dict[str, Any]]: Read copies labeled for graph metrics; absent targets remain pending.
    """
    meta, spec = obj["metadata"], obj["spec"]
    bindings = json.loads(meta["annotations"][RESOURCES])
    validate_bindings(bindings)
    graph = topology(spec, obj["kind"])
    if (
        obj["kind"] != "Graph"
        or meta.get("labels", {}).get(INTERNAL) != "true"
        or DEPLOYMENT in meta["annotations"]
        or graph.mode != "persistent"
        or graph.throughput is not None
        or graph.traffic
        or spec.get("network")
        or spec.get("activation")
        or {node.name for node in graph.nodes} != bindings.keys()
        or any(node.kind != "Resource" or node.ref != bindings[node.name]["name"] or node.requires or node.gate for node in graph.nodes)
    ):
        raise ValueError("service observations require an internal persistent Graph with exactly its bound Resource nodes")
    result = []
    for node, target in sorted(bindings.items()):
        native = await api.get(target["kind"], target["namespace"], target["name"])
        if native is not None:
            native["metadata"]["labels"] = {**native["metadata"].get("labels", {}), INTERNAL: "true", f"{GROUP}/node": node}
            result.append(native)
    return result


async def members(api: API, obj: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Read a reserved group's Deployment without acquiring lifecycle ownership.

    Args:
        api (API): Reader for the group's cluster and namespace.
        obj (dict[str, Any]): Reserved Graph with its operator Deployment binding.

    Returns:
        list[dict[str, Any]]: Observation copies labeled for graph metrics, never for mutation.
    """
    meta = obj["metadata"]
    if RESOURCES in meta.get("annotations", {}):
        return await service_members(api, obj)
    name = meta.get("annotations", {}).get(DEPLOYMENT)
    if not name:
        return []
    graph = topology(obj["spec"], obj["kind"])
    if (
        obj["kind"] != "Graph"
        or meta.get("labels", {}).get(INTERNAL) != "true"
        or len(graph.nodes) != 1
        or graph.nodes[0].kind != "Daemon"
        or graph.mode != "persistent"
        or graph.throughput is not None
        or graph.traffic
    ):
        raise ValueError("operator Deployment observations require an internal persistent single-Daemon Graph")
    deployment = await api.get("Deployment", meta["namespace"], name)
    if deployment is None:
        return []
    if deployment["metadata"].get("labels", {}).get(INTERNAL) != "true":
        raise ValueError("reserved operator Graph cannot observe a non-internal Deployment")
    # Only the returned observation is decorated. The native Deployment retains
    # Helm or OperatorPool ownership and is never adopted or deleted by this Graph.
    deployment["metadata"]["labels"] = {
        **deployment["metadata"].get("labels", {}),
        f"{GROUP}/node": graph.nodes[0].name,
    }
    return [deployment]


async def reconcile(controller: Controller, obj: dict[str, Any]) -> None:
    """
    Publish fresh operator group readiness while preserving its bootstrap owner.

    Args:
        controller (Controller): Existing guarded graph controller.
        obj (dict[str, Any]): Reserved observation Graph instance.

    Returns:
        None: Only the Graph's status is changed.
    """
    children = await members(controller.api, obj)
    reports = await check_live_rules(controller.api, obj)
    nodes = obj["spec"]["nodes"]
    suspended = obj["spec"].get("suspend", False)
    by_node = {child["metadata"]["labels"][f"{GROUP}/node"]: child for child in children}
    states = {name: observed(child) for name, child in by_node.items()} if not suspended else {}
    ready = len(states) == len(nodes) and all(state["ready"] for state in states.values())
    failed = any(state["failed"] for state in states.values())
    await controller.status(
        obj,
        {
            "phase": "Suspended" if suspended else "Failed" if failed else "Ready" if ready else "Waiting",
            "ready": ready,
            "completed": False,
            "failed": failed,
            "observedGeneration": obj["metadata"].get("generation", 1),
            "nodes": states,
            "structuralRules": reports,
            "workloads": {
                node["name"]: {
                    "kind": node["kind"],
                    "definition": node["ref"],
                    "values": {
                        "executions": int(node["name"] in by_node),
                        "readyExecutions": int(states.get(node["name"], {}).get("ready", False)),
                        "replicas": by_node.get(node["name"], {})
                        .get("status", {})
                        .get("replicas", by_node.get(node["name"], {}).get("status", {}).get("instances", 0)),
                        "readyReplicas": by_node.get(node["name"], {})
                        .get("status", {})
                        .get("readyReplicas", by_node.get(node["name"], {}).get("status", {}).get("readyInstances", 0)),
                    },
                }
                for node in nodes
            },
        },
    )
