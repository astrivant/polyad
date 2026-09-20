"""
Hold dependent application changes until an observed operator decision permits them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk.symbiosis.strategies.base import ConstraintAssessment, ConstraintStrategy

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from polyad_sdk.symbiosis.models import Environment


class DecisionGuardStrategy(ConstraintStrategy):
    """
    Check whether the operator has reached the stage your next action needs.

    Soul searching is Polyad's algorithm for adjusting graph connections and
    traffic routing to application demand. For example, your service may need
    it to apply a new layout before changing destinations. Set allowed to
    ("Applied",) for that case, then check the new destinations' health before
    sending work. Configure this strategy for actions that depend on such a
    decision; unrelated processing can continue under its existing checks.
    """

    def __init__(
        self,
        name: str,
        publish: Callable[[ConstraintAssessment], None],
        *,
        allowed: Sequence[str] = ("Satisfied", "BelowDemandThreshold", "Applied"),
    ) -> None:
        """
        Select observed phases that permit considering the dependent application action.

        Args:
            name (str): Constraint identity.
            publish (Callable[[ConstraintAssessment], None]): Application assessment sink.
            allowed (Sequence[str]): Explicitly accepted Soul searching phases.

        Raises:
            ValueError: Accepted phases are empty, invalid or supplied as one string.
        """
        super().__init__(name, publish)
        if isinstance(allowed, str) or not allowed or any(not isinstance(phase, str) or not phase.strip() for phase in allowed):
            raise ValueError("allowed must be a nonempty sequence of phase names")
        self._allowed = frozenset(allowed)

    def evaluate(self, current: Environment) -> ConstraintAssessment:
        """
        Inspect the fresh public decision without treating a proposal as an applied change.

        Args:
            current (Environment): Current graph observation and throughput decision.

        Returns:
            ConstraintAssessment: Whether the observed phase permits the dependent action.
        """

        # A remembered allowed phase cannot authorize new work when the current
        # environment is unavailable; stale evidence is explicitly unknown.
        decision = current.decision if current.available else None
        phase = decision.get("phase") if decision else None
        if not isinstance(phase, str) or not phase:
            return ConstraintAssessment(self.name, "unknown", "No fresh Soul searching decision")
        return ConstraintAssessment(self.name, "satisfied" if phase in self._allowed else "blocked", f"Soul searching phase: {phase}")
