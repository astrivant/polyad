"""
Constrain graph structure with explicit combinatorial and spectral measurements.
"""

from __future__ import annotations

import math
from typing import Literal

from attrs import field, frozen

from polyad_types.networking.access import NetworkAccess

__all__ = (
    "Cheeger",
    "CheegerComputation",
    "CheegerReduction",
    "LIMITS",
    "Spectrum",
    "StructuralPolicy",
)


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
class Cheeger:
    """
    Bound exact edge expansion on the simple undirected projection.

    A cut's ratio is its crossing-edge count divided by its smaller side's
    vertex count. The constant is the minimum ratio over all cuts. Raising
    the minimum rejects severe structural bottlenecks; lowering the maximum
    requires a sparse cut. Throughput policies can select these structural
    targets using application-reported rates; the constant itself is edge expansion.

    Attributes:
        minimum (float | None): Inclusive nonnegative minimum; raise it to reject sparse bottlenecks, or omit for no lower bound.
        maximum (float | None): Inclusive nonnegative maximum; lower it to require a sparse cut, or omit for no upper bound.
    """

    minimum: float | None = field(default=None, metadata={"schema": {"minimum": 0}})
    maximum: float | None = field(default=None, metadata={"schema": {"minimum": 0}})

    def __attrs_post_init__(self) -> None:
        """
        Reject invalid or contradictory bounds.

        Returns:
            None: No return value.
        """
        for value in (self.minimum, self.maximum):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
            ):
                raise ValueError("Cheeger bounds must be finite and nonnegative")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Cheeger minimum must not exceed maximum")


@frozen
class CheegerReduction:
    """
    Configure the opt-in, certificate-preserving Cheeger reduction tiers.

    Attributes:
        enabled (bool): Request reduced search before exact enumeration. The operator must also enable it.
        maxVertices (int): Largest boundary admitted to dense spectral reduction.
        components (int): Nontrivial Laplacian eigenvectors retained by fresh spectral reduction.
        supernodes (int): Maximum quotient vertices searched before exact enumeration.
        cache (bool): Reevaluate a recently cached quotient partition on the current graph first.
        cacheEntries (int): Maximum process-local partitions retained by the operator.
        maxEdgeChurn (float): Largest changed-edge fraction eligible for cached partition reuse.
        strategy (Literal['AdaptivePID', 'CacheFirst']): Preferred scheduler; either operator or policy can select legacy CacheFirst.
        targetSeconds (float): Soft mean computation-time goal; the operator value governs when administrator limits are supplied.
        feedback (Literal['CertificateGap', 'ComputationTime']): Adaptive PID objective; the administrator chooses the effective signal.
        targetRelativeError (float): Soft certified relative-error objective; 0.25 means 25 percent, not a hard accuracy guarantee.
    """

    enabled: bool = False
    maxVertices: int = field(default=256, metadata={"schema": {"minimum": 2, "maximum": 4096}})
    components: int = field(default=4, metadata={"schema": {"minimum": 1, "maximum": 64}})
    supernodes: int = field(default=8, metadata={"schema": {"minimum": 2, "maximum": 64}})
    cache: bool = True
    cacheEntries: int = field(default=128, metadata={"schema": {"minimum": 1, "maximum": 4096}})
    maxEdgeChurn: float = field(default=0.1, metadata={"schema": {"minimum": 0, "maximum": 1}})
    strategy: Literal["AdaptivePID", "CacheFirst"] = "AdaptivePID"
    targetSeconds: float = field(default=0.0015, metadata={"schema": {"minimum": 0.000001, "maximum": 300}})
    feedback: Literal["CertificateGap", "ComputationTime"] = "CertificateGap"
    targetRelativeError: float = field(default=0.25, metadata={"schema": {"minimum": 0.000001, "maximum": 100}})

    def __attrs_post_init__(self) -> None:
        """
        Reject ambiguous switches and unbounded reduction work.

        Returns:
            None: No return value.
        """
        if type(self.enabled) is not bool or type(self.cache) is not bool:
            raise ValueError("Cheeger reduction enabled and cache must be booleans")
        if type(self.maxVertices) is not int or not 2 <= self.maxVertices <= 4096:
            raise ValueError("Cheeger reduction maxVertices must be an integer between 2 and 4096")
        if type(self.components) is not int or not 1 <= self.components <= 64:
            raise ValueError("Cheeger reduction components must be an integer between 1 and 64")
        if type(self.supernodes) is not int or not 2 <= self.supernodes <= 64:
            raise ValueError("Cheeger reduction supernodes must be an integer between 2 and 64")
        if type(self.cacheEntries) is not int or not 1 <= self.cacheEntries <= 4096:
            raise ValueError("Cheeger reduction cacheEntries must be an integer between 1 and 4096")
        churn = self.maxEdgeChurn
        if isinstance(churn, bool) or not isinstance(churn, (int, float)) or not math.isfinite(churn) or not 0 <= churn <= 1:
            raise ValueError("Cheeger reduction maxEdgeChurn must be finite and between zero and one")
        if self.strategy not in {"AdaptivePID", "CacheFirst"}:
            raise ValueError("Cheeger reduction strategy must be AdaptivePID or CacheFirst")
        target = self.targetSeconds
        if type(target) not in (int, float) or not math.isfinite(target) or not 0.000001 <= target <= 300:
            raise ValueError("Cheeger reduction targetSeconds must be finite and between 0.000001 and 300")
        if self.feedback not in {"CertificateGap", "ComputationTime"}:
            raise ValueError("Cheeger reduction feedback must be CertificateGap or ComputationTime")
        accuracy = self.targetRelativeError
        if type(accuracy) not in (int, float) or not math.isfinite(accuracy) or not 0.000001 <= accuracy <= 100:
            raise ValueError("Cheeger reduction targetRelativeError must be finite and between 0.000001 and 100")


@frozen
class CheegerComputation:
    """
    Budget exact search and optionally try certified quotient reductions first.

    Attributes:
        maxVertices (int | None): Boundary vertex cap; null inherits the operator ceiling, normally 20.
        maxCuts (int | None): Unique cut evaluation budget; null inherits the operator ceiling, normally 524287.
        timeoutSeconds (float | None): Cooperative time budget; null inherits the operator ceiling, normally five seconds.
        priorityCuts (tuple[tuple[str, ...], ...]): Ordered vertex subsets to examine before exhaustive search; complements are equivalent.
        reduction (CheegerReduction): Optional cached quotient and fresh spectral stages before exact enumeration.
    """

    maxVertices: int | None = field(default=None, metadata={"schema": {"minimum": 2, "maximum": 4096}})
    maxCuts: int | None = field(default=None, metadata={"schema": {"minimum": 1, "maximum": 2147483647}})
    timeoutSeconds: float | None = field(default=None, metadata={"schema": {"minimum": 0.001, "maximum": 300}})
    priorityCuts: tuple[tuple[str, ...], ...] = field(
        default=(),
        metadata={
            "schema": {
                "maxItems": 64,
                "items": {
                    "minItems": 1,
                    "maxItems": 4096,
                    "x-kubernetes-list-type": "set",
                    "items": {"type": "string", "minLength": 1, "maxLength": 253},
                },
            }
        },
    )
    reduction: CheegerReduction = field(factory=CheegerReduction)

    def __attrs_post_init__(self) -> None:
        """
        Reject unbounded work, ambiguous cuts and invalid public Python inputs.

        Returns:
            None: No return value.
        """
        for value, lower, upper in ((self.maxVertices, 2, 4096), (self.maxCuts, 1, 2147483647)):
            if value is not None and (type(value) is not int or not lower <= value <= upper):
                raise ValueError("Cheeger vertex and cut budgets must be bounded positive integers")
        seconds = self.timeoutSeconds
        if seconds is not None and (
            isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0.001 <= seconds <= 300
        ):
            raise ValueError("Cheeger timeoutSeconds must be finite and between 0.001 and 300")
        if len(self.priorityCuts) > 64 or any(
            isinstance(cut, str)
            or not 1 <= len(cut) <= 4096
            or any(not isinstance(node, str) or not node or len(node) > 253 for node in cut)
            or len(set(cut)) != len(cut)
            for cut in self.priorityCuts
        ):
            raise ValueError("Cheeger priorityCuts require at most 64 nonempty subsets with unique vertex names")


@frozen
class StructuralPolicy:
    """
    Apply reusable mathematical constraints to each graph boundary and its subtree.

    Attributes:
        scope (Literal['Boundary', 'Subtree']): Whether selected policies propagate to descendant boundaries.
        enforcement (Literal['Namespace', 'Referenced']): Mandatory namespace policy or explicitly selected policy.
        relation (Literal['admission', 'connections']): Directed edge relation used for local measurements.
        limits (dict[str, int]): Inclusive upper bounds on named combinatorial measurements.
        shapes (tuple[Literal['acyclic', 'connected', 'tree', 'planar'], ...]): Required graph properties.
        spectrum (Spectrum | None): Optional undirected spectral constraints, limited to 256 vertices.
        network (NetworkAccess | None): Mandatory or referenced traffic restrictions inherited by graph descendants.
        cheeger (Cheeger | None): Optional edge-expansion bounds proven exactly or by a conservative interval.
        cheegerComputation (CheegerComputation): Cut priorities and per-calculation budgets within operator ceilings.
    """

    scope: Literal["Boundary", "Subtree"] = "Subtree"
    enforcement: Literal["Namespace", "Referenced"] = "Namespace"
    relation: Literal["admission", "connections"] = "admission"
    limits: dict[str, int] = field(factory=dict)
    shapes: tuple[Literal["acyclic", "connected", "tree", "planar"], ...] = ()
    spectrum: Spectrum | None = None
    network: NetworkAccess | None = None
    cheeger: Cheeger | None = None
    cheegerComputation: CheegerComputation = field(factory=CheegerComputation)

    def __attrs_post_init__(self) -> None:
        """
        Reject unknown measurements and invalid bounds before evaluating a policy.

        Returns:
            None: No return value.
        """
        if self.scope not in {"Boundary", "Subtree"}:
            raise ValueError("unknown policy scope")
        if self.enforcement not in {"Namespace", "Referenced"} or self.relation not in {"admission", "connections"}:
            raise ValueError("unknown policy enforcement or graph relation")
        if set(self.limits) - LIMITS or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in self.limits.values()):
            raise ValueError("policy limits require known measurements and nonnegative integers")
        if set(self.shapes) - {"acyclic", "connected", "tree", "planar"}:
            raise ValueError("unknown graph shape")
