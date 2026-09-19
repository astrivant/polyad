"""
Summarize fresh graph inventory and generation-fenced nested observations.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from attrs import evolve
from cattrs.errors import BaseValidationError

from polyad.compiler.registry import GRAPH_OWNED_KINDS
from polyad.graph.metrics import measure_topology
from polyad.graph.temporary import overlay
from polyad.operator.observability.rollup import measure_subtree
from polyad_types.graphs.topology import topology
from polyad_types.resources import (
    AUXILIARY_KINDS,
    BOUNDARY_KINDS,
    GROUP,
    ExecutionMetrics,
    GraphMetrics,
    ResourceCounts,
    ResourceMetrics,
    SubgraphMetrics,
    TopologyMetrics,
    converter,
    to_document,
)

if TYPE_CHECKING:
    from typing import Any

BOUNDARIES = BOUNDARY_KINDS


def _metric[T](document: dict[str, Any] | None, model: type[T]) -> T | None:
    return converter.structure(document, model) if document is not None else None


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
        current = status.get("observedGeneration", 0) >= obj["metadata"].get("generation", 1)
        failed = current and any(
            condition.get("type") == "Progressing"
            and condition.get("status") == "False"
            and condition.get("reason") == "ProgressDeadlineExceeded"
            for condition in status.get("conditions", [])
        )
        ready = (
            current
            and not failed
            and status.get("updatedReplicas", 0) == spec.get("replicas", 1)
            and status.get("readyReplicas", 0) >= spec.get("replicas", 1)
            and status.get("availableReplicas", 0) >= spec.get("replicas", 1)
        )
    elif obj["kind"] == "DaemonSet":
        desired = status.get("desiredNumberScheduled", 0)
        ready = bool(
            status.get("observedGeneration", 0) >= obj["metadata"].get("generation", 1)
            and desired > 0
            and status.get("updatedNumberScheduled", 0) == desired
            and status.get("numberReady", 0) == desired
            and status.get("numberAvailable", 0) == desired
            and status.get("numberMisscheduled", 0) == 0
        )
    elif obj["kind"] == "StatefulSet":
        current = status.get("observedGeneration", 0) >= obj["metadata"].get("generation", 1)
        replicas = spec.get("replicas", 1)
        strategy = spec.get("updateStrategy", {})
        partition = strategy.get("rollingUpdate", {}).get("partition", 0)
        expected_updated = max(0, replicas - partition)
        ready = (
            current
            and status.get("replicas", 0) == replicas
            and status.get("readyReplicas", 0) >= replicas
            and (not spec.get("minReadySeconds", 0) or status.get("availableReplicas", 0) >= replicas)
            and (strategy.get("type") == "OnDelete" or status.get("updatedReplicas", 0) >= expected_updated)
        )
    elif obj["kind"] in BOUNDARIES:
        current = status.get("observedGeneration") == obj["metadata"].get("generation", 1)
        ready, completed, failed = (current and status.get(k, False) for k in ("ready", "completed", "failed"))
        failed = failed or (current and status.get("phase") == "Invalid")
    elif obj["kind"] == "PersistentVolumeClaim":
        ready = status.get("phase") == "Bound"
        failed = status.get("phase") == "Lost"
    elif obj["kind"] == "Dragonfly":
        ready = status.get("phase", "").lower() == "ready" and not status.get("isRollingUpdate", False)
    elif obj["kind"] == "Cluster":
        ready = conditions.get("Ready", False) and status.get("readyInstances", 0) == spec.get("instances", 1)
        ready = ready and all(
            condition.get("observedGeneration", obj["metadata"].get("generation", 1)) == obj["metadata"].get("generation", 1)
            for condition in status.get("conditions", [])
            if condition["type"] == "Ready"
        )
    else:
        ready = True
    if obj["metadata"].get("deletionTimestamp"):
        ready = completed = False
    return {"started": not bool(obj["metadata"].get("deletionTimestamp")), "ready": ready, "completed": completed, "failed": failed}


def instance_metrics(obj: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Report boundary-local desired shape, observed work and nested graph freshness.

    Args:
        obj (dict[str, Any]): Latest graph instance document.
        children (list[dict[str, Any]]): Fresh inventory fenced by the parent's UID.

    Returns:
        dict[str, Any]: Stable status metrics without recursively duplicating descendant status.
    """
    return to_document(observe_graph(obj, children))


def observe_graph(obj: dict[str, Any], children: list[dict[str, Any]]) -> GraphMetrics:
    """
    Build the typed status metrics tree from a graph and its owned resources.

    Args:
        obj (dict[str, Any]): Latest graph instance document.
        children (list[dict[str, Any]]): Fresh inventory fenced by the parent's UID.

    Returns:
        GraphMetrics: Local observations and generation-fenced descendant summaries.
    """
    runtime = obj.get("status", {}).get("activationRuntime") or {}
    if runtime.get("generation") == obj["metadata"].get("generation", 1):
        # Metrics use execution aliases; traffic guards retain logical identities.
        base_spec = obj["spec"]
        if obj["kind"] == "ReplicaGroup":
            from polyad_types.graphs.replication import replica_topology

            base_spec = replica_topology(base_spec)
        obj = {
            **obj,
            "spec": {
                **base_spec,
                "nodes": runtime["nodes"],
                "connections": runtime["connections"],
                "network": None,
                "throughput": None,
                "traffic": [],
            },
        }
    else:
        runtime = {}
    if obj["kind"] == "ReplicaGroup" and "template" in obj["spec"]:
        from polyad_types.graphs.replication import replica_topology

        obj = {**obj, "spec": replica_topology(obj["spec"])}
    spec = obj["spec"]
    counts = Counter(child["kind"] for child in children)
    by_kind: dict[str, Any] = {kind: counts[kind] for kind in sorted(GRAPH_OWNED_KINDS | {"Dragonfly", "Cluster"})}
    result = GraphMetrics(
        observedGeneration=obj["metadata"].get("generation", 1),
        resources=ResourceMetrics(
            total=len(children),
            terminating=sum(bool(child["metadata"].get("deletionTimestamp")) for child in children),
            byKind=ResourceCounts(**by_kind),
        ),
    )
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
        if child["kind"] == "ReplicaGroup":
            from polyad.operator.clusters.remote_scaling import remote_revision

            current = current and status.get("remoteScaleRevision", "") == remote_revision(child)
        result.subgraphs.append(
            SubgraphMetrics(
                kind=child["kind"],
                name=meta["name"],
                uid=meta["uid"],
                node=meta.get("labels", {}).get(f"{GROUP}/node", ""),
                phase=status.get("phase", "Unknown"),
                generation=meta.get("generation", 1),
                observedGeneration=metrics.get("observedGeneration"),
                current=bool(current),
                topology=_metric(metrics.get("topology"), TopologyMetrics) if current else None,
                execution=_metric(metrics.get("execution"), ExecutionMetrics) if current else None,
            )
        )
    try:
        graph = topology(overlay(obj, spec), obj["kind"])
    except (ValueError, TypeError, BaseValidationError) as error:
        return evolve(result, topologyError=str(error), rollup=measure_subtree(obj, children, observed, valid=False))
    result = evolve(result, rollup=measure_subtree(obj, children, observed, valid=True), topology=measure_topology(graph))
    by_node = {
        child["metadata"].get("labels", {}).get(f"{GROUP}/runtime-node", child["metadata"].get("labels", {}).get(f"{GROUP}/node")): child
        for child in children
        if child["kind"] not in AUXILIARY_KINDS
    }
    present = {node.name: by_node[node.name] for node in graph.nodes if node.name in by_node}
    states = {name: observed(child) for name, child in present.items()}
    reserved = sum(node.slots for node in graph.nodes if node.name in states and not states[node.name]["completed"])
    return evolve(
        result,
        observedTopology=measure_topology(graph, set(present)),
        execution=ExecutionMetrics(
            observedNodes=len(present),
            pendingNodes=len(graph.nodes) - len(present) - len(set(runtime.get("dormant", [])) - present.keys()),
            activeNodes=sum(state["started"] and not state["completed"] and not state["failed"] for state in states.values()),
            readyNodes=sum(state["ready"] for state in states.values()),
            completedNodes=sum(state["completed"] for state in states.values()),
            failedNodes=sum(state["failed"] for state in states.values()),
            terminatingNodes=sum(bool(child["metadata"].get("deletionTimestamp")) for child in present.values()),
            slotCapacity=graph.slots,
            reservedSlots=reserved,
            availableSlots=max(0, graph.slots - reserved),
        ),
    )
