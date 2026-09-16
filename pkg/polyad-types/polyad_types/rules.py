"""
Constrain graph structure with explicit combinatorial and spectral measurements.
"""

from __future__ import annotations

import math
from typing import Literal

from attrs import field, frozen

from polyad_types.network import NetworkAccess

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
    requires a sparse cut. These bounds do not measure execution throughput.

    Attributes:
        minimum (float | None): Inclusive minimum Cheeger constant.
        maximum (float | None): Inclusive maximum Cheeger constant.
    """

    minimum: float | None = None
    maximum: float | None = None

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
        cheeger (Cheeger | None): Optional exact edge-expansion bounds, limited to 20 vertices.
    """

    scope: Literal["Boundary", "Subtree"] = "Subtree"
    enforcement: Literal["Namespace", "Referenced"] = "Namespace"
    relation: Literal["admission", "connections"] = "admission"
    limits: dict[str, int] = field(factory=dict)
    shapes: tuple[Literal["acyclic", "connected", "tree", "planar"], ...] = ()
    spectrum: Spectrum | None = None
    network: NetworkAccess | None = None
    cheeger: Cheeger | None = None

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
