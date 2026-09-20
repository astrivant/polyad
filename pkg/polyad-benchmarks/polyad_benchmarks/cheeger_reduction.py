"""
Measure PCA-guided quotient cuts without presenting an approximation as exact.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import networkx as nx
import numpy as np

if TYPE_CHECKING:
    from typing import Any


@dataclass(frozen=True)
class CutMeasurement:
    """
    Record one exhaustive or quotient-restricted cut search.

    Attributes:
        value (float): Smallest observed unnormalized edge-expansion ratio.
        subset (frozenset[int]): Original vertices witnessing the ratio.
        evaluatedCuts (int): Distinct complementary partitions evaluated.
        durationSeconds (float): Measured search or reduction-and-search duration.
    """

    value: float
    subset: frozenset[int]
    evaluatedCuts: int
    durationSeconds: float


def _masks(graph: nx.Graph[int]) -> tuple[list[int], list[int]]:
    """
    Encode a simple graph for allocation-free cut traversal.

    Args:
        graph (nx.Graph[int]): Connected graph labelled with consecutive integers.

    Returns:
        tuple[list[int], list[int]]: Neighbor bit masks and vertex degrees.
    """

    # Bit i means vertex i is adjacent. Population counts then recover degrees
    # and crossing-edge counts without constructing a set for every candidate cut.
    neighbors = [sum(1 << int(vertex) for vertex in graph[node]) for node in range(len(graph))]
    return neighbors, [mask.bit_count() for mask in neighbors]


def exact_cut(graph: nx.Graph[int]) -> CutMeasurement:
    """
    Compute the exact unnormalized edge expansion used by Polyad.

    Args:
        graph (nx.Graph[int]): Connected simple graph labelled from zero.

    Returns:
        CutMeasurement: Exact minimum and its measured exhaustive cost.
    """
    started = time.perf_counter()
    size = len(graph)
    if size < 2 or not nx.is_connected(graph):
        return CutMeasurement(0.0, frozenset(), 0, time.perf_counter() - started)

    # This reference enumerator is separate from the production selector. Fixing
    # the final vertex outside each subset visits every complementary cut once.
    neighbors, degrees = _masks(graph)
    subset = boundary = best_subset = 0
    best = float("inf")
    total = (1 << (size - 1)) - 1
    bit_count = int.bit_count

    # Gray code changes one vertex per step. Adding it contributes its degree
    # minus twice its neighbors already inside; removing it reverses that change.
    for step in range(1, total + 1):
        next_subset = step ^ (step >> 1)
        changed = subset ^ next_subset
        vertex = changed.bit_length() - 1
        delta = degrees[vertex] - 2 * bit_count(neighbors[vertex] & subset)
        boundary += delta if next_subset & changed else -delta
        subset = next_subset

        # Normalize by the smaller side, not by edge volume: this is the same
        # unnormalized expansion metric that production GraphRules constrain.
        count = bit_count(subset)
        value = boundary / min(count, size - count)
        if value < best:
            best, best_subset = value, subset

    witness = frozenset(index for index in range(size) if best_subset & (1 << index))
    return CutMeasurement(best, witness, total, time.perf_counter() - started)


def pca_embedding(graph: nx.Graph[int], components: int) -> tuple[np.ndarray, float]:
    """
    Project centered adjacency signatures onto their leading principal components.

    Args:
        graph (nx.Graph[int]): Graph whose rows describe each vertex's neighbors.
        components (int): Positive number of principal axes to retain.

    Returns:
        tuple[np.ndarray, float]: Vertex embedding and retained variance fraction.
    """
    if type(components) is not int or components < 1:
        raise ValueError("PCA components must be a positive integer")

    # Treat a vertex's adjacency row as its feature vector. Center each column,
    # then use U * singular_values as the coordinates along leading PCA axes.
    matrix = nx.to_numpy_array(graph, nodelist=list(range(len(graph))), dtype=np.dtype(float))
    centered = matrix - matrix.mean(axis=0, keepdims=True)
    left, singular, _ = np.linalg.svd(centered, full_matrices=False)
    retained = min(components, len(singular))

    # Explained adjacency variance describes compression, not Cheeger accuracy.
    # A low-variance direction can still contain an important sparse boundary.
    total_variance = float(np.square(singular).sum())
    explained = float(np.square(singular[:retained]).sum() / total_variance) if total_variance else 1.0
    return left[:, :retained] * singular[:retained], explained


def cluster_vertices(points: np.ndarray, clusters: int) -> tuple[int, ...]:
    """
    Group embedded vertices with deterministic farthest-first k-means.

    Args:
        points (np.ndarray): Vertex rows in retained PCA coordinates.
        clusters (int): Number of quotient supernodes.

    Returns:
        tuple[int, ...]: Stable cluster index for each original vertex.
    """
    count = len(points)
    if type(clusters) is not int or not 2 <= clusters <= count:
        raise ValueError("supernode count must be between two and the vertex count")
    if clusters == count:
        return tuple(range(count))

    # Farthest-first initialization spreads centers out without random restarts.
    # Stable index tie-breaking keeps a fixed embedding's grouping repeatable.
    norms = np.square(points).sum(axis=1)
    centers = [int(np.argmax(norms))]
    distances = np.square(points - points[centers[0]]).sum(axis=1)
    while len(centers) < clusters:
        candidate = int(np.argmax(distances))
        if candidate in centers:
            candidate = next(index for index in range(count) if index not in centers)
        centers.append(candidate)
        distances = np.minimum(distances, np.square(points - points[candidate]).sum(axis=1))

    # Repeatedly assign each vertex to its nearest center, then move each center
    # to its group's mean. Stop on stable assignments or the fixed iteration cap.
    centroids = points[centers].copy()
    labels = np.zeros(count, dtype=int)
    for _ in range(64):
        next_labels = np.square(points[:, None, :] - centroids[None, :, :]).sum(axis=2).argmin(axis=1)

        # Preserve the requested quotient size when symmetry produces coincident rows.
        missing = set(range(clusters)) - set(map(int, next_labels))
        for missing_cluster in sorted(missing):
            counts = np.bincount(next_labels, minlength=clusters)
            represented = np.array(
                [
                    np.square(points[index] - centroids[next_labels[index]]).sum() if counts[next_labels[index]] > 1 else -1.0
                    for index in range(count)
                ]
            )
            candidate = int(np.argmax(represented))
            next_labels[candidate] = missing_cluster

        if np.array_equal(labels, next_labels):
            break
        labels = next_labels
        centroids = np.array([points[labels == index].mean(axis=0) for index in range(clusters)])

    return tuple(map(int, labels))


def quotient_cut(graph: nx.Graph[int], labels: tuple[int, ...]) -> CutMeasurement:
    """
    Search cuts that are unions of PCA clusters and lift the witness to the graph.

    Args:
        graph (nx.Graph[int]): Original connected graph.
        labels (tuple[int, ...]): Cluster assignment for every original vertex.

    Returns:
        CutMeasurement: Certified upper bound witnessed by an original-graph cut.
    """
    started = time.perf_counter()
    size = len(graph)
    if len(labels) != size or not labels:
        raise ValueError("cluster labels must cover every graph vertex")
    clusters = max(labels) + 1
    if set(labels) != set(range(clusters)):
        raise ValueError("cluster labels must be contiguous and nonempty")

    # Each quotient node is a set of original vertices. Encoding that set as a
    # mask lets a union of groups become an original-graph cut using bitwise OR.
    cluster_masks = [sum(1 << vertex for vertex, label in enumerate(labels) if label == cluster) for cluster in range(clusters)]
    neighbors, _ = _masks(graph)
    all_nodes = (1 << size) - 1
    best = float("inf")
    best_subset = 0
    total = (1 << (clusters - 1)) - 1
    for cluster_subset in range(1, total + 1):
        subset = 0
        for cluster, mask in enumerate(cluster_masks):
            if cluster_subset & (1 << cluster):
                subset |= mask

        # Score the lifted subset using original edges and original vertex counts.
        # Restricting the candidate family can raise the minimum, never lower it.
        count = subset.bit_count()
        boundary = sum((neighbors[vertex] & (all_nodes ^ subset)).bit_count() for vertex in range(size) if subset & (1 << vertex))
        value = boundary / min(count, size - count)
        if value < best:
            best, best_subset = value, subset

    witness = frozenset(index for index in range(size) if best_subset & (1 << index))
    return CutMeasurement(best, witness, total, time.perf_counter() - started)


def spectral_lower_bound(graph: nx.Graph[int]) -> float:
    """
    Compute the combinatorial-Laplacian lower bound for vertex expansion.

    Args:
        graph (nx.Graph[int]): Connected unweighted graph.

    Returns:
        float: Certified lower bound lambda-two divided by two.
    """

    # This bound comes from the full graph, independently of the PCA clustering.
    # Its second-smallest Laplacian eigenvalue gives lambda_2 / 2 <= h(G).
    adjacency = nx.to_numpy_array(graph, nodelist=list(range(len(graph))), dtype=np.dtype(float))
    laplacian = np.diag(adjacency.sum(axis=1)) - adjacency
    eigenvalues = np.linalg.eigvalsh(laplacian)
    return max(0.0, float(eigenvalues[1]) / 2) if len(eigenvalues) > 1 else 0.0


def approximate_cut(graph: nx.Graph[int], components: int, supernodes: int) -> dict[str, Any]:
    """
    Build a PCA quotient and return a certified interval around its cut witness.

    Args:
        graph (nx.Graph[int]): Original graph with consecutive integer labels.
        components (int): Principal dimensions retained before clustering.
        supernodes (int): Clusters whose unions define the reduced search space.

    Returns:
        dict[str, Any]: Upper witness, spectral lower bound and reduction diagnostics.
    """
    started = time.perf_counter()

    # Compression chooses which cuts to search; it does not certify their error.
    # Pair the lifted upper witness with a separate original-graph lower bound.
    points, variance = pca_embedding(graph, components)
    labels = cluster_vertices(points, supernodes)
    estimate = quotient_cut(graph, labels)
    lower = spectral_lower_bound(graph)

    return {
        "upperBound": estimate.value,
        "lowerBound": lower,
        "intervalWidth": max(0.0, estimate.value - lower),
        "cut": sorted(estimate.subset),
        "labels": labels,
        "evaluatedCuts": estimate.evaluatedCuts,
        "retainedVariance": variance,
        "durationSeconds": time.perf_counter() - started,
    }


def graph_case(topology: str, vertices: int, seed: int) -> nx.Graph[int]:
    """
    Construct a deterministic connected graph family for controlled comparisons.

    Args:
        topology (str): Path, cycle, small-world or two-community family.
        vertices (int): Number of graph vertices.
        seed (int): Reproducible generator seed.

    Returns:
        nx.Graph[int]: Simple connected graph labelled with consecutive integers.
    """
    if topology == "path":
        graph = nx.path_graph(vertices)
    elif topology == "cycle":
        graph = nx.cycle_graph(vertices)
    elif topology == "small-world":
        graph = nx.watts_strogatz_graph(vertices, min(4, vertices - 1), 0.2, seed=seed)
    elif topology == "community":
        left = vertices // 2
        graph = nx.stochastic_block_model([left, vertices - left], [[0.55, 0.03], [0.03, 0.55]], seed=seed)
    else:
        raise ValueError(f"unknown graph topology: {topology}")

    # Random graph generators can leave components disconnected. Link them in a
    # chain so the study compares nontrivial expansion rather than a known zero.
    components = list(nx.connected_components(graph))
    for first, second in zip(components, components[1:], strict=False):
        graph.add_edge(min(first), min(second))
    return nx.Graph(nx.convert_node_labels_to_integers(graph))


def churn_graph(graph: nx.Graph[int], fraction: float, seed: int) -> nx.Graph[int]:
    """
    Replace a bounded fraction of edges while preserving connectivity and vertex identity.

    Args:
        graph (nx.Graph[int]): Stable baseline graph.
        fraction (float): Fraction of existing edges to replace.
        seed (int): Reproducible mutation seed.

    Returns:
        nx.Graph[int]: Connected snapshot with the same vertices and edge count.
    """
    if not 0 <= fraction <= 1:
        raise ValueError("edge churn must be between zero and one")
    changed = graph.copy()
    random = np.random.default_rng(seed)
    target = round(fraction * graph.number_of_edges())
    if target == 0:
        return changed

    # This original PCA study removes only non-bridge edges. Trees therefore
    # cannot change here; the strategy study's add-before-remove sampler handles
    # that case separately and explicitly measures achieved churn.
    removed = 0
    for index in random.permutation(graph.number_of_edges()):
        edge = list(graph.edges())[int(index)]
        changed.remove_edge(*edge)
        if nx.is_connected(changed):
            removed += 1
        else:
            changed.add_edge(*edge)
        if removed == target:
            break

    # Add genuinely new edges, not the ones just removed, and restore the count.
    original_edges = {frozenset(edge) for edge in graph.edges()}
    candidates = [edge for edge in nx.non_edges(changed) if frozenset(edge) not in original_edges]
    if len(candidates) < removed:
        raise ValueError("graph has too few unused edges for the requested churn")
    for index in random.permutation(len(candidates))[:removed]:
        changed.add_edge(*candidates[int(index)])

    return changed


def _record(graph: nx.Graph[int], exact: CutMeasurement, components: int, supernodes: int) -> dict[str, Any]:
    """
    Compare one reduced calculation with a previously measured exact reference.

    Args:
        graph (nx.Graph[int]): Graph being measured.
        exact (CutMeasurement): Exact reference for that same graph.
        components (int): Retained PCA dimensions.
        supernodes (int): Quotient search size.

    Returns:
        dict[str, Any]: Accuracy, certificate and runtime comparison.
    """
    approximation = approximate_cut(graph, components, supernodes)

    # Exact-relative error and speedup are retrospective study measurements.
    # Only the certificate bounds are available without knowing the exact answer.
    error = approximation["upperBound"] - exact.value
    return {
        **approximation,
        "exact": exact.value,
        "exactDurationSeconds": exact.durationSeconds,
        "exactCuts": exact.evaluatedCuts,
        "absoluteError": error,
        "relativeError": error / exact.value if exact.value else 0.0,
        "speedup": exact.durationSeconds / approximation["durationSeconds"],
        "components": components,
        "supernodes": supernodes,
        "vertices": len(graph),
        "edges": graph.number_of_edges(),
    }


def reduction_study(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Sweep one reduction axis at a time and exercise cached cuts under edge churn.

    Args:
        config (dict[str, Any]): Controlled topology, size, PCA, quotient and churn values.

    Returns:
        list[dict[str, Any]]: Full-factorial core, size scaling and stability measurements.
    """
    records: list[dict[str, Any]] = []
    fixed_vertices = config["fixedVertices"]

    # Cross dimensions and quotient sizes while reusing one exact reference for
    # each fixed graph. Plotters later slice this grid to isolate either axis.
    for topology in config["topologies"]:
        for seed in config["seeds"]:
            graph = graph_case(topology, fixed_vertices, seed)
            exact = exact_cut(graph)
            for components in config["components"]:
                for supernodes in config["supernodes"]:
                    records.append(
                        {
                            "sweep": "reduction",
                            "topology": topology,
                            "seed": seed,
                            **_record(graph, exact, components, supernodes),
                        }
                    )

    # Hold the graph family and reduction settings fixed while increasing n.
    for vertices in config["vertexCounts"]:
        for seed in config["seeds"]:
            graph = graph_case(config["fixedTopology"], vertices, seed)
            exact = exact_cut(graph)
            records.append(
                {
                    "sweep": "size",
                    "topology": config["fixedTopology"],
                    "seed": seed,
                    **_record(graph, exact, min(config["fixedComponents"], vertices), min(config["fixedSupernodes"], vertices)),
                }
            )

    # Cache a baseline partition once per seed. Rescore it on every changed graph
    # and compare with both a fresh PCA reduction and that snapshot's exact answer.
    for seed in config["seeds"]:
        baseline = graph_case(config["fixedTopology"], fixed_vertices, seed)
        points, _ = pca_embedding(baseline, config["fixedComponents"])
        cached_labels = cluster_vertices(points, config["fixedSupernodes"])
        for churn in config["edgeChurn"]:
            graph = churn_graph(baseline, churn, seed + round(churn * 10000))
            exact = exact_cut(graph)
            fresh = _record(graph, exact, config["fixedComponents"], config["fixedSupernodes"])

            # Cached timing excludes the original embedding and clustering cost;
            # it measures only the quotient search against current edges.
            started = time.perf_counter()
            cached = quotient_cut(graph, cached_labels)
            cached_duration = time.perf_counter() - started
            records.append(
                {
                    "sweep": "stability",
                    "topology": config["fixedTopology"],
                    "seed": seed,
                    "edgeChurn": churn,
                    **fresh,
                    "cachedUpperBound": cached.value,
                    "cachedAbsoluteError": cached.value - exact.value,
                    "cachedRelativeError": (cached.value - exact.value) / exact.value if exact.value else 0.0,
                    "cachedDurationSeconds": cached_duration,
                    "cachedCut": sorted(cached.subset),
                }
            )

    return records
