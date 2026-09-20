"""
Describe bounded queue dynamics and measurable effects of service relationships.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum

__all__ = (
    "Interaction",
    "QueueModel",
    "Relationship",
    "finite",
)


def finite(value: float, *, minimum: float = 0) -> None:
    """
    Reject nonnumeric, nonfinite and out-of-range configuration values.

    Args:
        value (float): Number to validate; booleans are rejected.
        minimum (float): Inclusive lower bound.

    Returns:
        None: Invalid values raise ValueError.
    """
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError("expected a finite number within the configured range")


class Relationship(StrEnum):
    """
    Name the signs of measured capacity effects on the first and second service.

    These are modeling categories. They do not grant permission or identify
    malicious services. Effects must be calibrated for the workload being studied.

    Attributes:
        NEUTRALISM: Neither service's capacity changes.
        MUTUALISM: Both services gain capacity.
        COMMENSALISM: The first gains capacity and the second stays unchanged.
        PARASITISM: The first gains capacity at a cost to the second.
        COMPETITION: Both lose capacity through contention.
        AMENSALISM: The first loses capacity and the second stays unchanged.
    """

    NEUTRALISM = "neutralism"
    MUTUALISM = "mutualism"
    COMMENSALISM = "commensalism"
    PARASITISM = "parasitism"
    COMPETITION = "competition"
    AMENSALISM = "amensalism"


@dataclass(frozen=True)
class Interaction:
    """
    Describe capacity gained or lost when two services interact.

    Positive effects add useful processing capacity in the model's work unit
    per second. A parasitic relationship benefits the first service and costs
    the second capacity, for example a tenant drawing on a shared worker pool.

    Attributes:
        relationship (Relationship): Required sign pattern for the effects.
        effects (tuple[float, float]): First-service and second-service capacity deltas.
    """

    relationship: Relationship = Relationship.NEUTRALISM
    effects: tuple[float, float] = (0, 0)

    def __post_init__(self) -> None:
        """
        Require explicit finite effects matching the selected biological analogy.

        Returns:
            None: Invalid effects fail before any computation.
        """
        if not isinstance(self.relationship, Relationship) or not isinstance(self.effects, tuple) or len(self.effects) != 2:
            raise ValueError("an interaction needs a Relationship and two immutable effects")
        for effect in self.effects:
            finite(effect, minimum=-1e9)

        # Relationship names constrain direction, while the supplied magnitudes determine capacity cost.
        signs = tuple((value > 0) - (value < 0) for value in self.effects)
        expected = {
            Relationship.NEUTRALISM: (0, 0),
            Relationship.MUTUALISM: (1, 1),
            Relationship.COMMENSALISM: (1, 0),
            Relationship.PARASITISM: (1, -1),
            Relationship.COMPETITION: (-1, -1),
            Relationship.AMENSALISM: (-1, 0),
        }
        if signs != expected[self.relationship]:
            raise ValueError("capacity effects do not match the relationship's sign pattern")


@dataclass(frozen=True)
class QueueModel:
    """
    Model a producer distributing work among one to three independent consumers.

    Rates use one common work unit. Each consumer is a work-conserving fluid
    queue: fractional work is permitted and an empty queue cannot become negative.
    Shared resources must already be allocated in the capacity bounds. This model
    does not infer those allocations, per-job latency or delivery guarantees.

    Attributes:
        names (tuple[str, ...]): Ordered consumer identities, also the queue state axes.
        capacities (tuple[float, ...]): Guaranteed service rates before interaction effects.
        limits (tuple[float, ...]): Maximum queued work for each consumer.
        targets (tuple[float, ...]): Required queue ceilings at the end of the horizon.
        arrival_bounds (tuple[float, float]): Inclusive lower and upper offered rates.
        shares (tuple[float, ...]): Fixed, approved fractions of arrivals sent to each consumer.
        interaction (Interaction): Effects on two consumers; neutral for other dimensions.
        warmup_max (float): Optional final state axis: seconds until the last consumer is ready.
        unit (str): Work unit shared by queues, limits and rates, such as records.
    """

    names: tuple[str, ...]
    capacities: tuple[float, ...]
    limits: tuple[float, ...]
    targets: tuple[float, ...]
    arrival_bounds: tuple[float, float]
    shares: tuple[float, ...]
    interaction: Interaction = Interaction()
    warmup_max: float = 0
    unit: str = "records"

    def __post_init__(self) -> None:
        """
        Validate dimensions, finite rates and conservation of incoming work.

        Returns:
            None: Invalid or physically inconsistent models raise ValueError.
        """
        if not isinstance(self.names, tuple) or not 1 <= len(self.names) <= 3:
            raise ValueError("use one to three consumer queues")
        if any(not isinstance(name, str) or not name.strip() for name in self.names) or len(set(self.names)) != len(self.names):
            raise ValueError("consumer identities must be nonempty and unique")
        for values in (self.capacities, self.limits, self.targets, self.shares):
            if not isinstance(values, tuple) or len(values) != len(self.names):
                raise ValueError("every queue needs capacity, limit, target and routing share")
            for value in values:
                finite(value)
        if any(limit <= 0 or target > limit for limit, target in zip(self.limits, self.targets, strict=True)):
            raise ValueError("queue limits must be positive and contain their targets")
        if not math.isclose(sum(self.shares), 1, abs_tol=1e-12, rel_tol=0):
            raise ValueError("routing shares must sum to one")
        if not isinstance(self.arrival_bounds, tuple) or len(self.arrival_bounds) != 2:
            raise ValueError("provide lower and upper arrival bounds")
        for rate in self.arrival_bounds:
            finite(rate)
        if self.arrival_bounds[0] > self.arrival_bounds[1]:
            raise ValueError("arrival bounds are reversed")
        if not isinstance(self.interaction, Interaction):
            raise ValueError("provide a validated Interaction")
        if len(self.names) != 2 and self.interaction.relationship != Relationship.NEUTRALISM:
            raise ValueError("non-neutral interactions describe exactly two consumers")

        # Signed interaction costs cannot create physically negative service capacity.
        if any(capacity < 0 for capacity in self.effective_capacities):
            raise ValueError("interaction costs exceed the available capacity")
        finite(self.warmup_max)
        if self.warmup_max and len(self.names) != 2:
            raise ValueError("the warmup model requires two consumer queues")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ValueError("specify the work unit")

    @property
    def effective_capacities(self) -> tuple[float, ...]:
        """
        Apply calibrated relationship effects to each processing rate.

        Returns:
            tuple[float, ...]: Guaranteed service rates in work units per second.
        """
        effects = self.interaction.effects if len(self.names) == 2 else (0,) * len(self.names)
        return tuple(rate + effect for rate, effect in zip(self.capacities, effects, strict=True))

    @property
    def domain(self) -> tuple[float, ...]:
        """
        Describe the upper endpoint of each state axis; lower endpoints are zero.

        Returns:
            tuple[float, ...]: Queue limits followed by an optional warmup clock.
        """
        return (*self.limits, self.warmup_max) if self.warmup_max else self.limits

    @property
    def fingerprint(self) -> str:
        """
        Bind observations and artifacts to the exact dynamics and routing choice.

        Returns:
            str: SHA-256 of canonical model configuration.
        """
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def bounds(self, state: tuple[float, ...], horizon: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
        """
        Compute worst-case queue peaks and terminal backlogs for a fixed routing choice.

        The maximum arrival rate and guaranteed processing rates bound every
        permitted arrival trace. A warming consumer serves no work until its
        clock reaches zero. Queue trajectories are linear between that point
        and the horizon, with reflection at zero, so their extrema occur at
        those endpoints. Targets describe the horizon endpoint, not a later hold.

        Args:
            state (tuple[float, ...]): Nonnegative backlogs and optional remaining warmup seconds.
            horizon (float): Duration to evaluate in seconds.

        Returns:
            tuple[tuple[float, ...], tuple[float, ...]]: Queue peaks and final backlogs under the upper arrival bound.
        """
        finite(horizon)
        if len(state) != len(self.domain):
            raise ValueError("state axes do not match the model")
        for value in state:
            finite(value)
        if self.warmup_max and state[-1] > self.warmup_max:
            raise ValueError("warmup state lies outside the model")
        peaks, terminal = [], []
        for index, capacity in enumerate(self.effective_capacities):
            arrival = self.arrival_bounds[1] * self.shares[index]
            delay = min(horizon, state[-1]) if self.warmup_max and index == len(self.names) - 1 else 0
            before_ready = state[index] + arrival * delay
            final = max(0, before_ready + (arrival - capacity) * (horizon - delay))
            peaks.append(max(state[index], before_ready, final))
            terminal.append(final)
        return tuple(peaks), tuple(terminal)
