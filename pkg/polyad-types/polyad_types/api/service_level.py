"""
Define service-level objectives and application observations for persistent services.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from attrs import field, frozen

__all__ = (
    "AdaptationBudget",
    "ServiceLevelPolicy",
    "ServiceLevelReport",
)


@frozen
class AdaptationBudget:
    """
    Bound customer-visible impact from application adaptation.

    Attributes:
        maximumDurationSeconds (float): Longest permitted active adaptation.
        maximumUnavailableSeconds (float): Unavailable time permitted in one sample.
        maximumFailedAttempts (int): Failed attempts permitted in the current generation.
    """

    maximumDurationSeconds: float = 60
    maximumUnavailableSeconds: float = 5
    maximumFailedAttempts: int = 2

    def __attrs_post_init__(self) -> None:
        """
        Require finite bounded transition budgets.

        Returns:
            None: Valid budgets retain their declared values.
        """
        if any(
            isinstance(value, bool) or not math.isfinite(value) or value < 0
            for value in (self.maximumDurationSeconds, self.maximumUnavailableSeconds)
        ):
            raise ValueError("adaptation duration budgets must be finite nonnegative numbers")
        if type(self.maximumFailedAttempts) is not int or not 0 <= self.maximumFailedAttempts <= 1000:
            raise ValueError("maximumFailedAttempts must be an integer from zero through 1000")


@frozen
class ServiceLevelPolicy:
    """
    Declare the minimum externally observable contract for a Daemon.

    Attributes:
        requiredCapabilities (tuple[str, ...]): Capabilities that must remain available.
        availability (float): Required successful-request ratio from zero through one.
        latencyP99Seconds (float | None): Optional inclusive latency objective.
        minimumThroughputPerSecond (float | None): Optional inclusive completed-work objective.
        windowSeconds (int): Fixed accounting-window duration.
        sampleMaxAgeSeconds (int): Maximum accepted observation age.
        adaptation (AdaptationBudget): Transition-specific budgets.
    """

    requiredCapabilities: tuple[str, ...] = field(default=(), metadata={"schema": {"maxItems": 64, "x-kubernetes-list-type": "set"}})
    availability: float = 0.999
    latencyP99Seconds: float | None = None
    minimumThroughputPerSecond: float | None = None
    windowSeconds: int = 2_592_000
    sampleMaxAgeSeconds: int = 60
    adaptation: AdaptationBudget = field(factory=AdaptationBudget)

    def __attrs_post_init__(self) -> None:
        """
        Reject ambiguous objectives and unbounded windows.

        Returns:
            None: Valid policies retain their normalized contract.
        """
        if len(set(self.requiredCapabilities)) != len(self.requiredCapabilities) or any(
            not value or len(value) > 64 for value in self.requiredCapabilities
        ):
            raise ValueError("required capabilities must be unique nonempty names of at most 64 characters")
        if isinstance(self.availability, bool) or not math.isfinite(self.availability) or not 0 <= self.availability <= 1:
            raise ValueError("availability must be a finite ratio from zero through one")
        for value in (self.latencyP99Seconds, self.minimumThroughputPerSecond):
            if value is not None and (isinstance(value, bool) or not math.isfinite(value) or value < 0):
                raise ValueError("latency and throughput objectives must be finite nonnegative numbers")
        if type(self.windowSeconds) is not int or not 60 <= self.windowSeconds <= 31_536_000:
            raise ValueError("windowSeconds must be from 60 through 31536000")
        if type(self.sampleMaxAgeSeconds) is not int or not 1 <= self.sampleMaxAgeSeconds <= 3600:
            raise ValueError("sampleMaxAgeSeconds must be from 1 through 3600")


@frozen
class ServiceLevelReport:
    """
    Report one non-overlapping service observation window.

    Attributes:
        graph (str): Containing graph name used for authorization.
        graphUid (str): Exact containing graph incarnation.
        target (str): Daemon definition name.
        targetUid (str): Exact Daemon incarnation.
        targetGeneration (int): Daemon generation represented by this sample.
        node (str): Logical graph node whose service instance was measured.
        observedAt (str): Timezone-aware end of the sample window.
        durationSeconds (float): Width of the non-overlapping sample window.
        serving (bool): Whether the minimum service endpoint was usable.
        capabilities (tuple[str, ...]): Capabilities served throughout the sample.
        eligibleRequests (int): Requests eligible for SLA accounting.
        successfulRequests (int): Eligible requests completed successfully.
        requestsWithinLatencyObjective (int): Eligible requests meeting the declared latency objective.
        latencyP99Seconds (float | None): Observed p99 latency.
        completedPerSecond (float | None): Observed completed work rate.
        unavailableSeconds (float): Customer-visible unavailable time in this sample.
        graphKind (Literal['Graph', 'PolyGraph', 'ReplicaGroup']): Containing boundary kind.
    """

    graph: str
    graphUid: str
    target: str
    targetUid: str
    targetGeneration: int
    node: str
    observedAt: str
    durationSeconds: float
    serving: bool
    capabilities: tuple[str, ...] = field(default=(), metadata={"schema": {"maxItems": 64, "x-kubernetes-list-type": "set"}})
    eligibleRequests: int = 0
    successfulRequests: int = 0
    requestsWithinLatencyObjective: int = 0
    latencyP99Seconds: float | None = None
    completedPerSecond: float | None = None
    unavailableSeconds: float = 0
    graphKind: Literal["Graph", "PolyGraph", "ReplicaGroup"] = "Graph"

    def __attrs_post_init__(self) -> None:
        """
        Reject unfenced, overlapping or mathematically invalid observations.

        Returns:
            None: Valid reports remain immutable.
        """
        if not all((self.graph, self.graphUid, self.target, self.targetUid, self.node)) or self.graphKind not in {
            "Graph",
            "PolyGraph",
            "ReplicaGroup",
        }:
            raise ValueError("service-level reports require graph and Daemon identities")
        if any(len(value) > 253 for value in (self.graph, self.graphUid, self.target, self.targetUid)) or len(self.node) > 64:
            raise ValueError("service-level report identities must be at most 253 characters")
        if type(self.targetGeneration) is not int or self.targetGeneration < 1:
            raise ValueError("service-level reports require a positive target generation")
        if datetime.fromisoformat(self.observedAt.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("service-level reports require a timezone")
        if len(set(self.capabilities)) != len(self.capabilities) or any(not value or len(value) > 64 for value in self.capabilities):
            raise ValueError("capabilities must be unique bounded names")
        counts = (self.eligibleRequests, self.successfulRequests, self.requestsWithinLatencyObjective)
        if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in counts):
            raise ValueError("service-level request counters must be nonnegative integers")
        if self.successfulRequests > self.eligibleRequests or self.requestsWithinLatencyObjective > self.eligibleRequests:
            raise ValueError("successful and within-objective requests cannot exceed eligible requests")
        values = (self.durationSeconds, self.unavailableSeconds)
        if any(isinstance(value, bool) or not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("service-level durations must be finite nonnegative numbers")
        if type(self.serving) is not bool:
            raise ValueError("serving must be a boolean")
        if not 0 < self.durationSeconds <= 3600 or self.unavailableSeconds > self.durationSeconds:
            raise ValueError("sample duration must be positive and contain unavailableSeconds")
        for value in (self.latencyP99Seconds, self.completedPerSecond):
            if value is not None and (isinstance(value, bool) or not math.isfinite(value) or value < 0):
                raise ValueError("service-level measurements must be finite nonnegative numbers")
