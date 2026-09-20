"""
Monitor capability mutation, survival and retirement above concurrent Soul policies.

    before: A B C E F H each square their assigned inputs
    during: A' and F' compute square-plus-one; B -> D and E -> G compose it
    after:  the square contract returns and the parent restores six square routes

Every selected service also runs the Soul study's modular SDK strategies.
The monitoring parent checks output contracts, retains every job until completion
and aggregates the measurements from old and replacement process generations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_benchmarks.studies.nature.selection import select
from polyad_benchmarks.studies.soul.runtime.engine import PopulationMonitor

if TYPE_CHECKING:
    from typing import Any

    import nature

__all__ = ("Monitor",)


class Monitor(PopulationMonitor):
    """
    Let Natural Selection choose the service composition and Soul adapt its workers.
    """

    def __init__(self, config: dict[str, Any], adaptive: bool) -> None:
        """
        Configure the parent that owns capability changes and measured routing.

        Args:
            config (dict[str, Any]): Required outcomes and bounded load recipe.
            adaptive (bool): Select new compositions or retain the initial population.
        """
        super().__init__("nature", config, adaptive)

    def select(self, output: str) -> nature.Plan:
        """
        Apply the expanded catalog to the current outcome and process constraints.

        Args:
            output (str): Required result label for the upcoming load phase.

        Returns:
            nature.Plan: Admitted composition preserving all output contracts.
        """
        return select(output, {name: member.placement for name, member in self.active.items()}, self.settings)
