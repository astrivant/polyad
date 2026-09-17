"""
Apply deployment-wide Cheeger computation ceilings to policy and feedback evaluations.
"""

from __future__ import annotations

import os

from polyad_types.rules import CheegerComputation


def computation_limits() -> CheegerComputation:
    """
    Read the local administrator's budgets without inheriting remote policy overrides.

    Returns:
        CheegerComputation: Validated maximum vertices, cut evaluations and cooperative runtime.
    """
    return CheegerComputation(
        maxVertices=int(os.getenv("POLYAD_CHEEGER_MAX_VERTICES", "20")),
        maxCuts=int(os.getenv("POLYAD_CHEEGER_MAX_CUTS", "524287")),
        timeoutSeconds=float(os.getenv("POLYAD_CHEEGER_TIMEOUT_SECONDS", "5")),
    )
