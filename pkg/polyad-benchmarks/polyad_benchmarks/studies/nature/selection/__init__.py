"""
Reuse the completed Nature planner with a study-local expanded capability catalog.
"""

from __future__ import annotations

import threading

from demo import nature
from polyad_benchmarks.studies.nature.capabilities import catalog

__all__ = ("select",)


_PLANNER_LOCK = threading.Lock()


def select(output: str, current: dict[str, nature.Placement], settings: nature.Settings) -> nature.Plan:
    """
    Select compatible routes without modifying the original root demo's source.

    The existing demo reads its catalog from a module constant. This adapter
    substitutes the study catalog only during one serialized parent-side call
    and restores the original even when selection fails. Workers never select
    compositions or modify this catalog.

    Args:
        output (str): Squared or enriched result required by the current environment.
        current (dict[str, nature.Placement]): Currently selected service incarnations.
        settings (nature.Settings): Fixed cost, process-overlap and search ceilings.

    Returns:
        nature.Plan: Cheapest compatible composition admitted by the original planner.
    """
    requirement = nature.Requirement(output, 6 if output == "squared" else 4, 7 if output == "squared" else 6)

    # The reused planner reads a module-global catalog; serialize replacement and restore on errors.
    with _PLANNER_LOCK:
        previous = nature.CATALOG
        try:
            vars(nature)["CATALOG"] = catalog()
            return nature.natural_selection(requirement, current, settings)
        finally:
            nature.CATALOG = previous
