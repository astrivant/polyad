"""
Bound operator polling costs without changing ownership or write ordering.
"""

from __future__ import annotations

import math
import os

from attrs import frozen


@frozen
class OperatorTuning:
    """
    Configure pauses after completed loop passes, in seconds.

    Attributes:
        rescan (float): Namespace rescan pause, bounded below inventory expiry.
        consume (float): Pause after delivering one notification per owned shard.
        metrics (float): Cached metrics publication pause.
        backlog (float): Shared queue sampling pause.
    """

    rescan: float = 5
    consume: float = 1
    metrics: float = 5
    backlog: float = 5

    def __attrs_post_init__(self) -> None:
        """
        Reject busy loops and intervals incompatible with telemetry freshness.

        Returns:
            None: No return value.
        """
        for name, low, high in (("rescan", 1, 15), ("consume", 0.1, 5), ("metrics", 1, 5), ("backlog", 1, 5)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"operator {name} interval must be between {low} and {high} seconds")

    @classmethod
    def from_environment(cls) -> OperatorTuning:
        """
        Read Helm-projected settings once, before starting operator workers.

        Returns:
            OperatorTuning: Validated process settings with existing defaults.
        """
        defaults = cls()
        return cls(
            **{
                name: float(os.environ.get(f"POLYAD_{name.upper()}_INTERVAL_SECONDS", str(getattr(defaults, name))))
                for name in ("rescan", "consume", "metrics", "backlog")
            }
        )
