"""
Define adaptation components and independently assessed application constraints.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    from polyad_sdk.symbiosis.models import Change, Environment


class AdaptationStrategy(ABC):
    """
    Define one part of how a service responds when its surroundings change.

    For example, a strategy can update the destinations a producer uses or
    request fewer workers when memory is scarce. AdaptiveService calls strategies
    in the order supplied, passing the observed changes and current information
    about the service's graph. Your application carries out the response.

    Strategies run in the service's observation loop. Keep each call short and
    safe to retry after a partial failure. The SDK tracks completed calls and
    retries unfinished ones; the application remains responsible for finishing
    or handing off work it has accepted.

    Common implementations bridge delayed Pod/node readiness with backpressure,
    reduce concurrency under memory pressure, or propose approved worker profiles.
    Kubernetes diagnostics prompt fresh status checks; this interface receives
    SDK observations, not a raw Kubernetes Event watch. Application timers handle
    local pressure between SDK changes. See docs/workloads/kubernetes-adaptation.md.
    """

    @abstractmethod
    def adapt(self, change: Change, current: Environment) -> None:
        """
        Handle a baseline or delta using context refreshed immediately before this call.

        Args:
            change (Change): Immutable delivery history and an isolated triggering event.
            current (Environment): Live freshness and grant expiry, which may differ
                from change.after when retrying an older delivery.

        Returns:
            None: This component completed; an exception keeps it pending for retry.
        """
        ...


@dataclass(frozen=True)
class ConstraintAssessment:
    """
    Report one independently named application constraint and its current evidence.

    Attributes:
        name (str): Application-chosen identity, such as roll-memory or sink-permission.
        state (Literal['satisfied', 'blocked', 'unknown']): Whether this constraint permits the protected action.
        reason (str): Human-readable explanation suitable for application logs.
    """

    name: str
    state: Literal["satisfied", "blocked", "unknown"]
    reason: str

    @property
    def satisfied(self) -> bool:
        """
        Permit progress only when this individual constraint has positive evidence.

        Returns:
            bool: False for both a known blocker and missing evidence.
        """
        return self.state == "satisfied"


class ConstraintStrategy(AdaptationStrategy):
    """
    Check one condition that must hold before the application takes an action.

    This is also called a guard: for example, check permission before sending
    work, or available memory before starting a worker. evaluate() returns
    satisfied, blocked or unknown. The publish callback receives that result
    when the SDK delivers its first snapshot or a later change.

    Your application requires every relevant check to pass before acting.
    Evaluate again against service.view immediately before the action, since
    information and permissions can expire between events. Reserve shared
    capacity in your own scheduling code so two operations cannot both spend
    the same available memory. Callbacks should save results or notify that
    scheduling code, and be safe to retry.

    Implement this interface for local conditions such as a peer circuit opened
    after repeated timeouts, a full durable queue, or insufficient budget while
    a Kubernetes replacement is Pending. Keep each blocker independently named.
    Unknown transport health or missing usage measurements must remain unknown;
    an unrelated topology recovery must not clear them.

    Attributes:
        name (str): Stable application-chosen constraint identity.
    """

    name: str

    def __init__(self, name: str, publish: Callable[[ConstraintAssessment], None]) -> None:
        """
        Bind a constraint identity and the application's assessment sink.

        Args:
            name (str): Unique name within the application's constraint collection.
            publish (Callable[[ConstraintAssessment], None]): Bounded callback receiving each assessment.

        Raises:
            ValueError: The name is empty.
            TypeError: The assessment sink is not callable.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("constraint name must be nonempty")
        if not callable(publish):
            raise TypeError("publish must be callable")
        self.name, self._publish = name, publish

    @abstractmethod
    def evaluate(self, current: Environment) -> ConstraintAssessment:
        """
        Assess current evidence without changing admission, routing or process state.

        Args:
            current (Environment): Fresh authorized view evaluated at the point of use.

        Returns:
            ConstraintAssessment: One constraint's independent result, never a global permission.
        """
        ...

    def adapt(self, change: Change, current: Environment) -> None:
        """
        Notify the application of current difficulty or recovery on a delivered change.

        Args:
            change (Change): Baseline or delta that triggered evaluation.
            current (Environment): Refreshed context, including expiry during retries.

        Returns:
            None: The application received this constraint's assessment.
        """
        self._publish(self.evaluate(current))
