"""
Fold generation-fenced descendant summaries without duplicating nested resources.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad.compiler.asts import BOUNDARY_KINDS, GROUP, NETWORK_POLICY_KINDS, RollupMetrics, converter, to_document

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

PHASES = ("Reconciling", "Ready", "Running", "Waiting", "Draining", "Suspended", "Stopped", "Completed", "Failed", "Invalid", "Unknown")
COUNTERS = (
    "leafNodes",
    "observedLeafNodes",
    "pendingLeafNodes",
    "activeLeafNodes",
    "readyLeafNodes",
    "completedLeafNodes",
    "failedLeafNodes",
    "terminatingLeafNodes",
    "resourceCount",
    "terminatingResources",
)


def subtree_metrics(
    obj: dict[str, Any], children: list[dict[str, Any]], observe: Callable[[dict[str, Any]], dict[str, bool]], *, valid: bool
) -> dict[str, Any]:
    """
    Combine direct leaf observations with each immediate boundary's subtree once.

    Args:
        obj (dict[str, Any]): Parent boundary with its latest lifecycle status.
        children (list[dict[str, Any]]): Direct children observed through UID-fenced ownership.
        observe (Callable[[dict[str, Any]], dict[str, bool]]): Resource lifecycle predicate evaluator.
        valid (bool): Whether the local desired topology passed validation.

    Returns:
        dict[str, Any]: Descendant counters and explicit completeness for unobserved subtrees.
    """
    return to_document(measure_subtree(obj, children, observe, valid=valid))


def measure_subtree(
    obj: dict[str, Any], children: list[dict[str, Any]], observe: Callable[[dict[str, Any]], dict[str, bool]], *, valid: bool
) -> RollupMetrics:
    """
    Build a typed recursive summary after checking raw child observation completeness.

    Args:
        obj (dict[str, Any]): Parent boundary with its latest lifecycle status.
        children (list[dict[str, Any]]): Direct children observed through UID-fenced ownership.
        observe (Callable[[dict[str, Any]], dict[str, bool]]): Resource lifecycle predicate evaluator.
        valid (bool): Whether the local desired topology passed validation.

    Returns:
        RollupMetrics: Recursive counters fenced by the observed generations.
    """
    generation = obj["metadata"].get("generation", 1)
    status = obj.get("status", {})
    phase = status.get("phase", "Unknown") if status.get("observedGeneration") == generation else "Unknown"
    phase = phase if phase in PHASES else "Unknown"
    result: dict[str, Any] = {
        "scope": "subtree",
        "observedGeneration": generation,
        "observationsComplete": valid,
        "graphCount": 1,
        "unobservedGraphs": 0,
        "nestingDepth": 1,
        "graphsByPhase": {name: int(name == phase) for name in PHASES},
        **dict.fromkeys(COUNTERS, 0),
    }
    spec = obj.get("spec", {})
    # Feedback's template describes the epoch child, not a second set of leaf work.
    nodes = spec.get("nodes", []) if obj["kind"] != "Feedback" and valid else []
    by_node = {
        child["metadata"].get("labels", {}).get(f"{GROUP}/node"): child for child in children if child["kind"] not in NETWORK_POLICY_KINDS
    }
    for node in nodes:
        if node["kind"] in BOUNDARY_KINDS:
            if node["name"] not in by_node:
                result["unobservedGraphs"] += 1
            continue
        result["leafNodes"] += 1
        child = by_node.get(node["name"])
        if child is None:
            result["pendingLeafNodes"] += 1
            continue
        state = observe(child)
        result["observedLeafNodes"] += 1
        result["activeLeafNodes"] += int(state["started"] and not state["completed"] and not state["failed"])
        for predicate in ("ready", "completed", "failed"):
            result[f"{predicate}LeafNodes"] += int(state[predicate])
        result["terminatingLeafNodes"] += int(bool(child["metadata"].get("deletionTimestamp")))
    result["resourceCount"] = len(children)
    result["terminatingResources"] = sum(bool(child["metadata"].get("deletionTimestamp")) for child in children)
    for child in children:
        if child["kind"] not in BOUNDARY_KINDS:
            continue
        meta, child_status = child["metadata"], child.get("status", {})
        metrics = child_status.get("metrics") or {}
        rollup = metrics.get("rollup") or {}
        current = (
            child_status.get("observedGeneration") == meta.get("generation", 1)
            and metrics.get("observedGeneration") == meta.get("generation", 1)
            and rollup.get("observedGeneration") == meta.get("generation", 1)
            and not meta.get("deletionTimestamp")
            and all(key in rollup for key in (*COUNTERS, "graphCount", "unobservedGraphs", "nestingDepth", "observationsComplete"))
            and all(phase in rollup.get("graphsByPhase", {}) for phase in PHASES)
        )
        if not current:
            result["graphCount"] += 1
            result["unobservedGraphs"] += 1
            result["graphsByPhase"]["Unknown"] += 1
            result["nestingDepth"] = max(result["nestingDepth"], 2)
            continue
        result["graphCount"] += rollup["graphCount"]
        result["unobservedGraphs"] += rollup["unobservedGraphs"]
        result["nestingDepth"] = max(result["nestingDepth"], 1 + rollup["nestingDepth"])
        result["observationsComplete"] &= rollup["observationsComplete"]
        for counter in COUNTERS:
            result[counter] += rollup[counter]
        for name in PHASES:
            result["graphsByPhase"][name] += rollup["graphsByPhase"][name]
    result["observationsComplete"] &= result["unobservedGraphs"] == 0
    return converter.structure(result, RollupMetrics)
