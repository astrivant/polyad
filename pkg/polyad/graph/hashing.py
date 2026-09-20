"""
Hash labeled graph structure recursively using child boundary digests.
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from polyad.graph.gates import DelayGate, Gate
    from polyad.graph.workloads import Work, Workload

__all__ = (
    "digest",
    "shape_hash",
)


_visiting: ContextVar[tuple[int, ...]] = ContextVar("polyad_shape_visiting", default=())


def digest(value: object) -> str:
    """
    Hash canonical JSON with a versioned domain separator.

    Args:
        value (object): JSON-compatible structural description.

    Returns:
        str: SHA-256 hexadecimal digest.
    """
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(b"polyad-shape-v1\0" + encoded).hexdigest()


def shape_hash(owner: object, members: Sequence[tuple[Work, Workload]], routes: Mapping[str, Gate | DelayGate]) -> str:
    """
    Combine labeled dependency edges, routing expressions and nested graph hashes.

    Args:
        owner (object): Boundary identity used only to detect invalid containment cycles.
        members (Sequence[tuple[Work, Workload]]): Current immutable boundary snapshot.
        routes (Mapping[str, Gate | DelayGate]): Admission expressions.

    Returns:
        str: Shape digest independent of submission order and runtime statistics.
    """
    visited = _visiting.get()
    if id(owner) in visited:
        raise ValueError("graph containment must be acyclic; repeat execution through application control flow")
    token = _visiting.set((*visited, id(owner)))
    try:
        nodes = [
            {
                "name": work.name,
                "requires": sorted(set(work.requires)),
                "child": getattr(unit, "shape_hash", None),
                "route": asdict(routes[work.name]) if work.name in routes else None,
            }
            for work, unit in sorted(members, key=lambda member: member[0].name)
        ]
        return digest({"kind": "dependency-graph", "nodes": nodes})
    finally:
        _visiting.reset(token)
