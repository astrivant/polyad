"""
Monitor six services while local Soul policies respond to demand and constraints.

    producer -> monitoring parent -> A / B / C / E / F / H -> verified results
                                    |   |   |   |   |   |
                                    worker generations, owned by each service

The parent owns routing and aggregates every process report. Service strategies
change admission, batching and child generations; the observer retains history
for the Matplotlib figures generated after verified shutdown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import nature
from polyad_benchmarks.studies.soul.runtime.engine import PopulationMonitor

if TYPE_CHECKING:
    from typing import Any


class Monitor(PopulationMonitor):
    """
    Keep six square-capable services while adapting worker trees and work assignment.
    """

    def __init__(self, config: dict[str, Any], adaptive: bool) -> None:
        """
        Select the Soul policy for the shared process-monitoring lifecycle.

        Args:
            config (dict[str, Any]): Finite load and disturbance recipe.
            adaptive (bool): Apply strategies or retain the fixed reference behavior.
        """
        super().__init__("soul", config, adaptive)

    def select(self, output: str) -> nature.Plan:
        """
        Keep application capabilities constant while measuring local adaptation.

        Args:
            output (str): Required square-result contract.

        Returns:
            nature.Plan: Six independent routes through real service processes.
        """
        if output != "squared":
            raise ValueError("Soul study keeps its application output contract fixed")
        routes = tuple((nature.Placement(name, nature.SQUARE),) for name in ("A", "B", "C", "E", "F", "H"))
        return nature.Plan(routes, 6, nature.expansion(routes))
