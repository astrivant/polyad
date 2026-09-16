"""
Compute graph measurements and evaluate structural constraints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import networkx as nx
import numpy as np

if TYPE_CHECKING:
    from typing import Any

    from polyad_types.rules import StructuralRule
    from polyad_types.topology import Topology


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


def graph_cheeger(graph: nx.Graph[str]) -> float:
    """
    Compute exact unnormalized edge expansion on at most 20 vertices.

    Args:
        graph (nx.Graph[str]): Relation; direction, weights, repeated edges and self-loops are ignored.

    Returns:
        float: Minimum cut size divided by the smaller side's vertex count; zero for fewer than two vertices.
    """
    if len(graph) > 20:
        raise ValueError("Cheeger rules support at most 20 vertices per boundary")
    simple: nx.Graph[str] = nx.Graph()
    simple.add_nodes_from(graph)
    simple.add_edges_from(graph.edges())
    simple.remove_edges_from(nx.selfloop_edges(simple))
    n = len(simple)
    if n < 2 or not nx.is_connected(simple):
        return 0.0
    indices = {node: index for index, node in enumerate(simple)}
    neighbors = [sum(1 << indices[neighbor] for neighbor in simple[node]) for node in simple]
    best = float(min(dict(simple.degree()).values()))
    subset = cut = 0
    # Gray-code traversal changes one vertex at a time. Fix the last vertex
    # outside the subset to visit each cut exactly once, without storing subsets.
    for step in range(1, 1 << (n - 1)):
        next_subset = step ^ (step >> 1)
        changed = subset ^ next_subset
        vertex = changed.bit_length() - 1
        delta = neighbors[vertex].bit_count() - 2 * (neighbors[vertex] & subset).bit_count()
        cut += delta if next_subset & changed else -delta
        subset = next_subset
        size = subset.bit_count()
        best = min(best, cut / min(size, n - size))
    return best


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
    measured: dict[str, int | float] = {
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
    if rule.cheeger is not None:
        actual = graph_cheeger(graph)
        measured["cheeger"] = actual
        for threshold, lower in ((rule.cheeger.minimum, True), (rule.cheeger.maximum, False)):
            if threshold is not None:
                tolerance = 1e-9 * max(1.0, abs(actual), abs(threshold))
                if (actual + tolerance < threshold) if lower else (actual - tolerance > threshold):
                    violations.append(f"cheeger={actual:.12g} violates {'minimum' if lower else 'maximum'} {threshold}")
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
