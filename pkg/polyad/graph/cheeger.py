"""
Search important cuts first, retaining exact semantics and explicit incomplete results.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import networkx as nx
from attrs import asdict, evolve, field, frozen

from polyad_types.graphs.rules import CheegerComputation

if TYPE_CHECKING:
    from typing import Any


@frozen
class CheegerResult:
    """
    Distinguish a proven constant from a witnessed upper bound after partial search.

    Attributes:
        exact (bool): Whether the minimum over all cuts is known.
        upperBound (float | None): Smallest witnessed ratio, or null before any cut is evaluated.
        cut (tuple[str, ...]): Witness vertices on one side of that cut.
        evaluatedCuts (int): Number of distinct partitions evaluated.
        reason (str): Completion, witnessed violation, or exhausted resource budget.
        skippedPriorityCuts (int): Configured subsets absent from or equal to the whole boundary.
        durationSeconds (float): Elapsed wall time for this calculation, including projection.
        inputs (dict[str, int | float]): Effective budgets, operator ceilings and projected graph dimensions.
    """

    exact: bool
    upperBound: float | None
    cut: tuple[str, ...]
    evaluatedCuts: int
    reason: str
    skippedPriorityCuts: int = 0
    durationSeconds: float = 0.0
    inputs: dict[str, int | float] = field(factory=dict)

    def report(self) -> dict[str, Any]:
        """
        Serialize the certificate without labelling an upper bound as a measured constant.

        Returns:
            dict[str, Any]: JSON-compatible diagnostics for status and logs.
        """
        return asdict(self)


class CheegerIncomplete(ValueError):
    """
    Refuse to return a partial cut search as an exact Cheeger constant.
    """

    def __init__(self, result: CheegerResult) -> None:
        """
        Preserve the search certificate for callers that expose decision diagnostics.

        Args:
            result (CheegerResult): Incomplete search result.
        """
        self.result = result
        super().__init__(f"Cheeger computation incomplete: {result.reason} after {result.evaluatedCuts} cuts")


def compute_cheeger(
    graph: nx.Graph[str],
    computation: CheegerComputation | None = None,
    *,
    limits: CheegerComputation | None = None,
    minimum: float | None = None,
) -> CheegerResult:
    """
    Evaluate configured cuts before an exhaustive Gray-code traversal under bounded work.

    Args:
        graph (nx.Graph[str]): Relation projected to simple undirected, unweighted edges without loops.
        computation (CheegerComputation | None): Local budgets and ordered preferred vertex subsets.
        limits (CheegerComputation | None): Optional administrator ceilings; local requests cannot exceed them.
        minimum (float | None): Stop early when a witnessed cut disproves this inclusive hard minimum.

    Returns:
        CheegerResult: Exact constant, a certified violation, or an explicitly incomplete search.
    """
    timed = time.perf_counter()
    started = time.monotonic()
    options = computation or CheegerComputation()
    ceilings = limits or CheegerComputation()
    budgets: dict[str, int | float] = {}
    for name, default in (("maxVertices", 20), ("maxCuts", 524287), ("timeoutSeconds", 5.0)):
        ceiling = getattr(ceilings, name) or default
        requested = getattr(options, name)
        if limits is not None and requested is not None and requested > ceiling:
            raise ValueError(f"Cheeger {name}={requested} exceeds operator ceiling {ceiling}")
        budgets[name] = ceiling if requested is None else requested
    inputs = {
        **budgets,
        **{
            "operator" + name[0].upper() + name[1:]: getattr(ceilings, name) or default
            for name, default in (("maxVertices", 20), ("maxCuts", 524287), ("timeoutSeconds", 5.0))
            if limits is not None
        },
        "vertices": len(graph),
        "priorityCuts": len(options.priorityCuts),
        "priorityVertices": sum(map(len, options.priorityCuts)),
    }

    def finish(value: CheegerResult) -> CheegerResult:
        return evolve(value, durationSeconds=time.perf_counter() - timed, inputs=dict(inputs))

    if len(graph) > budgets["maxVertices"]:
        return finish(CheegerResult(False, None, (), 0, f"VertexLimit: at most {budgets['maxVertices']} vertices per boundary"))
    simple: nx.Graph[str] = nx.Graph()
    simple.add_nodes_from(graph)
    simple.add_edges_from(graph.edges())
    simple.remove_edges_from(nx.selfloop_edges(simple))
    inputs["edges"] = simple.number_of_edges()
    n = len(simple)
    if n < 2:
        return finish(CheegerResult(True, 0.0, (), 0, "Complete"))
    if not nx.is_connected(simple):
        return finish(CheegerResult(True, 0.0, tuple(next(iter(nx.connected_components(simple)))), 0, "Complete"))
    nodes = list(simple)
    indices = {node: index for index, node in enumerate(nodes)}
    neighbors = [sum(1 << indices[neighbor] for neighbor in simple[node]) for node in nodes]
    total = (1 << (n - 1)) - 1
    all_nodes = (1 << n) - 1
    deadline = started + budgets["timeoutSeconds"]
    evaluated = skipped = best_subset = 0
    best: float | None = None
    priorities: set[int] = set()

    def result(reason: str) -> CheegerResult:
        return finish(
            CheegerResult(
                reason == "Complete",
                best,
                tuple(node for i, node in enumerate(nodes) if best_subset & (1 << i)),
                evaluated,
                reason,
                skipped,
            )
        )

    def exhausted() -> str | None:
        if evaluated >= budgets["maxCuts"]:
            return "CutBudget"
        if time.monotonic() >= deadline:
            return "TimeBudget"
        return None

    def observe(subset: int, cut: int) -> str | None:
        nonlocal evaluated, best, best_subset
        evaluated += 1
        size = subset.bit_count()
        value = cut / min(size, n - size)
        if best is None or value < best:
            best, best_subset = value, subset
        if evaluated == total:
            return "Complete"
        if minimum is not None and value + 1e-9 * max(1.0, abs(value), abs(minimum)) < minimum:
            return "MinimumViolated"
        return None

    for group in options.priorityCuts:
        # Subtree and Namespace rules can visit boundaries with different names.
        # Missing members never turn a preferred subset into a different cut.
        if not set(group) <= indices.keys() or len(group) == n:
            skipped += 1
            continue
        subset = sum(1 << indices[node] for node in group)
        if subset & (1 << (n - 1)):
            subset ^= all_nodes
        if subset in priorities:
            continue
        if reason := exhausted():
            return result(reason)
        priorities.add(subset)
        cut = sum((neighbors[i] & (all_nodes ^ subset)).bit_count() for i in range(n) if subset & (1 << i))
        if reason := observe(subset, cut):
            return result(reason)

    subset = cut = 0
    # Fix the last vertex outside each subset: complementary cuts count once.
    # Every step changes one vertex, so the cut can be updated incrementally.
    for step in range(1, total + 1):
        if evaluated >= budgets["maxCuts"]:
            return result("CutBudget")
        if (step == 1 or step % 256 == 0) and time.monotonic() >= deadline:
            return result("TimeBudget")
        next_subset = step ^ (step >> 1)
        changed = subset ^ next_subset
        vertex = changed.bit_length() - 1
        delta = neighbors[vertex].bit_count() - 2 * (neighbors[vertex] & subset).bit_count()
        cut += delta if next_subset & changed else -delta
        subset = next_subset
        if subset not in priorities and (reason := observe(subset, cut)):
            return result(reason)
    return result("Complete")


def graph_cheeger(
    graph: nx.Graph[str], computation: CheegerComputation | None = None, *, limits: CheegerComputation | None = None
) -> float:
    """
    Return exact unnormalized edge expansion, refusing incomplete budget-limited searches.

    Args:
        graph (nx.Graph[str]): Relation; directions, weights, repeated edges and self-loops are ignored.
        computation (CheegerComputation | None): Configurable vertex, cut and time budgets and cut priorities.
        limits (CheegerComputation | None): Optional administrator ceilings enforced independently of local preferences.

    Returns:
        float: Exact minimum cut ratio; zero for disconnected graphs or fewer than two vertices.
    """
    result = compute_cheeger(graph, computation, limits=limits)
    if not result.exact:
        raise CheegerIncomplete(result)
    assert result.upperBound is not None
    return result.upperBound
