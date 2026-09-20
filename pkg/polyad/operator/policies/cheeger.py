"""
Apply deployment-wide Cheeger computation ceilings to policy and feedback evaluations.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

from polyad_types.graphs.rules import CheegerComputation, CheegerReduction

if TYPE_CHECKING:
    from typing import Literal

__all__ = ("computation_limits",)


def _boolean(name: str, default: bool) -> bool:
    """
    Parse a Helm-rendered boolean environment variable.

    Args:
        name (str): Environment variable name.
        default (bool): Value used when the variable is absent.

    Returns:
        bool: Parsed switch.
    """
    return os.getenv(name, str(default)).lower() == "true"


def computation_limits() -> CheegerComputation:
    """
    Read the local administrator's budgets without inheriting remote policy overrides.

    Returns:
        CheegerComputation: Validated exact and opt-in reduction ceilings.
    """
    return CheegerComputation(
        maxVertices=int(os.getenv("POLYAD_CHEEGER_MAX_VERTICES", "20")),
        maxCuts=int(os.getenv("POLYAD_CHEEGER_MAX_CUTS", "524287")),
        timeoutSeconds=float(os.getenv("POLYAD_CHEEGER_TIMEOUT_SECONDS", "5")),
        reduction=CheegerReduction(
            enabled=_boolean("POLYAD_CHEEGER_REDUCTION_ENABLED", False),
            maxVertices=int(os.getenv("POLYAD_CHEEGER_REDUCTION_MAX_VERTICES", "256")),
            components=int(os.getenv("POLYAD_CHEEGER_REDUCTION_COMPONENTS", "4")),
            supernodes=int(os.getenv("POLYAD_CHEEGER_REDUCTION_SUPERNODES", "8")),
            cache=_boolean("POLYAD_CHEEGER_REDUCTION_CACHE", True),
            cacheEntries=int(os.getenv("POLYAD_CHEEGER_REDUCTION_CACHE_ENTRIES", "128")),
            maxEdgeChurn=float(os.getenv("POLYAD_CHEEGER_REDUCTION_MAX_EDGE_CHURN", "0.1")),
            strategy=cast("Literal['AdaptivePID', 'CacheFirst']", os.getenv("POLYAD_CHEEGER_REDUCTION_STRATEGY", "AdaptivePID")),
            targetSeconds=float(os.getenv("POLYAD_CHEEGER_REDUCTION_TARGET_SECONDS", "0.0015")),
        ),
    )
