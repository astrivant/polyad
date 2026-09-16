"""
Report application demand and completed work against one graph incarnation and revision.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from attrs import frozen


@frozen
class ThroughputSample:
    """
    Supply one aggregate measurement from a graph's designated application reporter.

    Attributes:
        graph (str): Graph name in the API listener's namespace.
        graphUid (str): Exact target incarnation, preventing name reuse.
        generation (int): Topology revision whose throughput was measured.
        observedAt (str): Timezone-aware end of the application's measurement window.
        unit (str): Work unit matching the graph's throughput policy.
        offeredPerSecond (float): Aggregate arrival or demanded work rate over that window.
        completedPerSecond (float): Successfully completed work rate over the same window.
        kind (Literal['Graph', 'PolyGraph']): Target boundary kind.
    """

    graph: str
    graphUid: str
    generation: int
    observedAt: str
    unit: str
    offeredPerSecond: float
    completedPerSecond: float
    kind: Literal["Graph", "PolyGraph"] = "Graph"

    def __attrs_post_init__(self) -> None:
        """
        Reject invalid rates, missing identities and ambiguous timestamps.

        Returns:
            None: No return value.
        """
        if not self.graph or not self.graphUid or self.kind not in {"Graph", "PolyGraph"} or not self.unit:
            raise ValueError("throughput samples require a graph identity and work unit")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("throughput samples require a positive graph generation")
        if datetime.fromisoformat(self.observedAt.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("throughput observations require a timezone")
        for value in (self.offeredPerSecond, self.completedPerSecond):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("throughput rates must be finite and nonnegative")
