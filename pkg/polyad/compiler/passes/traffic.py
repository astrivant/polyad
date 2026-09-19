"""
Compile optional Istio traffic splits without changing structural edge weights.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from attrs import evolve

from polyad.compiler.passes.network import scope_label
from polyad_types.networking.traffic import TrafficWeights

if TYPE_CHECKING:
    from typing import Any

    from polyad_types.graphs.topology import Topology
    from polyad_types.networking.traffic import TrafficRoute


def subset_name(target: str) -> str:
    """
    Keep a destination subset's identity stable when routes are reordered.

    Args:
        target (str): Relative downstream node path.

    Returns:
        str: Stable Istio-compatible subset name.
    """
    return "target-" + hashlib.sha256(target.encode()).hexdigest()[:16]


def capacity_weights(route: TrafficRoute, capacities: dict[str, float]) -> TrafficWeights | None:
    """
    Allocate integer percentages proportionally to measured capacity within destination bounds.

    Args:
        route (TrafficRoute): Configured destination ranges.
        capacities (dict[str, float]): Completed rate plus reported headroom for every destination.

    Returns:
        TrafficWeights | None: Bounded percentages, or None when measured capacity cannot satisfy the ranges.
    """
    weights = {destination.target: destination.minWeight for destination in route.destinations}
    if not any(capacities.values()):
        return None
    maximums = {
        destination.target: destination.maxWeight if capacities[destination.target] > 0 else destination.minWeight
        for destination in route.destinations
    }
    if sum(maximums.values()) < 100:
        return None
    # Highest averages apportions exactly 100 integer points, retaining configured
    # minimums and caps. A zero-capacity destination receives only its reserved minimum.
    for _ in range(100 - sum(weights.values())):
        eligible = [name for name in weights if weights[name] < maximums[name]]
        winner = max(eligible, key=lambda name: capacities[name] / (weights[name] + 1))
        weights[winner] += 1
    return TrafficWeights(route.name, weights)


def step_weights(graph: Topology, targets: tuple[TrafficWeights, ...], step: int) -> tuple[TrafficRoute, ...]:
    """
    Move toward approved percentages while preserving totals and per-target step bounds.

    Args:
        graph (Topology): Validated current routes and destination bounds.
        targets (tuple[TrafficWeights, ...]): Complete distributions from the selected demand tier.
        step (int): Maximum percentage-point change for each destination.

    Returns:
        tuple[TrafficRoute, ...]: Routes with one bounded adjustment toward the selected targets.
    """
    selected = {target.route: target.weights for target in targets}
    result = []
    for route in graph.traffic:
        desired = selected.get(route.name)
        if desired is None:
            result.append(route)
            continue
        weights = {destination.target: destination.weight for destination in route.destinations}
        donors = {name: min(step, value - desired[name]) for name, value in weights.items() if value > desired[name]}
        receivers = {name: min(step, desired[name] - value) for name, value in weights.items() if value < desired[name]}
        for donor in donors:
            for receiver in receivers:
                amount = min(donors[donor], receivers[receiver])
                weights[donor] -= amount
                weights[receiver] += amount
                donors[donor] -= amount
                receivers[receiver] -= amount
        result.append(
            evolve(route, destinations=tuple(evolve(destination, weight=weights[destination.target]) for destination in route.destinations))
        )
    return tuple(result)


def route_specs(
    namespace: str, kind: str, name: str, route: TrafficRoute, selectors: dict[str, dict[str, str]] | None = None
) -> dict[str, dict[str, Any]]:
    """
    Scope a Service's downstream subsets and percentage routes to one caller subtree.

    Args:
        namespace (str): Namespace containing both the graph and the Service.
        kind (str): Owning graph boundary kind.
        name (str): Persisted graph instance name.
        route (TrafficRoute): Validated local percentage route.
        selectors (dict[str, dict[str, str]] | None): Refreshed membership selectors for nested target paths.

    Returns:
        dict[str, dict[str, Any]]: DestinationRule followed by VirtualService specifications.
    """
    host = f"{route.service}.{namespace}.svc.cluster.local"
    source = {scope_label(namespace, kind, name, route.source): "true"}
    subsets = {destination.target: subset_name(destination.target) for destination in route.destinations}
    selectors = selectors or {
        destination.target: {scope_label(namespace, kind, name, destination.target): "true"} for destination in route.destinations
    }
    return {
        "DestinationRule": {
            "host": host,
            "exportTo": ["."],
            "workloadSelector": {"matchLabels": source},
            "subsets": [
                {"name": subsets[destination.target], "labels": selectors[destination.target]} for destination in route.destinations
            ],
        },
        "VirtualService": {
            "hosts": [host],
            "gateways": ["mesh"],
            "exportTo": ["."],
            "http": [
                {
                    "name": route.name,
                    "match": [{"sourceLabels": source, "sourceNamespace": namespace, "port": route.port}],
                    "route": [
                        {
                            "destination": {"host": host, "subset": subsets[destination.target], "port": {"number": route.port}},
                            "weight": destination.weight,
                        }
                        for destination in route.destinations
                    ],
                }
            ],
        },
    }
