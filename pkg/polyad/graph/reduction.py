"""
Build conservative cached and spectral quotient certificates for Cheeger policies.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING

import networkx as nx
import numpy as np

if TYPE_CHECKING:
    from polyad_types.graphs.rules import CheegerReduction


@dataclass(frozen=True)
class ReductionCertificate:
    """
    Describe bounds obtained without enumerating every original-graph cut.

    Attributes:
        lowerBound (float): Proven lower bound on the original Cheeger constant.
        upperBound (float): Lifted original-graph cut and therefore a proven upper bound.
        cut (tuple[str, ...]): Original vertices in the lifted witness.
        evaluatedCuts (int): Quotient partitions evaluated.
        stage (str): CachedQuotient or FreshSpectralReduction.
        edgeChurn (float): Symmetric edge difference relative to a reused partition.
        labels (tuple[int, ...]): Cluster assignment in sorted vertex order.
    """

    lowerBound: float
    upperBound: float
    cut: tuple[str, ...]
    evaluatedCuts: int
    stage: str
    edgeChurn: float
    labels: tuple[int, ...]


@dataclass(frozen=True)
class _CacheEntry:
    edges: frozenset[tuple[str, str]]
    labels: tuple[int, ...]
    lowerBound: float


# Vertex identities and reduction settings identify a reusable partition. Edge
# sets live in the entry so topology changes can be measured against its origin.
_CACHE: OrderedDict[tuple[tuple[str, ...], int, int], _CacheEntry] = OrderedDict()
_CACHE_LOCK = RLock()


def _edges(graph: nx.Graph[str]) -> frozenset[tuple[str, str]]:
    """
    Return canonical undirected edges for churn comparison.

    Args:
        graph (nx.Graph[str]): Simple undirected graph.

    Returns:
        frozenset[tuple[str, str]]: Sorted endpoint pairs.
    """

    # An undirected edge has one identity regardless of endpoint insertion order.
    return frozenset((left, right) if left <= right else (right, left) for left, right in graph.edges())


def _churn(previous: frozenset[tuple[str, str]], current: frozenset[tuple[str, str]]) -> float:
    """
    Measure changed edges relative to the larger snapshot.

    Args:
        previous (frozenset[tuple[str, str]]): Cached edge set.
        current (frozenset[tuple[str, str]]): Current edge set.

    Returns:
        float: Symmetric-difference fraction from zero through one.
    """

    # Symmetric difference counts both removed and added edges. Dividing by the
    # union keeps the distance in [0, 1], including a safe zero for empty graphs.
    return len(previous ^ current) / max(1, len(previous | current))


def _cluster(points: np.ndarray, clusters: int) -> tuple[int, ...]:
    """
    Group spectral coordinates with deterministic farthest-first k-means.

    Args:
        points (np.ndarray): One row per vertex.
        clusters (int): Requested nonempty quotient groups.

    Returns:
        tuple[int, ...]: Stable contiguous cluster labels.
    """
    count = len(points)
    if clusters == count:
        return tuple(range(count))

    # Spread initial centers across the embedding: each new center is the vertex
    # farthest from its nearest existing center, with deterministic index ties.
    norms = np.square(points).sum(axis=1)
    centers = [int(np.argmax(norms))]
    distances = np.square(points - points[centers[0]]).sum(axis=1)
    while len(centers) < clusters:
        candidate = int(np.argmax(distances))
        if candidate in centers:
            candidate = next(index for index in range(count) if index not in centers)
        centers.append(candidate)
        distances = np.minimum(distances, np.square(points - points[candidate]).sum(axis=1))

    # Alternate nearest-center assignment and mean-center updates. The iteration
    # cap bounds clustering work even when assignments do not settle quickly.
    centroids = points[centers].copy()
    labels = np.zeros(count, dtype=int)
    for _ in range(64):
        next_labels = np.square(points[:, None, :] - centroids[None, :, :]).sum(axis=2).argmin(axis=1)

        # Symmetric vertices can coincide and leave a cluster empty. Reassign a
        # poorly represented vertex from a non-singleton group to keep k groups.
        for missing in sorted(set(range(clusters)) - set(map(int, next_labels))):
            counts = np.bincount(next_labels, minlength=clusters)
            residuals = np.array(
                [
                    np.square(points[index] - centroids[next_labels[index]]).sum() if counts[next_labels[index]] > 1 else -1.0
                    for index in range(count)
                ]
            )
            next_labels[int(np.argmax(residuals))] = missing

        if np.array_equal(labels, next_labels):
            break
        labels = next_labels
        centroids = np.array([points[labels == index].mean(axis=0) for index in range(clusters)])

    return tuple(map(int, labels))


def _quotient_cut(graph: nx.Graph[str], nodes: tuple[str, ...], labels: tuple[int, ...]) -> tuple[float, tuple[str, ...], int]:
    """
    Search cluster unions and rescore every witness on the original graph.

    Args:
        graph (nx.Graph[str]): Current original graph.
        nodes (tuple[str, ...]): Vertex order corresponding to labels.
        labels (tuple[int, ...]): Nonempty contiguous cluster assignments.

    Returns:
        tuple[float, tuple[str, ...], int]: Upper bound, lifted witness and quotient cut count.
    """
    clusters = max(labels) + 1
    groups = [frozenset(node for node, label in zip(nodes, labels, strict=True) if label == cluster) for cluster in range(clusters)]
    best = float("inf")
    witness: frozenset[str] = frozenset()

    # Fix the last group outside every subset to avoid complementary duplicates.
    # Searching group unions shrinks the problem from n vertices to k groups.
    total = (1 << (clusters - 1)) - 1
    for mask in range(1, total + 1):
        subset = frozenset().union(*(groups[index] for index in range(clusters) if mask & (1 << index)))

        # Count actual original-graph edges and vertices, not quotient nodes.
        # Each lifted cut therefore remains a valid upper bound on the true h.
        value = nx.cut_size(graph, subset) / min(len(subset), len(nodes) - len(subset))
        if value < best:
            best, witness = float(value), subset

    return best, tuple(sorted(witness)), total


def cached_quotient(graph: nx.Graph[str], settings: CheegerReduction) -> ReductionCertificate | None:
    """
    Reevaluate a low-churn cached partition without trusting a stale measurement.

    Args:
        graph (nx.Graph[str]): Current simple connected graph.
        settings (CheegerReduction): Effective reduction controls.

    Returns:
        ReductionCertificate | None: Current-graph certificate, or null when reuse is unsafe.
    """
    nodes = tuple(sorted(graph))
    key = (nodes, settings.components, min(settings.supernodes, len(nodes)))
    current = _edges(graph)

    # Hold the lock only while consulting and updating cache bookkeeping. Entries
    # are immutable, so the expensive cut search can run after releasing it.
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        churn = _churn(entry.edges, current)
        if churn > settings.maxEdgeChurn:
            return None
        _CACHE.move_to_end(key)

    # The old grouping may still produce a useful cut on new edges. Its old
    # spectral lower bound is valid only if the entire edge set is unchanged.
    upper, cut, evaluated = _quotient_cut(graph, nodes, entry.labels)
    lower = entry.lowerBound if entry.edges == current else 0.0
    return ReductionCertificate(lower, upper, cut, evaluated, "CachedQuotient", churn, entry.labels)


def fresh_spectral_reduction(graph: nx.Graph[str], settings: CheegerReduction) -> ReductionCertificate:
    """
    Build a Laplacian embedding, search its quotient and cache only the partition.

    Args:
        graph (nx.Graph[str]): Current simple connected graph.
        settings (CheegerReduction): Effective reduction controls.

    Returns:
        ReductionCertificate: Certified spectral lower bound and lifted-cut upper bound.
    """
    nodes = tuple(sorted(graph))
    adjacency = nx.to_numpy_array(graph, nodelist=nodes, dtype=np.dtype(float))

    # L = D - A is the unnormalized Laplacian used by this expansion metric.
    # The constant first eigenvector carries no partition information; use the
    # next low-frequency vectors to describe vertices' structural similarity.
    laplacian = np.diag(adjacency.sum(axis=1)) - adjacency
    eigenvalues, eigenvectors = np.linalg.eigh(laplacian)
    retained = min(settings.components, max(1, len(nodes) - 1))
    points = eigenvectors[:, 1 : retained + 1]
    clusters = min(settings.supernodes, len(nodes))
    labels = _cluster(points, clusters)

    # Clustering is a heuristic, not a preservation theorem. Safety comes from
    # the lifted cut above h and the independent spectral bound lambda_2 / 2 below h.
    upper, cut, evaluated = _quotient_cut(graph, nodes, labels)
    lower = max(0.0, float(eigenvalues[1]) / 2)
    current = _edges(graph)
    key = (nodes, settings.components, clusters)

    # Keep the newest partition at the end and evict the least recently used
    # entry first when multiple graph boundaries compete for the cache.
    if settings.cache:
        with _CACHE_LOCK:
            _CACHE[key] = _CacheEntry(current, labels, lower)
            _CACHE.move_to_end(key)
            while len(_CACHE) > settings.cacheEntries:
                _CACHE.popitem(last=False)

    return ReductionCertificate(lower, upper, cut, evaluated, "FreshSpectralReduction", 0.0, labels)


def clear_reduction_cache() -> None:
    """
    Clear process-local quotient partitions for deterministic tests.

    Returns:
        None: No return value.
    """
    with _CACHE_LOCK:
        _CACHE.clear()
