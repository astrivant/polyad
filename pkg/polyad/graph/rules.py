"""
Constrain graph structure with explicit combinatorial and spectral measurements.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

import networkx as nx
import numpy as np
from attrs import field, frozen

from polyad.graph.network import NetworkAccess

if TYPE_CHECKING:
    from typing import Any

    from polyad.graph.topology import Topology

LIMITS = frozenset(
    {"nodes", "edges", "depth", "breadth", "fanIn", "fanOut", "cycleRank", "strongComponent", "expandedNodes", "nestingDepth"}
)


@frozen
class Spectrum:
    """
    Bound the spectrum of the simple undirected projection of a graph relation.

    Attributes:
        maxRadius (float | None): Maximum absolute adjacency eigenvalue.
        minConnectivity (float | None): Minimum second eigenvalue of the combinatorial Laplacian.
        maxLaplacian (float | None): Maximum combinatorial Laplacian eigenvalue.
    """

    maxRadius: float | None = None
    minConnectivity: float | None = None
    maxLaplacian: float | None = None

    def __attrs_post_init__(self) -> None:
        """
        Reject undefined or negative spectral thresholds.

        Returns:
            None: No return value.
        """
        for value in (self.maxRadius, self.minConnectivity, self.maxLaplacian):
            if value is not None and (isinstance(value, bool) or not math.isfinite(value) or value < 0):
                raise ValueError("spectral thresholds must be finite and nonnegative")


@frozen
class StructuralRule:
    """
    Apply reusable mathematical constraints to each graph boundary and its subtree.

    Attributes:
        scope (Literal['Boundary', 'Subtree']): Whether selected rules propagate to descendant boundaries.
        enforcement (Literal['Namespace', 'Referenced']): Mandatory namespace policy or explicitly selected rule.
        relation (Literal['admission', 'connections']): Directed edge relation used for local measurements.
        limits (dict[str, int]): Inclusive upper bounds on named combinatorial measurements.
        shapes (tuple[Literal['acyclic', 'connected', 'tree', 'planar'], ...]): Required graph properties.
        spectrum (Spectrum | None): Optional undirected spectral constraints, limited to 256 vertices.
        network (NetworkAccess | None): Mandatory or referenced traffic restrictions inherited by graph descendants.
    """

    scope: Literal["Boundary", "Subtree"] = "Subtree"
    enforcement: Literal["Namespace", "Referenced"] = "Namespace"
    relation: Literal["admission", "connections"] = "admission"
    limits: dict[str, int] = field(factory=dict)
    shapes: tuple[Literal["acyclic", "connected", "tree", "planar"], ...] = ()
    spectrum: Spectrum | None = None
    network: NetworkAccess | None = None

    def __attrs_post_init__(self) -> None:
        """
        Reject unknown measurements and invalid bounds before evaluating a policy.

        Returns:
            None: No return value.
        """
        if self.scope not in {"Boundary", "Subtree"}:
            raise ValueError("unknown rule scope")
        if self.enforcement not in {"Namespace", "Referenced"} or self.relation not in {"admission", "connections"}:
            raise ValueError("unknown rule enforcement or graph relation")
        if set(self.limits) - LIMITS or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in self.limits.values()):
            raise ValueError("rule limits require known measurements and nonnegative integers")
        if set(self.shapes) - {"acyclic", "connected", "tree", "planar"}:
            raise ValueError("unknown graph shape")


def relation_graph(topology: Topology, relation: str) -> nx.DiGraph[str]:
    """
    Build one directed simple relation including isolated vertices.

    Args:
        topology (Topology): Validated graph boundary.
        relation (str): Admission dependencies or data-flow connections.

    Returns:
        nx.DiGraph[str]: Directed relation with repeated edges collapsed.
    """
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_nodes_from(node.name for node in topology.nodes)
    if relation == "admission":
        graph.add_edges_from((edge.node, node.name) for node in topology.nodes for edge in node.requires)
    elif relation == "connections":
        graph.add_edges_from((edge.source, edge.target) for edge in topology.connections)
    else:
        raise ValueError("unknown graph relation")
    return graph


def graph_spectrum(graph: nx.DiGraph[str]) -> dict[str, Any]:
    """
    Compute adjacency and combinatorial Laplacian spectra of an undirected projection.

    Args:
        graph (nx.DiGraph[str]): Directed relation; directions, duplicate edges and self-loops are discarded.

    Returns:
        dict[str, Any]: Sorted eigenvalues, spectral radius, algebraic connectivity and largest Laplacian eigenvalue.
    """
    if len(graph) > 256:
        raise ValueError("spectral rules support at most 256 vertices per boundary")
    projection: nx.Graph[str] = nx.Graph()
    projection.add_nodes_from(graph)
    projection.add_edges_from(graph.edges)
    projection.remove_edges_from(nx.selfloop_edges(projection))
    adjacency = nx.to_numpy_array(projection, nodelist=sorted(projection), dtype=np.dtype(float))
    laplacian = np.diag(adjacency.sum(axis=1)) - adjacency
    try:
        a, lap = np.linalg.eigvalsh(adjacency), np.linalg.eigvalsh(laplacian)
    except np.linalg.LinAlgError as error:
        raise ValueError("graph eigenvalue computation did not converge") from error
    return {
        "projection": "simple-undirected-without-self-loops",
        "adjacency": a.tolist(),
        "laplacian": lap.tolist(),
        "radius": float(np.max(np.abs(a))) if len(a) else 0.0,
        "connectivity": max(0.0, float(lap[1])) if len(lap) > 1 else 0.0,
        "largestLaplacian": max(0.0, float(lap[-1])) if len(lap) else 0.0,
    }


def evaluate_rule(rule: StructuralRule, topology: Topology, *, expanded_nodes: int, nesting_depth: int) -> dict[str, Any]:
    """
    Evaluate inclusive bounds, required shapes and optional spectral constraints.

    Args:
        rule (StructuralRule): Engineer-defined structural policy.
        topology (Topology): Validated boundary being considered for admission.
        expanded_nodes (int): Node occurrences across this boundary and all referenced subgraph instances.
        nesting_depth (int): Maximum boundary nesting, counting this boundary as one.

    Returns:
        dict[str, Any]: Measurements, violations and the policy verdict.
    """
    graph = relation_graph(topology, rule.relation)
    simple = nx.Graph(graph)
    simple.remove_edges_from(nx.selfloop_edges(simple))
    condensed = nx.condensation(graph)
    layers = [len(layer) for layer in nx.topological_generations(condensed)]
    components = list(nx.strongly_connected_components(graph))
    measured = {
        "nodes": len(graph),
        "edges": graph.number_of_edges(),
        "depth": len(layers),
        "breadth": max(layers, default=0),
        "fanIn": max((v for _, v in graph.in_degree()), default=0),
        "fanOut": max((v for _, v in graph.out_degree()), default=0),
        "cycleRank": simple.number_of_edges() - len(simple) + nx.number_connected_components(simple),
        "strongComponent": max(map(len, components), default=0),
        "expandedNodes": expanded_nodes,
        "nestingDepth": nesting_depth,
    }
    violations = [f"{key}={measured[key]} exceeds {limit}" for key, limit in rule.limits.items() if measured[key] > limit]
    shapes = {
        "acyclic": nx.is_directed_acyclic_graph(graph),
        "connected": bool(simple) and nx.is_connected(simple),
        "tree": bool(simple) and nx.is_tree(simple),
    }
    if "planar" in rule.shapes:
        shapes["planar"] = nx.check_planarity(simple)[0]
    violations.extend(f"required shape: {shape}" for shape in rule.shapes if not shapes[shape])
    spectrum = graph_spectrum(graph) if rule.spectrum is not None else None
    if rule.spectrum is not None and spectrum is not None:
        for key, threshold, lower in (
            ("radius", rule.spectrum.maxRadius, False),
            ("connectivity", rule.spectrum.minConnectivity, True),
            ("largestLaplacian", rule.spectrum.maxLaplacian, False),
        ):
            if threshold is not None:
                actual = spectrum[key]
                tolerance = 1e-9 * max(1.0, abs(actual), abs(threshold))
                if (actual + tolerance < threshold) if lower else (actual - tolerance > threshold):
                    violations.append(f"{key}={actual:.12g} violates {'minimum' if lower else 'maximum'} {threshold}")
    return {"allowed": not violations, "relation": rule.relation, "measurements": measured, "spectrum": spectrum, "violations": violations}
