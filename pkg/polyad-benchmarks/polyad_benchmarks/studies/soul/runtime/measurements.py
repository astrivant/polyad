"""
Compute reproducible service-level and local resource-loop study measurements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


def percentile(values: list[float], fraction: float) -> float | None:
    """
    Select an inclusive nearest-rank percentile from measured values.

    Args:
        values (list[float]): Finite nonnegative observations.
        fraction (float): Quantile from zero through one.

    Returns:
        float | None: Selected observation, or None when no value exists.
    """
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def service_level(
    *,
    serving: bool,
    eligible: int,
    successful: int,
    latencies: list[float],
    completed_per_second: float,
    adapting_seconds: float | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """
    Evaluate the study's bounded service contract from measured process outcomes.

    Args:
        serving (bool): Whether guards currently permit new application work.
        eligible (int): Terminal requests included in availability accounting.
        successful (int): Eligible requests completed with a verified result.
        latencies (list[float]): Successful end-to-end service durations.
        completed_per_second (float): Completion rate since the prior sample.
        adapting_seconds (float | None): Age of the active worker replacement.
        policy (dict[str, Any]): Validated study objectives.

    Returns:
        dict[str, Any]: State, violations and exact values used by the plots.
    """
    availability = successful / eligible if eligible else float(serving)
    latency = percentile(latencies, 0.99)
    violations = []
    if not serving:
        violations.append("serving")
    if availability < policy["availability"]:
        violations.append("availability")
    if latency is not None and latency > policy["latencyP99Seconds"]:
        violations.append("latencyP99Seconds")
    if adapting_seconds is not None and adapting_seconds > policy["maximumAdaptationSeconds"]:
        violations.append("adaptation.maximumDurationSeconds")
    state = "Unavailable" if not serving else "Degraded" if violations else "Compliant"
    allowance = max(1e-12, 1 - policy["availability"])
    return {
        "state": state,
        "contractSatisfied": not violations,
        "availability": availability,
        "latencyP99Seconds": latency,
        "completedPerSecond": completed_per_second,
        "errorBudgetRemaining": max(0.0, 1 - (1 - availability) / allowance),
        "violations": violations,
    }


@dataclass
class ResourceLoop:
    """
    Model a bounded VPA allocation loop without claiming to mutate a local cgroup.

    Attributes:
        minimum (int): Smallest modeled memory allocation in bytes.
        maximum (int): Largest modeled memory allocation in bytes.
        step (int): Bytes added or removed per control action.
        target (float): Desired modeled utilization.
        interval (float): Minimum seconds between allocation changes.
        base (int): Modeled service-process memory in bytes.
        worker (int): Modeled memory per live child process in bytes.
        queued_job (int): Modeled memory retained per outstanding job in bytes.
        assigned (int): Current modeled allocation in bytes.
        last_change (float): Monotonic time of the prior allocation action.
    """

    minimum: int
    maximum: int
    step: int
    target: float
    interval: float
    base: int
    worker: int
    queued_job: int
    assigned: int
    last_change: float = 0

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ResourceLoop:
        """
        Construct the loop from the validated process-study recipe.

        Args:
            config (dict[str, Any]): Resource-loop settings using explicit byte units.

        Returns:
            ResourceLoop: Independent service-local controller state.
        """
        return cls(
            config["minMemoryBytes"],
            config["maxMemoryBytes"],
            config["stepMemoryBytes"],
            config["targetUtilization"],
            config["intervalSeconds"],
            config["baseMemoryBytes"],
            config["workerMemoryBytes"],
            config["queuedJobMemoryBytes"],
            config["initialMemoryBytes"],
        )

    def observe(self, now: float, *, workers: int, backlog: int) -> tuple[int, int, bool]:
        """
        Reconcile one modeled allocation step from application-visible demand.

        Args:
            now (float): Current monotonic timestamp.
            workers (int): Live child-process count.
            backlog (int): Accepted jobs not yet returned.

        Returns:
            tuple[int, int, bool]: Assigned bytes, modeled use and whether allocation changed.
        """
        used = self.base + workers * self.worker + backlog * self.queued_job
        previous = self.assigned
        if now - self.last_change >= self.interval:
            utilization = used / self.assigned
            if utilization > self.target + 0.1:
                self.assigned = min(self.maximum, self.assigned + self.step)
            elif utilization < self.target - 0.2:
                self.assigned = max(self.minimum, self.assigned - self.step)
            if self.assigned != previous:
                self.last_change = now
        return self.assigned, used, self.assigned != previous
