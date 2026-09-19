"""
Use a current queue envelope as one independent SDK adaptation guard.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from polyad_sdk.symbiosis.reachability.models import finite
from polyad_sdk.symbiosis.strategies.base import ConstraintAssessment, ConstraintStrategy

if TYPE_CHECKING:
    from collections.abc import Callable

    from polyad_sdk.symbiosis.models import Environment
    from polyad_sdk.symbiosis.reachability.envelope import Envelope


@dataclass(frozen=True)
class Observation:
    """
    Bind an application measurement to its modeled queues and planned action.

    Attributes:
        state (tuple[float, ...]): Backlogs and optional remaining warmup seconds.
        uncertainty (tuple[float, ...]): Nonnegative absolute error bounds for those axes.
        observed_at (float): Unix timestamp of the measurement, not its receipt.
        revision (str): Current application contract and topology revision.
        fingerprint (str): Model fingerprint including the action actually being considered.
    """

    state: tuple[float, ...]
    uncertainty: tuple[float, ...]
    observed_at: float
    revision: str
    fingerprint: str


class ReachabilityStrategy(ConstraintStrategy):
    """
    Check a proposed routing choice against its current, bounded queue envelope.

    Evaluation uses a few scalar operations and never imports JAX, reads files or
    launches workers. Load trusted artifacts outside the observation callback.
    A satisfied result only covers modeled queue safety and the terminal target;
    permission, readiness, delivery semantics and shared reservations remain
    independent checks. A blocked result rejects this routing choice, not every
    possible adaptation.
    """

    def __init__(
        self,
        name: str,
        publish: Callable[[ConstraintAssessment], None],
        *,
        artifact: Callable[[], Envelope | None],
        observe: Callable[[Environment], Observation | None],
        max_age_seconds: float = 1,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        Bind a prepared artifact and cheap application measurement callbacks.

        Args:
            name (str): Independent constraint identity.
            publish (Callable[[ConstraintAssessment], None]): Callback receiving assessments.
            artifact (Callable[[], Envelope | None]): Current prepared envelope, without I/O.
            observe (Callable[[Environment], Observation | None]): Fresh measurements and planned-action identity.
            max_age_seconds (float): Maximum acceptable measurement age in seconds.
            clock (Callable[[], float]): Unix time source, injectable for deterministic studies.
        """
        super().__init__(name, publish)
        finite(max_age_seconds, minimum=0.001)
        if not all(callable(value) for value in (artifact, observe, clock)):
            raise TypeError("artifact, observation and clock callbacks must be callable")
        self._artifact, self._observe, self._clock = artifact, observe, clock
        self._max_age = max_age_seconds

    def evaluate(self, current: Environment) -> ConstraintAssessment:
        """
        Assess a matching observation, conservatively accounting for measurement age.

        Args:
            current (Environment): Authorized SDK context, refreshed before action.

        Returns:
            ConstraintAssessment: Satisfied or blocked for this model; unknown for inapplicable evidence.
        """
        if not current.available:
            return ConstraintAssessment(self.name, "unknown", "Current graph context is unavailable")
        artifact, observation, now = self._artifact(), self._observe(current), self._clock()
        if artifact is None or observation is None:
            return ConstraintAssessment(self.name, "unknown", "Queue envelope or observation is missing")
        try:
            finite(now)
            finite(observation.observed_at)
            age = now - observation.observed_at
            if not artifact.created_at <= now < artifact.expires_at or not 0 <= age <= self._max_age:
                raise ValueError("Envelope expired or measurement is stale or future-dated")
            if observation.revision != artifact.revision or observation.fingerprint != artifact.model.fingerprint:
                raise ValueError("Topology, capacity contract or proposed routing differs from the envelope")
            if len(observation.state) != len(artifact.model.domain) or len(observation.uncertainty) != len(observation.state):
                raise ValueError("Observation axes do not match the modeled state variables")
            for value in (*observation.state, *observation.uncertainty):
                finite(value)
            upper = tuple(value + error for value, error in zip(observation.state, observation.uncertainty, strict=True))
            # Before dispatch the proposed route may not yet be active. Any one
            # queue could have received all bounded arrivals since observation.
            upper = tuple(
                value + artifact.model.arrival_bounds[1] * age if index < len(artifact.model.names) else value
                for index, value in enumerate(upper)
            )
            allowed, margin = artifact.assess(upper)
            finite(abs(margin))
        except (ValueError, OverflowError) as error:
            return ConstraintAssessment(self.name, "unknown", str(error))
        return ConstraintAssessment(
            self.name,
            "satisfied" if allowed else "blocked",
            f"Fixed-routing queue bound {'passes' if allowed else 'fails'}; slack={margin:.6g} {artifact.model.unit}; "
            f"horizon={artifact.horizon:g}s; revision={artifact.revision}",
        )
