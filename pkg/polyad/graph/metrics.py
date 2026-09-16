"""
Measure admission shape and cyclic data flow without enumerating paths or cycles.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

import networkx as nx

from polyad_types.resources import (
    AdmissionMetrics,
    ConnectionMetrics,
    LayerMetrics,
    NodeCounts,
    TopologyMetrics,
    converter,
    to_document,
)

if TYPE_CHECKING:
    from typing import Any

    from polyad_types.topology import Topology


def _layers(graph: nx.DiGraph[str] | nx.DiGraph[int]) -> LayerMetrics:
    widths = [len(layer) for layer in nx.topological_generations(graph)]
    depth, breadth = len(widths), max(widths, default=0)
    return LayerMetrics(depth=depth, breadth=breadth, layerWidths=widths, breadthDepth=breadth * depth)


def topology_metrics(graph: Topology, present: set[str] | None = None) -> dict[str, Any]:
    """
    Measure one boundary, treating nested graphs as individual nodes.

    Args:
        graph (Topology): Validated graph whose admission relation is acyclic.
        present (set[str] | None): Optional node subset for the observed induced graph.

    Returns:
        dict[str, Any]: Node counts, admission layers and cyclic connection components.
    """
    return to_document(measure_topology(graph, present))


def measure_topology(graph: Topology, present: set[str] | None = None) -> TopologyMetrics:
    """
    Build a typed observation of admission layers and cyclic connection components.

    Args:
        graph (Topology): Validated graph whose admission relation is acyclic.
        present (set[str] | None): Optional node subset for the observed induced graph.

    Returns:
        TopologyMetrics: Boundary shape as a serializable attrs tree.
    """
    nodes = [node for node in graph.nodes if present is None or node.name in present]
    names = {node.name for node in nodes}
    admission: nx.DiGraph[str] = nx.DiGraph()
    admission.add_nodes_from(names)
    admission.add_edges_from((edge.node, node.name) for node in nodes for edge in node.requires if edge.node in names)
    flow: nx.DiGraph[str] = nx.DiGraph()
    flow.add_nodes_from(names)
    flow.add_edges_from((edge.source, edge.target) for edge in graph.connections if edge.source in names and edge.target in names)
    components = list(nx.strongly_connected_components(flow))
    condensed = nx.condensation(flow, components)
    counts: Counter[str] = Counter(node.kind for node in nodes)
    return TopologyMetrics(
        nodeCount=len(nodes),
        nodesByKind=converter.structure(
            {
                kind: counts[kind]
                for kind in (
                    "Workload",
                    "Daemon",
                    "Resource",
                    "Graph",
                    "PolyGraph",
                    "ReplicaGroup",
                )
            },
            NodeCounts,
        ),
        subgraphCount=sum(counts[kind] for kind in ("Graph", "PolyGraph", "ReplicaGroup")),
        admission=AdmissionMetrics(
            edgeCount=admission.number_of_edges(),
            **to_document(_layers(admission)),
            rootCount=sum(degree == 0 for _, degree in admission.in_degree()),
            leafCount=sum(degree == 0 for _, degree in admission.out_degree()),
            maxFanIn=max((degree for _, degree in admission.in_degree()), default=0),
            maxFanOut=max((degree for _, degree in admission.out_degree()), default=0),
        ),
        connections=ConnectionMetrics(
            edgeCount=flow.number_of_edges(),
            weakComponents=nx.number_weakly_connected_components(flow),
            strongComponents=len(components),
            cyclicComponents=sum(len(component) > 1 or any(flow.has_edge(node, node) for node in component) for component in components),
            largestStrongComponent=max(map(len, components), default=0),
            condensation=_layers(condensed),
        ),
    )
