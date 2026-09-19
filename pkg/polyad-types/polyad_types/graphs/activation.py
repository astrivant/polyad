"""
Define bounded pulse admission independently of workload completion.
"""

from __future__ import annotations

from typing import Literal

from attrs import field, frozen


@frozen
class ActivationPolicy:
    """
    Control how repeated requests start a downstream workload or graph.

    Attributes:
        mode (Literal['Queue', 'Reject', 'Coalesce', 'Parallel']): Busy-target admission policy.
        maxConcurrent (int): Maximum selected or running executions for parallel mode.
        maxPending (int): Maximum retained pending requests admitted to the queue.
        replicasPerActivation (int): Deployment or StatefulSet replicas in each daemon activation.
        maxReplicas (int): Upper bound on the total daemon replicas reserved by concurrent pulses.
        minIntervalSeconds (int): Minimum time between execution admissions.
        maxIntervalSeconds (int | None): Maximum expected time between admissions.
        onDeadline (Literal['Report', 'Activate']): Report overdue admission or submit a timer pulse.
    """

    mode: Literal["Queue", "Reject", "Coalesce", "Parallel"] = "Queue"
    maxConcurrent: int = field(default=1, metadata={"schema": {"minimum": 1, "maximum": 64}})
    maxPending: int = field(default=64, metadata={"schema": {"minimum": 1, "maximum": 1024}})
    replicasPerActivation: int = field(default=1, metadata={"schema": {"minimum": 1, "maximum": 1024}})
    maxReplicas: int = field(default=1024, metadata={"schema": {"minimum": 1, "maximum": 1024}})
    minIntervalSeconds: int = field(default=0, metadata={"schema": {"minimum": 0, "maximum": 86400}})
    maxIntervalSeconds: int | None = field(default=None, metadata={"schema": {"minimum": 1, "maximum": 604800}})
    onDeadline: Literal["Report", "Activate"] = "Report"

    def __attrs_post_init__(self) -> None:
        """
        Reject unbounded parallelism and contradictory activation intervals.

        Returns:
            None: Invalid policies raise before compilation.
        """
        if self.mode not in {"Queue", "Reject", "Coalesce", "Parallel"} or self.onDeadline not in {"Report", "Activate"}:
            raise ValueError("unknown activation policy")
        if not 1 <= self.maxConcurrent <= 64 or not 1 <= self.maxPending <= 1024 or not 1 <= self.replicasPerActivation <= 1024:
            raise ValueError("activation bounds exceed supported limits")
        if not 1 <= self.maxReplicas <= 1024 or self.maxConcurrent * self.replicasPerActivation > self.maxReplicas:
            raise ValueError("concurrent activation replicas exceed maxReplicas")
        if self.mode != "Parallel" and self.maxConcurrent != 1:
            raise ValueError("maxConcurrent above one requires Parallel mode")
        if not 0 <= self.minIntervalSeconds <= 86400:
            raise ValueError("minIntervalSeconds must be between 0 and 86400")
        if self.maxIntervalSeconds is not None and not max(1, self.minIntervalSeconds) <= self.maxIntervalSeconds <= 604800:
            raise ValueError("maxIntervalSeconds must be at least minIntervalSeconds and at most one week")
        if self.onDeadline == "Activate" and self.maxIntervalSeconds is None:
            raise ValueError("automatic pulses require maxIntervalSeconds")
