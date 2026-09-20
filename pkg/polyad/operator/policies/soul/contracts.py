"""
Define the Soul searching stages' internal handoff and durable annotation identities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from polyad_types.resources import GROUP

if TYPE_CHECKING:
    from typing import Any

    from polyad_types.api.throughput import ThroughputSample
    from polyad_types.graphs.topology import ThroughputPolicy, Topology
    from polyad_types.networking.traffic import TrafficWeights

__all__ = (
    "Proposal",
    "SAMPLE",
    "STATE",
    "Search",
)


SAMPLE = f"{GROUP}/throughput-sample"
STATE = f"{GROUP}/throughput-state"


@dataclass
class Search:
    """
    Carry one reconciliation's observations, stabilization history and public decision.

    Attributes:
        graph (Topology): Parsed current boundary.
        policy (ThroughputPolicy): Administrator-approved adaptation policy.
        status (dict[str, Any]): Public throughput status, refined as each stage runs.
        current (float | None): Exact current Cheeger value, or None when computation was limited.
        clock (float): Evaluation time as Unix seconds.
        capacity (str): Observed execution inventory fingerprint.
        previous (dict[str, Any]): Persisted history before this evaluation.
        state (dict[str, Any]): Updated stabilization history and rolling change budget.
    """

    graph: Topology
    policy: ThroughputPolicy
    status: dict[str, Any]
    current: float | None = None
    clock: float = 0
    capacity: str = ""
    previous: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Proposal:
    """
    Carry a recommendation and the measurement evidence required to admit its write.

    Attributes:
        spec (dict[str, Any]): Proposed graph specification with approved parameter changes.
        sample (ThroughputSample): Fresh measurement that justified the recommendation.
        observed (float): Measurement window end as Unix seconds.
        traffic_targets (tuple[TrafficWeights, ...] | None): Selected splits to revalidate in Headroom mode.
    """

    spec: dict[str, Any]
    sample: ThroughputSample
    observed: float
    traffic_targets: tuple[TrafficWeights, ...] | None
