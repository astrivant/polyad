"""
Search important cuts first, retaining exact semantics and explicit incomplete results.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import networkx as nx
from attrs import asdict, evolve, field, frozen

# Preserve existing import paths while keeping each exception defined centrally.
from polyad.exceptions.graph import CheegerIncomplete as CheegerIncomplete
from polyad.graph.reduction import cached_quotient, fresh_spectral_reduction
from polyad.graph.refresh import begin_refresh, finish_refresh
from polyad_types.graphs.rules import CheegerComputation

if TYPE_CHECKING:
    from typing import Any

    from polyad.graph.refresh import RefreshTicket

__all__ = (
    "CheegerIncomplete",
    "CheegerResult",
    "compute_cheeger",
    "graph_cheeger",
)


@frozen
class CheegerResult:
    """
    Distinguish a proven constant from a witnessed upper bound after partial search.

    Attributes:
        exact (bool): Whether the minimum over all cuts is known.
        lowerBound (float): Proven lower bound, equal to upperBound for an exact result.
        upperBound (float | None): Smallest witnessed ratio, or null before any cut is evaluated.
        cut (tuple[str, ...]): Witness vertices on one side of that cut.
        evaluatedCuts (int): Number of distinct partitions evaluated.
        reason (str): Completion, witnessed violation, or exhausted resource budget.
        stage (str): CachedQuotient, FreshSpectralReduction or ExactEnumeration.
        edgeChurn (float): Changed-edge fraction when a cached partition was reused; otherwise zero.
        skippedPriorityCuts (int): Configured subsets absent from or equal to the whole boundary.
        durationSeconds (float): Elapsed wall time for this calculation, including projection.
        inputs (dict[str, int | float]): Effective budgets, ceilings, reduction state and graph dimensions.
        scheduler (dict[str, object]): Causal PID diagnostics; empty when adaptive scheduling is inactive.
    """

    exact: bool
    lowerBound: float
    upperBound: float | None
    cut: tuple[str, ...]
    evaluatedCuts: int
    reason: str
    stage: str = "ExactEnumeration"
    edgeChurn: float = 0.0
    skippedPriorityCuts: int = 0
    durationSeconds: float = 0.0
    inputs: dict[str, int | float] = field(factory=dict)
    scheduler: dict[str, object] = field(factory=dict)

    def report(self) -> dict[str, Any]:
        """
        Serialize the certificate without labelling an upper bound as a measured constant.

        Returns:
            dict[str, Any]: JSON-compatible diagnostics for status and logs.
        """
        return asdict(self)


def compute_cheeger(
    graph: nx.Graph[str],
    computation: CheegerComputation | None = None,
    *,
    limits: CheegerComputation | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    cache_scope: str = "",
) -> CheegerResult:
    """
    Evaluate configured cuts before an exhaustive Gray-code traversal under bounded work.

    Args:
        graph (nx.Graph[str]): Relation projected to simple undirected, unweighted edges without loops.
        computation (CheegerComputation | None): Local budgets and ordered preferred vertex subsets.
        limits (CheegerComputation | None): Optional administrator ceilings; local requests cannot exceed them.
        minimum (float | None): Stop early when a witnessed cut disproves this inclusive hard minimum.
        maximum (float | None): Use certified lower bounds to disprove this inclusive hard maximum.
        cache_scope (str): Stable boundary identity isolating controllers and partitions; set it for independent graphs.

    Returns:
        CheegerResult: Exact constant, a certified violation, or an explicitly incomplete search.
    """
    timed = time.perf_counter()
    started = time.monotonic()

    # Resolve boundary-specific requests against administrator ceilings before
    # allocating matrices or starting either form of exponential cut search.
    options = computation or CheegerComputation()
    ceilings = limits or CheegerComputation()
    budgets: dict[str, int | float] = {}
    for name, default in (("maxVertices", 20), ("maxCuts", 524287), ("timeoutSeconds", 5.0)):
        ceiling = getattr(ceilings, name) or default
        requested = getattr(options, name)
        if limits is not None and requested is not None and requested > ceiling:
            raise ValueError(f"Cheeger {name}={requested} exceeds operator ceiling {ceiling}")
        budgets[name] = ceiling if requested is None else requested

    # Opting in locally is not enough when an operator supplies cluster limits.
    # Both the feature and its individual resource allowances must be permitted.
    reduction = options.reduction
    if reduction.enabled and limits is not None:
        allowed = ceilings.reduction
        if not allowed.enabled:
            raise ValueError("Cheeger reduction is disabled by the operator")
        for name in ("maxVertices", "components", "supernodes", "cacheEntries", "maxEdgeChurn"):
            if getattr(reduction, name) > getattr(allowed, name):
                raise ValueError(f"Cheeger reduction {name}={getattr(reduction, name)} exceeds operator ceiling {getattr(allowed, name)}")
        if reduction.cache and not allowed.cache:
            raise ValueError("Cheeger reduction cache is disabled by the operator")
        reduction = evolve(
            reduction,
            strategy="CacheFirst" if "CacheFirst" in (reduction.strategy, allowed.strategy) else "AdaptivePID",
            targetSeconds=allowed.targetSeconds,
        )

    # Carry the effective configuration into every return path so an incomplete
    # answer can be explained using the limits that actually governed this call.
    inputs: dict[str, int | float] = {
        **budgets,
        **{
            "operator" + name[0].upper() + name[1:]: getattr(ceilings, name) or default
            for name, default in (("maxVertices", 20), ("maxCuts", 524287), ("timeoutSeconds", 5.0))
            if limits is not None
        },
        "vertices": len(graph),
        "priorityCuts": len(options.priorityCuts),
        "priorityVertices": sum(map(len, options.priorityCuts)),
        "reductionEnabled": int(reduction.enabled),
    }

    ticket: RefreshTicket | None = None
    cache_attempted = refreshed = False

    def finish(value: CheegerResult) -> CheegerResult:
        scheduler = (
            finish_refresh(ticket, time.perf_counter() - timed, cache_attempted=cache_attempted, refreshed=refreshed) if ticket else {}
        )
        return evolve(value, durationSeconds=time.perf_counter() - timed, inputs=dict(inputs), scheduler=scheduler)

    if len(graph) > budgets["maxVertices"]:
        return finish(CheegerResult(False, 0.0, None, (), 0, f"VertexLimit: at most {budgets['maxVertices']} vertices per boundary"))

    # This metric counts connections, not weights or directions. Parallel edges
    # collapse to one connection, and self-loops cannot cross a cut.
    simple: nx.Graph[str] = nx.Graph()
    simple.graph["cheegerCacheScope"] = cache_scope
    simple.add_nodes_from(graph)
    simple.add_edges_from(graph.edges())
    simple.remove_edges_from(nx.selfloop_edges(simple))
    inputs["edges"] = simple.number_of_edges()
    n = len(simple)

    # A disconnected component already witnesses a zero-size edge boundary;
    # neither reduction nor exhaustive enumeration can improve on zero.
    if n < 2:
        return finish(CheegerResult(True, 0.0, 0.0, (), 0, "Complete"))
    if not nx.is_connected(simple):
        return finish(CheegerResult(True, 0.0, 0.0, tuple(next(iter(nx.connected_components(simple)))), 0, "Complete"))

    def decision(lower: float, upper: float) -> str | None:
        """
        Decide configured inclusive bounds only when the certificate proves the answer.

        Args:
            lower (float): Proven lower bound.
            upper (float): Proven upper bound.

        Returns:
            str | None: Decision reason, or null when another tier is required.
        """

        # The true value lies somewhere in [lower, upper]. Reject only when that
        # whole interval misses a bound; accept only when it fits every bound.
        if minimum is not None and upper + 1e-9 * max(1.0, abs(upper), abs(minimum)) < minimum:
            return "MinimumViolated"
        if maximum is not None and lower - 1e-9 * max(1.0, abs(lower), abs(maximum)) > maximum:
            return "MaximumViolated"
        lower_satisfies = minimum is None or lower + 1e-9 * max(1.0, abs(lower), abs(minimum)) >= minimum
        upper_satisfies = maximum is None or upper - 1e-9 * max(1.0, abs(upper), abs(maximum)) <= maximum
        return "BoundsSatisfied" if (minimum is not None or maximum is not None) and lower_satisfies and upper_satisfies else None

    reduction_lower: float | None = None
    reduction_upper: float | None = None
    reduction_cut: tuple[str, ...] = ()

    # Reduction can settle a policy without finding the exact constant. Numeric
    # callers without thresholds deliberately continue to the exact-search path.
    if reduction.enabled and (minimum is not None or maximum is not None) and len(simple) <= reduction.maxVertices:
        certificates = []
        if reduction.cache and reduction.strategy == "AdaptivePID":
            ticket = begin_refresh(simple, reduction)
            inputs["adaptivePID"] = 1
            inputs["targetSeconds"] = reduction.targetSeconds

        # Tier 1 reuses a partition, but rescores its cuts against today's edges.
        if reduction.cache and not (ticket and ticket.scheduled):
            cache_attempted = True
            if (cached := cached_quotient(simple, reduction)) is not None:
                certificates.append(cached)
        for certificate in certificates:
            inputs["reductionEvaluatedCuts"] = certificate.evaluatedCuts
            if reason := decision(certificate.lowerBound, certificate.upperBound):
                return finish(
                    CheegerResult(
                        False,
                        certificate.lowerBound,
                        certificate.upperBound,
                        certificate.cut,
                        certificate.evaluatedCuts,
                        reason,
                        certificate.stage,
                        certificate.edgeChurn,
                    )
                )

        # Tier 2 rebuilds the spectral partition when reuse is unavailable or
        # its interval cannot decide the policy. Retain these bounds for fallback.
        fresh = fresh_spectral_reduction(simple, reduction)
        refreshed = True
        inputs["reductionEvaluatedCuts"] = fresh.evaluatedCuts
        reduction_lower, reduction_upper, reduction_cut = fresh.lowerBound, fresh.upperBound, fresh.cut
        if reason := decision(fresh.lowerBound, fresh.upperBound):
            return finish(
                CheegerResult(
                    False,
                    fresh.lowerBound,
                    fresh.upperBound,
                    fresh.cut,
                    fresh.evaluatedCuts,
                    reason,
                    fresh.stage,
                    fresh.edgeChurn,
                )
            )

    # Tier 3 enumerates original-graph cuts. A bit represents one vertex, so
    # intersections and neighbor counts can use native integer operations.
    nodes = list(simple)
    indices = {node: index for index, node in enumerate(nodes)}
    neighbors = [sum(1 << indices[neighbor] for neighbor in simple[node]) for node in nodes]
    degrees = [value.bit_count() for value in neighbors]
    total = (1 << (n - 1)) - 1
    all_nodes = (1 << n) - 1
    deadline = started + budgets["timeoutSeconds"]
    evaluated = skipped = best_subset = 0
    best: float | None = None
    priorities: set[int] = set()

    def result(reason: str) -> CheegerResult:
        upper = best
        witness = tuple(node for i, node in enumerate(nodes) if best_subset & (1 << i))

        # A partial exact search must not discard a better reduced witness.
        # Only complete enumeration collapses the lower and upper bounds.
        if reduction_upper is not None and (upper is None or reduction_upper < upper):
            upper, witness = reduction_upper, reduction_cut
        exact = reason == "Complete"
        return finish(
            CheegerResult(
                exact,
                upper if exact and upper is not None else reduction_lower or 0.0,
                upper,
                witness,
                evaluated,
                reason,
                "ExactEnumeration",
                0.0,
                skipped,
            )
        )

    def exhausted() -> str | None:
        # These checks govern exact-search work. Quotient cuts are additional,
        # and the earlier spectral operations are not interrupted mid-call.
        if evaluated >= budgets["maxCuts"]:
            return "CutBudget"
        if time.monotonic() >= deadline:
            return "TimeBudget"
        return None

    def observe(subset: int, cut: int) -> str | None:
        nonlocal evaluated, best, best_subset
        evaluated += 1

        # Expansion is crossing edges per vertex on the smaller side. One low
        # ratio disproves a minimum even before every other cut has been tried.
        size = subset.bit_count()
        value = cut / min(size, n - size)
        if best is None or value < best:
            best, best_subset = value, subset
        if evaluated == total:
            return "Complete"
        if minimum is not None and value + 1e-9 * max(1.0, abs(value), abs(minimum)) < minimum:
            return "MinimumViolated"
        return None

    # Subtree and Namespace rules can visit boundaries with different names.
    # Missing members never turn a preferred subset into a different cut.
    for group in options.priorityCuts:
        if not set(group) <= indices.keys() or len(group) == n:
            skipped += 1
            continue

        # Store only the orientation that excludes the last vertex, so a subset
        # and its complement cannot consume the priority budget twice.
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
    # Python's arbitrary-width integers keep this path correct above 63 vertices,
    # while int.bit_count() performs the hot population counts in native code.
    bit_count = int.bit_count
    max_cuts = int(budgets["maxCuts"])
    monotonic = time.monotonic
    next_time_check = 1
    has_priorities = bool(priorities)
    has_minimum = minimum is not None
    for step in range(1, total + 1):
        if evaluated >= max_cuts:
            return result("CutBudget")

        # Preserve bounded cancellation checks before steps 1, 256, 512, ...
        if step == next_time_check:
            if monotonic() >= deadline:
                return result("TimeBudget")
            next_time_check = 256 if step == 1 else step + 256

        # Consecutive Gray-code masks flip just one vertex. When adding it,
        # edges to outside vertices enter the cut and edges to inside vertices
        # leave it: degree - 2 * inside_neighbors. Removal reverses that delta.
        next_subset = step ^ (step >> 1)
        changed = subset ^ next_subset
        vertex = changed.bit_length() - 1
        delta = degrees[vertex] - 2 * bit_count(neighbors[vertex] & subset)
        cut += delta if next_subset & changed else -delta
        subset = next_subset

        # Even a previously tested priority cut must update the Gray-code state;
        # skip only its evaluation, not the transition needed by the next mask.
        if has_priorities and subset in priorities:
            continue

        # Keep the exhaustive hot path inline: a Python function call per cut is
        # material at the default 524,287-cut ceiling.
        evaluated += 1
        size = bit_count(subset)
        complement_size = n - size
        value = cut / (size if size < complement_size else complement_size)
        if best is None or value < best:
            best, best_subset = value, subset
        if evaluated < total and has_minimum and minimum is not None and value + 1e-9 * max(1.0, abs(value), abs(minimum)) < minimum:
            return result("MinimumViolated")

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

    # Callers of this scalar API cannot inspect uncertainty. Refuse a partial
    # answer instead of silently presenting a witnessed upper bound as exact.
    result = compute_cheeger(graph, computation, limits=limits)
    if not result.exact:
        raise CheegerIncomplete(result)
    assert result.upperBound is not None
    return result.upperBound
