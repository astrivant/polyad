"""Summarize fresh graph inventory and generation-fenced nested observations."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from cattrs.errors import BaseValidationError

from polyad.compiler.asts import BOUNDARY_KINDS, GROUP
from polyad.graph.metrics import topology_metrics
from polyad.graph.topology import topology
from polyad.operator.rollup import subtree_metrics

if TYPE_CHECKING:
    from typing import Any

BOUNDARIES = BOUNDARY_KINDS


def observed(obj: dict[str, Any]) -> dict[str, bool]:
    """
    Distinguish creation, readiness, completion and failure without stale rollout readiness.

    Args:
        obj (dict[str, Any]): Resource document from the latest API observation.

    Returns:
        dict[str, bool]: Started, ready, completed and failed lifecycle predicates.
    """
    status, spec = obj.get("status", {}), obj.get("spec", {})
    conditions = {c["type"]: c["status"] == "True" for c in status.get("conditions", [])}
    ready = completed = failed = False
    if obj["kind"] == "Job":
        completed, failed = conditions.get("Complete", False), conditions.get("Failed", False)
        ready = bool(status.get("active", 0)) or completed
    elif obj["kind"] == "Deployment":
        ready = (
            status.get("observedGeneration", 0) >= obj["metadata"].get("generation", 1)
            and status.get("updatedReplicas", 0) == spec.get("replicas", 1)
            and status.get("readyReplicas", 0) >= spec.get("replicas", 1)
            and status.get("availableReplicas", 0) >= spec.get("replicas", 1)
        )
    elif obj["kind"] in BOUNDARIES:
        current = status.get("observedGeneration") == obj["metadata"].get("generation", 1)
        ready, completed, failed = (current and status.get(k, False) for k in ("ready", "completed", "failed"))
        failed = failed or (current and status.get("phase") == "Invalid")
    elif obj["kind"] == "PersistentVolumeClaim":
        ready = status.get("phase") == "Bound"
    else:
        ready = True
    if obj["metadata"].get("deletionTimestamp"):
        ready = completed = False
    return {"started": not bool(obj["metadata"].get("deletionTimestamp")), "ready": ready, "completed": completed, "failed": failed}


def instance_metrics(obj: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Report boundary-local desired shape, observed work and nested graph freshness.

    Args:
        obj (dict[str, Any]): Latest graph or Feedback instance document.
        children (list[dict[str, Any]]): Fresh inventory fenced by the parent's UID.

    Returns:
        dict[str, Any]: Stable status metrics without recursively duplicating descendant status.
    """
    spec = obj["spec"]["graph"] if obj["kind"] == "Feedback" else obj["spec"]
    result: dict[str, Any] = {
        "scope": "boundary",
        "observedGeneration": obj["metadata"].get("generation", 1),
        "topology": None,
        "observedTopology": None,
        "execution": None,
        "topologyError": "",
        "resources": {
            "total": len(children),
            "terminating": sum(bool(child["metadata"].get("deletionTimestamp")) for child in children),
            "byKind": {
                kind: sum(child["kind"] == kind for child in children)
                for kind in (
                    "Job",
                    "Deployment",
                    "Service",
                    "ConfigMap",
                    "PersistentVolumeClaim",
                    "Graph",
                    "EphemeralGraph",
                    "Feedback",
                    "PolyGraph",
                )
            },
        },
        "subgraphs": [],
    }
    for child in sorted(children, key=lambda item: (item["kind"], item["metadata"]["name"])):
        if child["kind"] not in BOUNDARIES:
            continue
        meta, status = child["metadata"], child.get("status", {})
        metrics = status.get("metrics") or {}
        current = (
            not meta.get("deletionTimestamp")
            and metrics.get("observedGeneration") == meta.get("generation", 1)
            and status.get("observedGeneration") == meta.get("generation", 1)
        )
        result["subgraphs"].append(
            {
                "kind": child["kind"],
                "name": meta["name"],
                "uid": meta["uid"],
                "node": meta.get("labels", {}).get(f"{GROUP}/node", ""),
                "phase": status.get("phase", "Unknown"),
                "generation": meta.get("generation", 1),
                "observedGeneration": metrics.get("observedGeneration"),
                "current": bool(current),
                "topology": copy.deepcopy(metrics.get("topology")) if current else None,
                "execution": copy.deepcopy(metrics.get("execution")) if current else None,
            }
        )
    try:
        graph = topology(spec, obj["spec"].get("kind", "Graph") if obj["kind"] == "Feedback" else obj["kind"])
    except (ValueError, TypeError, BaseValidationError) as error:
        result["topologyError"] = str(error)
        result["rollup"] = subtree_metrics(obj, children, observed, valid=False)
        return result
    result["rollup"] = subtree_metrics(obj, children, observed, valid=True)
    result["topology"] = topology_metrics(graph)
    if obj["kind"] == "Feedback":
        epoch = f"epoch-{obj.get('status', {}).get('epoch', 0)}"
        current_epoch = next((child for child in result["subgraphs"] if child["node"] == epoch and child["current"]), None)
        if current_epoch:
            # The epoch graph owns the workload inventory. Never aggregate historical epochs.
            result["execution"] = current_epoch["execution"]
            instance = next(child for child in children if child["metadata"]["uid"] == current_epoch["uid"])
            result["observedTopology"] = copy.deepcopy(instance["status"]["metrics"].get("observedTopology"))
        return result
    by_node = {child["metadata"].get("labels", {}).get(f"{GROUP}/node"): child for child in children}
    present = {node.name: by_node[node.name] for node in graph.nodes if node.name in by_node}
    states = {name: observed(child) for name, child in present.items()}
    reserved = sum(node.slots for node in graph.nodes if node.name in states and not states[node.name]["completed"])
    result["observedTopology"] = topology_metrics(graph, set(present))
    result["execution"] = {
        "observedNodes": len(present),
        "pendingNodes": len(graph.nodes) - len(present),
        "activeNodes": sum(state["started"] and not state["completed"] and not state["failed"] for state in states.values()),
        "readyNodes": sum(state["ready"] for state in states.values()),
        "completedNodes": sum(state["completed"] for state in states.values()),
        "failedNodes": sum(state["failed"] for state in states.values()),
        "terminatingNodes": sum(bool(child["metadata"].get("deletionTimestamp")) for child in present.values()),
        "slotCapacity": graph.slots,
        "reservedSlots": reserved,
        "availableSlots": max(0, graph.slots - reserved),
    }
    return result
