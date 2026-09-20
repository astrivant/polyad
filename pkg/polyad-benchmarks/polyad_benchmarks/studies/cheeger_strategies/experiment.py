"""
Measure production tier selection, graph churn and certificate accuracy locally.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import random
import time
from dataclasses import asdict, replace
from importlib.metadata import version
from typing import TYPE_CHECKING
from unittest.mock import patch

import networkx as nx
from attrs import asdict as attributes
from attrs import evolve

from polyad.graph import cheeger as solver
from polyad.graph.pid import AccuracyTargetPID, certificate_gap
from polyad.graph.reduction import cached_quotient, clear_reduction_cache, fresh_spectral_reduction
from polyad_benchmarks.cheeger_reduction import approximate_cut, exact_cut, graph_case
from polyad_benchmarks.studies.cheeger_strategies.pid import CacheTargetConfig, CacheTargetPID, RefreshConfig, RefreshPID
from polyad_types.graphs.rules import CheegerComputation, CheegerReduction

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

__all__ = (
    "Experiment",
    "STRATEGIES",
    "edge_churn",
    "named_graph",
    "numbered_graph",
    "policy_decision",
    "pid_report",
    "rewire",
    "run",
    "trace_selector",
)


STRATEGIES = (
    "Exact",
    "PCA",
    "Fresh spectral",
    "Cached spectral",
    "Selector",
    "Cache-first selector",
    "Adaptive-target PID",
    "Accuracy-target PID",
)


def numbered_graph(graph: nx.Graph[str]) -> nx.Graph[int]:
    """
    Assign consecutive integer identifiers in sorted name order for the independent oracle.

    Args:
        graph (nx.Graph[str]): Named boundary.

    Returns:
        nx.Graph[int]: Isomorphic simple graph.
    """

    # The independent enumerator expects bit positions 0..n-1. Sorting names
    # lets its cut be translated back to the same service identities afterward.
    indices = {node: index for index, node in enumerate(sorted(graph))}
    result: nx.Graph[int] = nx.Graph()
    result.add_nodes_from(indices.values())
    result.add_edges_from((indices[left], indices[right]) for left, right in graph.edges())
    return result


def named_graph(graph: nx.Graph[int]) -> nx.Graph[str]:
    """
    Assign stable padded names to generated integer vertices.

    Args:
        graph (nx.Graph[int]): Generated graph.

    Returns:
        nx.Graph[str]: Isomorphic named boundary.
    """

    # Padding keeps lexical order aligned with numeric order (v002 before v010),
    # which stabilizes matrix rows and cached partition labels across runs.
    result: nx.Graph[str] = nx.Graph()
    result.add_nodes_from(f"v{node:03d}" for node in graph)
    result.add_edges_from((f"v{left:03d}", f"v{right:03d}") for left, right in graph.edges())
    return result


def edge_churn(previous: nx.Graph[str], current: nx.Graph[str]) -> float:
    """
    Measure Jaccard edge distance, matching the production cache gate.

    Args:
        previous (nx.Graph[str]): Baseline topology.
        current (nx.Graph[str]): Current topology.

    Returns:
        float: Changed edges divided by the edge union.
    """

    # Ignore endpoint orientation. Replacing one edge counts as both a removal
    # and an addition, so this distance differs from the requested replacement rate.
    old = {frozenset(edge) for edge in previous.edges()}
    new = {frozenset(edge) for edge in current.edges()}
    return len(old ^ new) / max(1, len(old | new))


def rewire(graph: nx.Graph[str], fraction: float, seed: int) -> nx.Graph[str]:
    """
    Replace distinct baseline edges while preserving connectivity and edge count.

    Args:
        graph (nx.Graph[str]): Baseline graph, including trees.
        fraction (float): Requested fraction of original edges to replace.
        seed (int): Stable random seed.

    Returns:
        nx.Graph[str]: Rewired graph; saturated graphs may achieve less churn.
    """
    changed = graph.copy()
    rng = random.Random(seed)

    # Canonicalize endpoints before shuffling. NetworkX's nonedge orientation
    # can depend on Python's hash seed even when our random seed is fixed.
    old = sorted(tuple(sorted(edge)) for edge in graph.edges())
    new = sorted(tuple(sorted(edge)) for edge in nx.non_edges(graph))
    rng.shuffle(old)
    rng.shuffle(new)

    # Dense graphs may not have enough unused edges to meet the requested rate.
    # Limit the attempt here; record achieved churn separately from the request.
    target = min(round(fraction * len(old)), len(new))
    removed = 0
    for addition in new:
        if removed == target:
            break

        # Add first, then remove: a tree needs the new connection before any of
        # its bridges can be replaced without temporarily disconnecting the graph.
        changed.add_edge(*addition)

        # Try removals until one preserves connectivity. If none succeeds, the
        # for-else branch rolls back the addition to preserve the edge count.
        for index, edge in enumerate(old):
            changed.remove_edge(*edge)
            if nx.is_connected(changed):
                old.pop(index)
                removed += 1
                break
            changed.add_edge(*edge)
        else:
            changed.remove_edge(*addition)

    return changed


def policy_decision(lower: float, upper: float | None, policy: dict[str, float]) -> str:
    """
    Classify an interval with the same inclusive tolerance as admission.

    Args:
        lower (float): Conservative lower bound.
        upper (float | None): Witnessed upper bound, if known.
        policy (dict[str, float]): Optional minimum and maximum.

    Returns:
        str: Pass, Violate or Unknown.
    """
    if upper is None:
        return "Unknown"

    # Numerical tolerance matches admission's inclusive thresholds. An interval
    # straddling a threshold is unknown, even if its upper witness looks accurate.
    minimum, maximum = policy.get("minimum"), policy.get("maximum")
    lower_tolerance = 1e-9 * max(1.0, lower, minimum or 0, maximum or 0)
    upper_tolerance = 1e-9 * max(1.0, upper, minimum or 0, maximum or 0)
    if (minimum is not None and upper + upper_tolerance < minimum) or (maximum is not None and lower - lower_tolerance > maximum):
        return "Violate"
    if (minimum is None or lower + lower_tolerance >= minimum) and (maximum is None or upper - upper_tolerance <= maximum):
        return "Pass"
    return "Unknown"


def trace_selector(graph: nx.Graph[str], settings: CheegerComputation, policy: dict[str, float]) -> dict[str, Any]:
    """
    Observe actual reducer calls without simulating the production selection logic.

    Args:
        graph (nx.Graph[str]): Current graph.
        settings (CheegerComputation): Local controls also used as administrator ceilings.
        policy (dict[str, float]): Bounds sent directly to compute_cheeger.

    Returns:
        dict[str, Any]: Solver certificate and all attempted reduction tiers.
    """
    attempts: list[dict[str, Any]] = []

    def observe(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def measured(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            certificate = function(*args, **kwargs)

            # A fresh certificate is successful work, but never a cache hit.
            # Keep failed cache lookups too, so an attempt is not confused with reuse.
            attempts.append(
                {
                    "stage": name,
                    "cacheHit": name == "CachedQuotient" and certificate is not None,
                    "certificateReturned": certificate is not None,
                    "edgeChurn": certificate.edgeChurn if certificate is not None else None,
                    "evaluatedCuts": certificate.evaluatedCuts if certificate else 0,
                    "durationSeconds": time.perf_counter() - started,
                }
            )
            return certificate

        return measured

    # Wrap the real call sites only for this synchronous measurement. The
    # production selector still decides which tiers run; the study just observes.
    with (
        patch.object(solver, "cached_quotient", observe("CachedQuotient", cached_quotient)),
        patch.object(solver, "fresh_spectral_reduction", observe("FreshSpectralReduction", fresh_spectral_reduction)),
    ):
        result = solver.compute_cheeger(graph, settings, limits=settings, minimum=policy.get("minimum"), maximum=policy.get("maximum"))

    # The final report counts its finishing tier, not all preceding work. Sum
    # reducer attempts separately without double-counting a reduced final result.
    exact_cuts = result.evaluatedCuts if result.stage == "ExactEnumeration" else 0
    return {
        **result.report(),
        "attempts": attempts,
        "totalEvaluatedCuts": exact_cuts + sum(item["evaluatedCuts"] for item in attempts),
        "cacheHit": any(item["stage"] == "CachedQuotient" and item["cacheHit"] for item in attempts),
    }


def pid_report(graph: nx.Graph[str], settings: CheegerReduction, controller: RefreshPID, *, force_refresh: bool = False) -> dict[str, Any]:
    """
    Time the real cached or fresh reducer chosen by a causal refresh controller.

    Args:
        graph (nx.Graph[str]): Current boundary; no exact reference is passed in.
        settings (CheegerReduction): Identical partition dimensions and quotient size.
        controller (RefreshPID): State retained across one ordered churn trajectory.
        force_refresh (bool): Honor an accuracy controller's saturated request for zero reuse.

    Returns:
        dict[str, Any]: Current certificate, all attempted work and controller telemetry.
    """
    scheduled = force_refresh or controller.refresh_due()
    attempts: list[dict[str, Any]] = []
    certificate = None

    # Match the raw cached comparator's permissive churn gate. Otherwise a
    # static gate could masquerade as a PID-triggered refresh in this experiment.
    settings = evolve(settings, cache=True, maxEdgeChurn=1)
    for name, reducer in (("CachedQuotient", cached_quotient), ("FreshSpectralReduction", fresh_spectral_reduction)):
        if name == "CachedQuotient" and scheduled:
            continue
        started = time.perf_counter()
        certificate = reducer(graph, settings)
        attempts.append(
            {
                "stage": name,
                "cacheHit": name == "CachedQuotient" and certificate is not None,
                "certificateReturned": certificate is not None,
                "edgeChurn": certificate.edgeChurn if certificate is not None else None,
                "evaluatedCuts": certificate.evaluatedCuts if certificate is not None else 0,
                "durationSeconds": time.perf_counter() - started,
            }
        )
        if certificate is not None:
            break
    if certificate is None:
        raise ValueError("PID comparator requires a spectral certificate")

    # A miss must really rebuild the partition, and counts as going to cache.
    # Controller failure is deliberately independent of certificate correctness.
    refreshed = certificate.stage == "FreshSpectralReduction"
    telemetry = controller.observe(cache_attempted=not scheduled, refreshed=refreshed)
    return {
        **asdict(certificate),
        "attempts": attempts,
        "totalEvaluatedCuts": sum(attempt["evaluatedCuts"] for attempt in attempts),
        "cacheHit": any(attempt["cacheHit"] for attempt in attempts),
        "pid": {**telemetry, "refreshScheduled": scheduled, "cacheAttempted": not scheduled},
    }


class Experiment:
    """
    Keep exact references, reproducible snapshots and individual timing samples together.

    Attributes:
        config (dict[str, Any]): Frozen recipe for all sweeps.
        records (list[dict[str, Any]]): Unaggregated measurements.
        graphs (dict[str, dict[str, Any]]): Snapshot edges and independently enumerated truth.
    """

    config: dict[str, Any]
    records: list[dict[str, Any]]
    graphs: dict[str, dict[str, Any]]

    def __init__(self, config: dict[str, Any]) -> None:
        """
        Validate bounded study work before constructing any graph.

        Args:
            config (dict[str, Any]): Explicit study axes.
        """

        # Ground truth is exhaustive, so keep the requested size sweeps and
        # quotient enumerations small enough for a repeatable local experiment.
        if not 4 <= config["fixedVertices"] <= 20 or any(not 4 <= n <= 20 for n in config["vertexCounts"]):
            raise ValueError("exact study references require between 4 and 20 vertices")
        if not 1 <= config["repetitions"] <= 10 or any(not 2 <= k <= 10 for k in config["supernodes"]):
            raise ValueError("study repeats and quotient size are capped at ten")
        warmup = config.get("preferredWarmupObservations", 12)
        if type(warmup) is not int or not 0 <= warmup <= 128:
            raise ValueError("preferred warmup must contain between 0 and 128 observations")

        # Older recipes remain valid and omit the optional, study-only comparator.
        self._pid_config = RefreshConfig(**config["pidRefresh"]) if "pidRefresh" in config else None
        self._feedback = config.get("pidFeedback", {})
        self._accuracy = config.get("pidAccuracy", {})
        self._outer_config = None
        for controls in (self._feedback, self._accuracy):
            if not controls.get("enabled", False):
                continue
            self._outer_config = CacheTargetConfig(**self._feedback.get("controller", {}))
            observations = controls["observationsPerPhase"]
            phases = controls["phases"]
            if type(observations) is not int or not self._outer_config.updateEvery <= observations <= 128 or not 1 <= len(phases) <= 12:
                raise ValueError("PID feedback requires bounded phases with at least one complete outer window each")
            for phase in phases:
                if not 0 <= phase["replacement"] <= 1 or not math.isfinite(phase["timeTargetSeconds"]) or phase["timeTargetSeconds"] <= 0:
                    raise ValueError("PID feedback requires valid churn and positive finite time targets")
                accuracy_target = phase.get("relativeErrorTarget", config.get("preferredRelativeError", 0.25))
                if isinstance(accuracy_target, bool) or not math.isfinite(accuracy_target) or not 0.000001 <= accuracy_target <= 100:
                    raise ValueError("PID feedback requires a finite relative-error target between 0.000001 and 100")

        self.config = config
        self.records: list[dict[str, Any]] = []
        self.graphs: dict[str, dict[str, Any]] = {}

    def graph(self, topology: str, vertices: int, seed: int) -> nx.Graph[str]:
        """
        Construct a named boundary with stable lexical vertex order.

        Args:
            topology (str): Graph family.
            vertices (int): Boundary size.
            seed (int): Generator seed.

        Returns:
            nx.Graph[str]: Simple, connected unweighted graph.
        """
        return named_graph(graph_case(topology, vertices, seed))

    def settings(self, **overrides: Any) -> CheegerComputation:
        """
        Build opt-in controls under explicit administrator ceilings.

        Args:
            **overrides (Any): Reduction fields varied by a controlled sweep.

        Returns:
            CheegerComputation: Complete settings used by the production solver.
        """

        # Most sweeps vary one reduction field while inheriting the same recipe.
        # Generous outer budgets keep those comparisons separate from budget probes.
        reduction = CheegerReduction(
            **{
                "enabled": True,
                "components": self.config["fixedComponents"],
                "supernodes": self.config["fixedSupernodes"],
                "maxEdgeChurn": self.config["fixedCacheChurnLimit"],
                "targetSeconds": self.config.get("preferredTimeTargetSeconds", 0.0015),
                "targetRelativeError": self.config.get("preferredRelativeError", 0.25),
                **overrides,
            }
        )
        return CheegerComputation(maxVertices=21, maxCuts=1048575, timeoutSeconds=self.config["timeoutSeconds"], reduction=reduction)

    def snapshot(self, graph: nx.Graph[str]) -> tuple[str, float]:
        """
        Retain reconstructable edges and an independent exhaustive oracle once per snapshot.

        Args:
            graph (nx.Graph[str]): Original graph being measured.

        Returns:
            tuple[str, float]: Content hash and exact unnormalized edge expansion.
        """

        # Hash topology rather than an experiment label: repeated timings can
        # share one exact reference while retaining reconstructable graph inputs.
        document = {"nodes": sorted(graph), "edges": sorted(sorted(edge) for edge in graph.edges())}
        identity = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        if identity not in self.graphs:
            reference = exact_cut(numbered_graph(graph))
            self.graphs[identity] = {**document, "exact": reference.value, "oracleCuts": reference.evaluatedCuts}

        return identity, float(self.graphs[identity]["exact"])

    def prime(self, graph: nx.Graph[str], settings: CheegerComputation) -> None:
        """
        Give every independent trial the same baseline cache state outside its timed region.

        Args:
            graph (nx.Graph[str]): Cache baseline.
            settings (CheegerComputation): Matching partition parameters.

        Returns:
            None: Reset and prime the local production cache.
        """

        # Independent comparisons must not inherit a previous method's refresh.
        # Priming is untimed; persistent-history experiments intentionally skip it.
        clear_reduction_cache()
        if settings.reduction.cache:
            fresh_spectral_reduction(graph, settings.reduction)

    def record(
        self,
        graph: nx.Graph[str],
        baseline: nx.Graph[str],
        settings: CheegerComputation,
        policy: dict[str, float],
        context: dict[str, Any],
        strategy: str,
        *,
        controller: RefreshPID | None = None,
        outer: CacheTargetPID | None = None,
        accuracy: AccuracyTargetPID | None = None,
    ) -> None:
        """
        Measure one strategy and audit its certificate and policy answer against exhaustive truth.

        Args:
            graph (nx.Graph[str]): Current snapshot.
            baseline (nx.Graph[str]): Priming snapshot, or previous event for a persistent timeline.
            settings (CheegerComputation): Fixed or explicitly varied solver controls.
            policy (dict[str, float]): Bounds used for decision comparisons.
            context (dict[str, Any]): Sweep, seed and repetition identity.
            strategy (str): Named baseline, selector or PID cached spectral comparator.
            controller (RefreshPID | None): Independent PID trajectory state, if selected.
            outer (CacheTargetPID | None): Slower computation-time feedback, if selected.
            accuracy (AccuracyTargetPID | None): Alternative certificate-gap feedback; never fed oracle error.

        Returns:
            None: Append a checked measurement without aggregating away variability.
        """

        # Compute reference answers before starting the method's clock. No method
        # receives this exact value unless it independently enumerates the graph.
        identity, truth = self.snapshot(graph)
        baseline_id, _ = self.snapshot(baseline)
        if strategy == "Cache-first selector":
            settings = evolve(settings, reduction=evolve(settings.reduction, strategy="CacheFirst"))
        cpu_started = time.process_time()
        started = time.perf_counter()

        # Selector uses real policy shortcuts. Exact removes both those shortcuts
        # and reduction so its measured work covers exhaustive enumeration.
        if strategy in {"Selector", "Cache-first selector"}:
            report = trace_selector(graph, settings, policy)
        elif strategy in {"PID cached spectral", "Fixed-target PID", "Adaptive-target PID", "Accuracy-target PID"}:
            if controller is None:
                raise ValueError("PID cached spectral requires its own trajectory controller")
            report = pid_report(graph, settings.reduction, controller, force_refresh=accuracy is not None and accuracy.target == 0)
        elif strategy == "Exact":
            report = solver.compute_cheeger(graph, evolve(settings, reduction=CheegerReduction())).report()
        elif strategy == "PCA":
            report = approximate_cut(
                numbered_graph(graph),
                settings.reduction.components,
                min(settings.reduction.supernodes, len(graph)),
            )
            report["cut"] = [sorted(graph)[index] for index in report["cut"]]
        else:
            certificate = (
                cached_quotient(graph, evolve(settings.reduction, maxEdgeChurn=1))
                if strategy == "Cached spectral"
                else fresh_spectral_reduction(graph, evolve(settings.reduction, cache=False))
            )

            # Raw cached cuts deliberately bypass the churn gate to measure how
            # the old partition degrades. Only Selector enforces deployment gates.
            if certificate is None:
                raise ValueError("forced cached comparator requires a matching baseline partition")
            report = asdict(certificate)
        duration = time.perf_counter() - started
        if outer is not None:
            if controller is None:
                raise ValueError("outer PID requires an inner refresh controller")

            # Feedback sees completed inner work only. Record total method time
            # separately so its own update overhead is never silently free.
            report["outerPid"] = outer.observe(duration, context.get("timeTargetSeconds", settings.reduction.targetSeconds))
            controller.set_target(outer.target)
            duration = time.perf_counter() - started
        if accuracy is not None:
            if controller is None or outer is not None:
                raise ValueError("accuracy PID requires an inner controller and replaces the time-target outer loop")
            report["accuracyPid"] = accuracy.observe(
                report["lowerBound"], report["upperBound"], context.get("relativeErrorTarget", settings.reduction.targetRelativeError)
            )
            controller.set_target(accuracy.target)
            duration = time.perf_counter() - started
        cpu_duration = time.process_time() - cpu_started

        # Compare the interval's mathematical conclusion with the actual runtime
        # decision. An exhausted selector remains Unknown, not an inferred pass.
        lower, upper = report["lowerBound"], report["upperBound"]
        certificate_answer = policy_decision(lower, upper, policy)
        answer = certificate_answer
        if strategy in {"Selector", "Cache-first selector"} and not report["exact"]:
            answer = {"BoundsSatisfied": "Pass", "MinimumViolated": "Violate", "MaximumViolated": "Violate"}.get(
                report["reason"], "Unknown"
            )

        # Reject unsafe measurements before publication: every reported interval
        # must contain the reference, and every decisive answer must agree with it.
        oracle = policy_decision(truth, truth, policy)
        contains = lower <= truth + 1e-8 and (upper is None or truth <= upper + 1e-8)
        agrees = None if answer == "Unknown" else answer == oracle
        if not contains or agrees is False:
            raise ValueError(f"unsafe certificate from {strategy} on {identity}")

        # Save individual observations, including unresolved answers and all-tier
        # work. Plotting can aggregate them without hiding misses or overshoots.
        work = report.get("totalEvaluatedCuts", report["evaluatedCuts"])
        self.records.append(
            {
                **context,
                **({"pid": report["pid"]} if "pid" in report else {}),
                **({"outerPid": report["outerPid"]} if "outerPid" in report else {}),
                **({"accuracyPid": report["accuracyPid"]} if "accuracyPid" in report else {}),
                **({"scheduler": report["scheduler"]} if report.get("scheduler") else {}),
                "strategy": strategy,
                "graphId": identity,
                "baselineId": baseline_id,
                "vertices": len(graph),
                "edges": graph.number_of_edges(),
                "density": nx.density(graph),
                "edgeChurn": edge_churn(baseline, graph),
                "components": settings.reduction.components,
                "supernodes": settings.reduction.supernodes,
                "cacheChurnLimit": settings.reduction.maxEdgeChurn,
                "maxCuts": settings.maxCuts,
                "timeoutSeconds": settings.timeoutSeconds,
                "exactValue": truth,
                "policy": policy,
                "decision": answer,
                "certificateDecision": certificate_answer,
                "oracleDecision": oracle,
                "decisionMatchesOracle": agrees,
                "intervalContainsExact": contains,
                "stage": report.get("stage", strategy),
                "reason": report.get("reason", "Witness"),
                "exact": report.get("exact", strategy == "Exact"),
                "lowerBound": lower,
                "upperBound": upper,
                "cut": report["cut"],
                "relativeError": (upper - truth) / truth if upper is not None and truth else None,
                "intervalWidth": upper - lower if upper is not None else None,
                "certificateGap": certificate_gap(lower, upper),
                "durationSeconds": duration,
                "cpuSeconds": cpu_duration,
                "averageMillicores": 1000 * cpu_duration / duration if duration > 0 else None,
                "millicoreSeconds": 1000 * cpu_duration,
                "totalEvaluatedCuts": work,
                "workExceedsCutBudget": work > (settings.maxCuts or 524287),
                "elapsedExceedsTimeBudget": duration > (settings.timeoutSeconds or 5),
                "attempts": report.get("attempts", []),
                "cacheHit": report.get("cacheHit", False),
                "settings": attributes(settings),
            }
        )

    def compare(self, graph: nx.Graph[str], baseline: nx.Graph[str], settings: CheegerComputation, context: dict[str, Any]) -> None:
        """
        Compare all methods on identical snapshots with a fixed baseline policy.

        Args:
            graph (nx.Graph[str]): Current graph.
            baseline (nx.Graph[str]): Cache and threshold baseline.
            settings (CheegerComputation): Shared budgets.
            context (dict[str, Any]): Sweep and generator seed.

        Returns:
            None: Retain each timing replicate and rotate method order deterministically.
        """

        # Hold the baseline policy fixed as edges change; otherwise the goalposts
        # would move along with the graph whose adaptation we want to measure.
        policy = {"minimum": self.snapshot(baseline)[1] * 0.75}

        # Rotate ordering to reduce warmup/order bias, then restore the same
        # initial cache before each method. Repeats are timing samples only.
        for repeat in range(self.config["repetitions"]):
            order = list(STRATEGIES)
            random.Random(context["seed"] + repeat).shuffle(order)
            for strategy in order:
                self.prime(baseline, settings)
                inner, outer = None, None
                accuracy = None
                warmups = 0
                if strategy == "Adaptive-target PID":
                    outer = CacheTargetPID(self._outer_config or CacheTargetConfig())
                    inner = RefreshPID(replace(self._pid_config or RefreshConfig(), targetCacheRate=outer.target))
                    warmups = self.config.get("preferredWarmupObservations", 12)

                    # Learn only from the common baseline, never the changed
                    # graph or its oracle. Keep warmup separate from query cost.
                    for _ in range(warmups):
                        started = time.perf_counter()
                        pid_report(baseline, settings.reduction, inner)
                        outer.observe(time.perf_counter() - started, settings.reduction.targetSeconds)
                        inner.set_target(outer.target)
                elif strategy == "Accuracy-target PID":
                    accuracy = AccuracyTargetPID(self._outer_config or CacheTargetConfig())
                    inner = RefreshPID(replace(self._pid_config or RefreshConfig(), targetCacheRate=accuracy.target))
                    warmups = self.config.get("preferredWarmupObservations", 12)
                    for _ in range(warmups):
                        report = pid_report(baseline, settings.reduction, inner, force_refresh=accuracy.target == 0)
                        accuracy.observe(report["lowerBound"], report["upperBound"], settings.reduction.targetRelativeError)
                        inner.set_target(accuracy.target)
                elif strategy in {"Selector", "Cache-first selector"}:
                    warmups = self.config.get("preferredWarmupObservations", 12)
                    options = (
                        evolve(settings, reduction=evolve(settings.reduction, strategy="CacheFirst"))
                        if strategy == "Cache-first selector"
                        else settings
                    )

                    # Give both complete selectors the same baseline-only history.
                    # The adaptive selector learns from whole-call cost, including
                    # exact fallback, without seeing future churn or oracle error.
                    for _ in range(warmups):
                        trace_selector(baseline, options, policy)
                self.record(
                    graph,
                    baseline,
                    settings,
                    policy,
                    {**context, "repeat": repeat, "warmupObservations": warmups},
                    strategy,
                    controller=inner,
                    outer=outer,
                    accuracy=accuracy,
                )

    def comparisons(self) -> None:
        """
        Sweep graph churn, dimensions, compression, size and density on matched snapshots.

        Returns:
            None: Add comparisons with one changed axis at a time except the declared dimension grid.
        """
        config = self.config

        # First vary topology change with fixed reduction settings. Then cross
        # dimensions and quotient size on each unchanged baseline graph.
        for topology in config["topologies"]:
            for seed in config["seeds"]:
                baseline = self.graph(topology, config["fixedVertices"], seed)
                for fraction in config["edgeReplacement"]:
                    current = rewire(baseline, fraction, seed)
                    self.compare(
                        current, baseline, self.settings(), {"sweep": "churn", "topology": topology, "seed": seed, "replacement": fraction}
                    )

                for components in config["components"]:
                    for supernodes in config["supernodes"]:
                        self.compare(
                            baseline,
                            baseline,
                            self.settings(components=components, supernodes=supernodes),
                            {"sweep": "reduction", "topology": topology, "seed": seed},
                        )

        # Size and density sweeps isolate different sources of cost: the number
        # of original vertices versus the number of edges between those vertices.
        for seed in config["seeds"]:
            for vertices in config["vertexCounts"]:
                graph = self.graph(config["fixedTopology"], vertices, seed)
                self.compare(graph, graph, self.settings(), {"sweep": "size", "topology": config["fixedTopology"], "seed": seed})

            for density in config["densities"]:
                generated: nx.Graph[int] = nx.gnp_random_graph(config["fixedVertices"], density, seed=seed)

                # Connect random components so the answer is not trivially zero.
                # Keep the repaired edges and achieved density in the raw snapshot.
                components = list(nx.connected_components(generated))
                for left, right in zip(components, components[1:], strict=False):
                    generated.add_edge(min(left), min(right))
                named = named_graph(generated)
                self.compare(
                    named, named, self.settings(), {"sweep": "density", "topology": "random", "seed": seed, "probability": density}
                )

        self.pid_churn()

    def pid_churn(self) -> None:
        """
        Replay the matched churn snapshots with a persistent PID and spectral cache.

        Returns:
            None: Add one causal observation per churn level and timing repetition.
        """
        if self._pid_config is None or not self._pid_config.enabled:
            return
        config = self.config
        settings = self.settings(maxEdgeChurn=1)
        for topology in config["topologies"]:
            for seed in config["seeds"]:
                baseline = self.graph(topology, config["fixedVertices"], seed)
                policy = {"minimum": self.snapshot(baseline)[1] * 0.75}
                for repeat in range(config["repetitions"]):
                    # Reset between independent repeats, never between levels.
                    # The baseline and oracle stay outside the measured callback.
                    self.prime(baseline, settings)
                    controller = RefreshPID(self._pid_config)
                    for step, fraction in enumerate(config["edgeReplacement"]):
                        self.record(
                            rewire(baseline, fraction, seed),
                            baseline,
                            settings,
                            policy,
                            {
                                "sweep": "churn",
                                "topology": topology,
                                "seed": seed,
                                "repeat": repeat,
                                "replacement": fraction,
                                "step": step,
                            },
                            "PID cached spectral",
                            controller=controller,
                        )

    def pid_feedback(self) -> None:
        """
        Compare fixed and adaptive cache targets through independently changed inputs.

        Returns:
            None: Retain paired trajectories, real latencies and both feedback histories.
        """
        self._feedback_replay("pid-feedback", self._feedback)

    def pid_accuracy(self) -> None:
        """
        Vary accuracy goals independently of churn and the original time objective.

        Returns:
            None: Preserve actual error, certified uncertainty and the CPU price of each response.
        """
        self._feedback_replay("pid-accuracy", self._accuracy)

    def _feedback_replay(self, sweep: str, controls: dict[str, Any]) -> None:
        """
        Replay paired snapshots without giving any controller the independent oracle.

        Args:
            sweep (str): Output series identity for separate time and accuracy experiments.
            controls (dict[str, Any]): Validated phases and observation count.

        Returns:
            None: Record every query, including warmup transients and saturated objectives.
        """
        if self._outer_config is None or not controls.get("enabled", False):
            return
        config = self.config
        inner_config = replace(self._pid_config or RefreshConfig(), targetCacheRate=self._outer_config.initialCacheRate)
        settings = self.settings(maxEdgeChurn=1)
        for seed in config["seeds"]:
            baseline = self.graph(config["fixedTopology"], config["fixedVertices"], seed)
            policy = {"minimum": self.snapshot(baseline)[1] * 0.75}
            for repeat in range(config["repetitions"]):
                strategies = ["Cached spectral", "Fresh spectral", "Fixed-target PID", "Adaptive-target PID", "Accuracy-target PID"]
                random.Random(seed + repeat).shuffle(strategies)
                for strategy in strategies:
                    # Each method gets the same complete sequence and its own
                    # primed cache. Shuffling trajectories limits ordering bias
                    # without leaking another method's partition or PID state.
                    self.prime(baseline, settings)
                    inner = RefreshPID(inner_config) if strategy.endswith("PID") else None
                    outer = CacheTargetPID(self._outer_config) if strategy == "Adaptive-target PID" else None
                    accuracy = AccuracyTargetPID(self._outer_config) if strategy == "Accuracy-target PID" else None
                    for phase_index, phase in enumerate(controls["phases"]):
                        graph = rewire(baseline, phase["replacement"], seed)
                        for sample in range(controls["observationsPerPhase"]):
                            self.record(
                                graph,
                                baseline,
                                settings,
                                policy,
                                {
                                    "sweep": sweep,
                                    "topology": config["fixedTopology"],
                                    "seed": seed,
                                    "repeat": repeat,
                                    "step": phase_index * controls["observationsPerPhase"] + sample,
                                    "phase": phase["name"],
                                    "phaseIndex": phase_index,
                                    "phaseStep": sample,
                                    "replacement": phase["replacement"],
                                    "timeTargetSeconds": phase["timeTargetSeconds"],
                                    "relativeErrorTarget": phase.get("relativeErrorTarget", settings.reduction.targetRelativeError),
                                },
                                strategy,
                                controller=inner,
                                outer=outer,
                                accuracy=accuracy,
                            )

    def thresholds(self) -> None:
        """
        Cross policy bounds and cache churn gates while keeping spectral settings fixed.

        Returns:
            None: Record actual tier transitions at thresholds normalized by study-only oracle values.
        """
        config = self.config
        for seed in config["seeds"]:
            baseline = self.graph(config["fixedTopology"], config["fixedVertices"], seed)
            for fraction in config["edgeReplacement"]:
                graph = rewire(baseline, fraction, seed)
                truth = self.snapshot(graph)[1]

                # Oracle-normalized thresholds are experimental inputs, not a
                # production dependency. They probe both sides of the true boundary.
                for kind in config["policyKinds"]:
                    for ratio in config["thresholdRatios"]:
                        threshold = ratio * truth
                        policy = {kind: threshold} if kind != "range" else {"minimum": 0.9 * threshold, "maximum": 1.1 * threshold}
                        for gate in config["cacheChurnLimits"]:
                            settings = self.settings(maxEdgeChurn=gate)

                            # Each gate sees the same old partition, rather than a
                            # refresh performed by an earlier threshold trial.
                            for strategy in ("Selector", "Cache-first selector"):
                                self.prime(baseline, settings)
                                self.record(
                                    graph,
                                    baseline,
                                    settings,
                                    policy,
                                    {
                                        "sweep": "threshold",
                                        "topology": config["fixedTopology"],
                                        "seed": seed,
                                        "repeat": 0,
                                        "replacement": fraction,
                                        "policyKind": kind,
                                        "thresholdRatio": ratio,
                                    },
                                    strategy,
                                )

    def controls(self) -> None:
        """
        Exercise cache capacity, disabled switches and finite work controls separately.

        Returns:
            None: Record cache eviction and whether each finite budget resolves the selected policy.
        """
        config = self.config
        for seed in config["seeds"]:
            graph = self.graph(config["fixedTopology"], config["fixedVertices"], seed)
            truth = self.snapshot(graph)[1]
            base = self.settings()

            # Change one budget or switch per case. Requiring minimum == h makes
            # the lower-bound proof difficult and exposes incomplete searches.
            cases = [(f"cut budget {cuts}", evolve(base, maxCuts=cuts)) for cuts in config["maxCuts"]]
            cases += [(f"time budget {seconds}s", evolve(base, timeoutSeconds=seconds)) for seconds in config["timeouts"]]
            cases += [
                ("reduction disabled", evolve(base, reduction=CheegerReduction())),
                ("cache disabled", evolve(base, reduction=evolve(base.reduction, cache=False))),
                ("spectral size cap", evolve(base, reduction=evolve(base.reduction, maxVertices=8))),
                ("boundary size cap", evolve(base, maxVertices=8)),
                ("priority witness", evolve(base, maxCuts=1, priorityCuts=(tuple(sorted(graph)[: len(graph) // 2]),))),
            ]
            for name, settings in cases:
                for strategy in ("Selector", "Cache-first selector"):
                    self.prime(graph, settings)
                    self.record(
                        graph,
                        graph,
                        settings,
                        {"minimum": truth},
                        {"sweep": "controls", "topology": config["fixedTopology"], "seed": seed, "repeat": 0, "case": name},
                        strategy,
                    )

            # Distinct names give otherwise identical boundaries separate cache
            # entries. Three round-robin passes expose filling and later eviction.
            for entries in config["cacheEntries"]:
                settings = self.settings(cacheEntries=entries)
                for strategy in ("Selector", "Cache-first selector"):
                    clear_reduction_cache()
                    for step in range(config["boundaryCount"] * 3):
                        boundary = step % config["boundaryCount"]
                        named = nx.relabel_nodes(graph, {node: f"b{boundary}-{node}" for node in graph})
                        self.record(
                            named,
                            named,
                            settings,
                            {"maximum": float(len(named))},
                            {
                                "sweep": "cache-capacity",
                                "topology": config["fixedTopology"],
                                "seed": seed,
                                "repeat": 0,
                                "cacheEntries": entries,
                                "step": step,
                                "boundary": boundary,
                            },
                            strategy,
                        )

    def timeline(self) -> None:
        """
        Follow persistent cache state through steady periods, churn and vertex membership changes.

        Returns:
            None: Retain an ordered event sequence showing all three production tiers.
        """
        config = self.config
        for seed in config["seeds"]:
            baseline = self.graph(config["fixedTopology"], config["fixedVertices"], seed)
            mild = rewire(baseline, 1 / baseline.number_of_edges(), seed)
            burst = rewire(baseline, 0.6, seed)
            joined = burst.copy()
            joined.add_edge("new-service", sorted(burst)[0])

            # Alternate easy maximum policies with tight minimum probes. The
            # service's departure restores an earlier vertex set, testing reuse
            # of its old partition rather than forcing another membership miss.
            events = [
                ("cold", baseline, "maximum"),
                ("steady", baseline, "maximum"),
                ("small churn", mild, "maximum"),
                ("steady", mild, "maximum"),
                ("tight minimum", mild, "minimum"),
                ("churn burst", burst, "maximum"),
                ("steady", burst, "maximum"),
                ("service joins", joined, "maximum"),
                ("steady", joined, "maximum"),
                ("service leaves", burst, "maximum"),
                ("tight minimum", burst, "minimum"),
                ("steady", burst, "maximum"),
            ]

            # Reset once per sequence, not once per event: cache history is the
            # independent variable here. Top-level churn refers to the last event.
            for strategy in ("Selector", "Cache-first selector"):
                clear_reduction_cache()
                previous = baseline
                for step, (event, graph, kind) in enumerate(events):
                    policy = {kind: self.snapshot(graph)[1] if kind == "minimum" else float(len(graph))}
                    self.record(
                        graph,
                        previous,
                        self.settings(),
                        policy,
                        {"sweep": "timeline", "topology": config["fixedTopology"], "seed": seed, "repeat": 0, "step": step, "event": event},
                        strategy,
                    )
                    previous = graph


def run(root: Path) -> dict[str, Any]:
    """
    Execute and save the registered local experiment without Kubernetes access.

    Args:
        root (Path): Prepared study artifact directory.

    Returns:
        dict[str, Any]: Recipe, raw measurements, graph snapshots and runtime versions.
    """
    from polyad_benchmarks.refresh import write_json

    config = json.loads((root / "inputs/cheeger-strategies.json").read_text())
    experiment = Experiment(config)

    # Warm library imports and kernels before collecting timing replicates.
    solver.compute_cheeger(nx.path_graph(6))
    fresh_spectral_reduction(nx.path_graph([str(n) for n in range(6)]), experiment.settings().reduction)

    # Every sweep must finish before marking the study complete. Always remove
    # study-created cache entries, including when a certificate audit fails.
    try:
        for sweep in (
            experiment.comparisons,
            experiment.thresholds,
            experiment.controls,
            experiment.timeline,
            experiment.pid_feedback,
            experiment.pid_accuracy,
        ):
            sweep()
            print(f"cheeger-strategies: {sweep.__name__}: {len(experiment.records)} measurements", flush=True)
    finally:
        clear_reduction_cache()

    # Runtime versions and thread settings give the host-specific timings context;
    # the refresh protocol separately records source and input fingerprints.
    result = {
        "study": "cheeger-strategies",
        "runId": config["runId"],
        "complete": True,
        "recipe": config,
        "records": experiment.records,
        "graphs": experiment.graphs,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "numpy": version("numpy"),
            "networkx": version("networkx"),
            "logicalCPUs": os.cpu_count(),
            "cpuMeasurement": "process_time: all threads in the isolated study process; excludes oracle, baseline priming and warmup",
            "threadLimits": {name: os.getenv(name) for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")},
        },
    }
    write_json(root / "outputs/cheeger-strategies/results.json", result)
    return result
