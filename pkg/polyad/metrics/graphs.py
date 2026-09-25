"""
Export precomputed graph diagnostics without graph analysis or external I/O during publication.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

__all__ = (
    "HELP",
    "graph_rows",
    "numeric_values",
)


HELP = {
    "graph_topology": "Precomputed declared and observed boundary topology dimensions, including layers and components.",
    "graph_execution": "Current boundary execution and recursive rollup observations; distinguish scope before aggregating.",
    "graph_policy_current": "Whether a saved admission report matches the graph and current policy identity and generation.",
    "graph_policy_allowed": "Verdict of the most recent current successful admission policy evaluation.",
    "graph_policy_measurement": "Structural measurements of the live boundary used for admission, by policy and relation.",
    "graph_policy_parameter": "Configured numeric structural and spectral bounds; absent bounds are omitted.",
    "graph_policy_shape": "Calculated shape predicates; one means the predicate holds.",
    "graph_spectrum": "Precomputed undirected adjacency and Laplacian spectral summaries.",
    "graph_eigenvalue": "Sorted precomputed eigenvalues, indexed from zero; requires retained full spectra.",
    "graph_cheeger_input": "Effective Cheeger calculation budgets, operator ceilings and projected input dimensions.",
    "graph_cheeger_result": "Cheeger calculation certificate; constant exists only for exact results, upperBound may be incomplete.",
    "graph_cheeger_calculation_info": "Cheeger calculation completion reason and projection; never treats an incomplete result as exact.",
    "graph_throughput": "Soul searching numeric observations, targets, traffic weights and capacity preparation parameters.",
    "graph_throughput_info": "Soul searching mode, phase and administrator-selected demand signal and unit.",
}


def numeric_values(value: Any, path: str = "") -> Iterator[tuple[str, float]]:
    """
    Flatten finite numeric diagnostics while retaining array indices and omitting unknown values.

    Args:
        value (Any): A preselected diagnostic tree, never arbitrary workload or Secret data.
        path (str): Stable field path within the diagnostic tree.

    Yields:
        (str, float): Numeric field path and finite value; unknown values are omitted.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            yield from numeric_values(child, f"{path}.{key}" if path else key)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from numeric_values(child, f"{path}.{index}")
    elif isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            return
        if math.isfinite(number):
            yield path, number


def graph_rows(snapshot: dict[str, Any]) -> dict[str, list[tuple[dict[str, str], float]]]:
    """
    Collect local and root-held remote graph observations with independent freshness fences.

    Args:
        snapshot (dict[str, Any]): Immutable scheduler inventory and remote observations.

    Returns:
        dict[str, list[tuple[dict[str, str], float]]]: Prometheus rows grouped by metric family.
    """
    rows: dict[str, list[tuple[dict[str, str], float]]] = defaultdict(list)

    def numbers(family: str, labels: dict[str, str], values: Any, dimension: str = "parameter") -> None:
        for key, value in numeric_values(values):
            rows[family].append(({**labels, dimension: key}, value))

    def calculation(labels: dict[str, str], result: dict[str, Any] | None) -> None:
        if not result:
            return
        numbers("graph_cheeger_input", labels, result.get("inputs", {}))
        values = {
            key: result.get(key)
            for key in ("exact", "lowerBound", "upperBound", "edgeChurn", "evaluatedCuts", "skippedPriorityCuts", "durationSeconds")
        }
        if "cut" in result:
            values["cutSize"] = len(result["cut"])
        if result.get("exact") is True:
            values["constant"] = result.get("upperBound")
        numbers("graph_cheeger_result", labels, values, "statistic")

        # Error messages may contain unbounded text; expose only known completion categories.
        reason = result.get("reason", "Unknown").split(":", 1)[0]
        if reason not in {
            "Complete",
            "BoundsSatisfied",
            "MinimumViolated",
            "MaximumViolated",
            "VertexLimit",
            "CutBudget",
            "TimeBudget",
        }:
            reason = "Unknown"
        rows["graph_cheeger_calculation_info"].append(
            (
                {
                    **labels,
                    "reason": reason,
                    "stage": str(result.get("stage", "ExactEnumeration")),
                    "projection": "simple-undirected-without-self-loops",
                },
                1,
            )
        )

    for cluster, source in [("", snapshot), *sorted(snapshot.get("clusters", {}).items())]:
        inventory = source["inventory"]
        if not inventory.get("fresh"):
            continue
        for obj in inventory["objects"]:
            if obj["kind"] not in {"Graph", "PolyGraph", "ReplicaGroup"} or obj["role"] != "instance":
                continue
            root, parent = obj.get("root") or {}, obj.get("parent") or {}
            labels = {
                "cluster": cluster,
                "graph_namespace": source.get("namespace", snapshot["namespace"]),
                "kind": obj["kind"],
                "name": obj["name"],
                "root_kind": root.get("kind", ""),
                "root": root.get("name", ""),
                "parent_kind": parent.get("kind", ""),
                "parent": parent.get("name", ""),
            }
            if obj["statusCurrent"]:
                for view, field in (("declared", "topology"), ("observed", "observedTopology")):
                    numbers("graph_topology", {**labels, "view": view}, obj.get(field), "dimension")
                for scope in ("execution", "rollup"):
                    numbers("graph_execution", {**labels, "scope": scope}, obj.get(scope), "dimension")
            for report in obj.get("structuralPolicies", []):
                policy_labels = {**labels, "policy": report["name"], "relation": report["relation"]}
                current = bool(report.get("current"))
                rows["graph_policy_current"].append((policy_labels, int(current)))
                if not current:
                    continue
                rows["graph_policy_allowed"].append((policy_labels, int(report["allowed"])))
                numbers("graph_policy_measurement", policy_labels, report.get("measurements"), "dimension")
                numbers("graph_policy_parameter", policy_labels, report.get("parameters"))
                numbers("graph_policy_shape", policy_labels, report.get("shapes"), "shape")
                spectrum = report.get("spectrum") or {}
                numbers(
                    "graph_spectrum",
                    policy_labels,
                    {key: spectrum.get(key) for key in ("radius", "connectivity", "largestLaplacian")},
                    "statistic",
                )
                for matrix in ("adjacency", "laplacian"):
                    for index, value in enumerate(spectrum.get(matrix, [])):
                        if isinstance(value, (int, float)) and math.isfinite(value):
                            rows["graph_eigenvalue"].append(({**policy_labels, "matrix": matrix, "index": str(index)}, value))
                calculation({**policy_labels, "source": "policy", "stage": "current", "layout": ""}, report.get("cheegerComputation"))
            throughput = obj.get("throughput")
            if throughput:
                # Certificates have their own families, keeping exact and incomplete semantics explicit.
                numbers(
                    "graph_throughput",
                    labels,
                    {
                        key: value
                        for key, value in throughput.items()
                        if key not in {"computation", "currentComputation", "candidateComputations", "observedGeneration"}
                    },
                )
                rows["graph_throughput_info"].append(
                    ({**labels, **{key: str(throughput.get(key, "")) for key in ("mode", "phase", "demandSignal", "demandUnit")}}, 1)
                )
                calculation(
                    {**labels, "policy": "", "relation": "connections", "source": "throughput", "stage": "current", "layout": ""},
                    throughput.get("currentComputation"),
                )
                for result in throughput.get("candidateComputations", []):
                    calculation(
                        {
                            **labels,
                            "policy": "",
                            "relation": "connections",
                            "source": "throughput",
                            "stage": "candidate",
                            "layout": result["layout"],
                        },
                        result,
                    )
    return rows
