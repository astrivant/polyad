"""
Verify PCA quotient measurements remain conservative and reproducible.
"""

from __future__ import annotations

import networkx as nx
import pytest

from polyad.graph.cheeger import compute_cheeger
from polyad_benchmarks.cheeger_reduction import (
    approximate_cut,
    churn_graph,
    cluster_vertices,
    exact_cut,
    graph_case,
    pca_embedding,
    quotient_cut,
)


@pytest.mark.parametrize("topology", ["path", "cycle", "small-world", "community"])
def test_study_oracle_matches_production_cheeger_semantics(topology):
    """
    Keep the independent study reference aligned with Polyad's exact calculation.
    """
    graph = graph_case(topology, 10, 11)
    expected = compute_cheeger(nx.relabel_nodes(graph, str))
    measured = exact_cut(graph)
    assert expected.exact
    assert measured.value == pytest.approx(expected.upperBound)
    assert measured.evaluatedCuts == expected.evaluatedCuts == 511


def test_quotient_search_returns_a_witnessed_upper_bound_and_safe_interval():
    """
    Reduced cuts may overestimate expansion but cannot beat the exact minimum.
    """
    graph = graph_case("community", 14, 29)
    exact = exact_cut(graph)
    reduced = approximate_cut(graph, components=3, supernodes=6)
    assert reduced["lowerBound"] <= exact.value + 1e-9
    assert exact.value <= reduced["upperBound"] + 1e-9
    assert reduced["intervalWidth"] == pytest.approx(reduced["upperBound"] - reduced["lowerBound"])
    assert reduced["evaluatedCuts"] == 31
    assert 0 < reduced["retainedVariance"] <= 1


def test_one_supernode_per_vertex_recovers_the_exact_cut():
    """
    Removing quotient compression must restore the full partition search.
    """
    graph = graph_case("small-world", 9, 47)
    points, retained = pca_embedding(graph, components=4)
    labels = cluster_vertices(points, clusters=9)
    reduced = quotient_cut(graph, labels)
    exact = exact_cut(graph)
    assert retained <= 1
    assert reduced.value == pytest.approx(exact.value)
    assert reduced.evaluatedCuts == exact.evaluatedCuts


def test_cached_partition_is_reevaluated_on_current_edges():
    """
    Reuse cluster membership without reusing a stale Cheeger measurement.
    """
    baseline = graph_case("community", 14, 11)
    labels = cluster_vertices(pca_embedding(baseline, 3)[0], 6)
    steady = churn_graph(baseline, 0, 99)
    changed = churn_graph(baseline, 0.25, 99)
    cached = quotient_cut(changed, labels)
    exact = exact_cut(changed)
    assert set(changed) == set(baseline)
    assert set(steady.edges()) == set(baseline.edges())
    assert changed.number_of_edges() == baseline.number_of_edges()
    assert cached.value >= exact.value
    assert cached.subset
