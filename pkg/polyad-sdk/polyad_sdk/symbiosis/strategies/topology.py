"""
Assess topology freshness, usable neighbors and temporary connection permission.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk.symbiosis.strategies.base import ConstraintAssessment, ConstraintStrategy

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

    from polyad_sdk.symbiosis.models import Environment

__all__ = (
    "ConnectionPermissionStrategy",
    "FreshnessStrategy",
    "PeerAvailabilityStrategy",
)


class FreshnessStrategy(ConstraintStrategy):
    """
    Check whether the SDK has recent, usable information about this service's connections.

    Use this check before choosing where to send new work. It reports a problem
    if the graph information is too old or the service is suspended, being
    replaced or shutting down. Your application can pause new sends and finish
    work it has already accepted, then resume when the information is usable again.
    """

    def evaluate(self, current: Environment) -> ConstraintAssessment:
        """
        Check the SDK's topology availability and lifecycle assessment.

        Args:
            current (Environment): Current topology freshness and admission state.

        Returns:
            ConstraintAssessment: Satisfied for an available view, otherwise unknown or blocked.
        """
        if current.available:
            return ConstraintAssessment(self.name, "satisfied", "Topology admits considering new assignments")
        return ConstraintAssessment(
            self.name, "unknown" if current.topology is None else "blocked", current.reason or "Topology unavailable"
        )


class PeerAvailabilityStrategy(ConstraintStrategy):
    """
    Check that enough downstream services are ready to receive work.

    A peer is another node that this service can send work to in its graph.
    The SDK supplies candidates with observed workloads that have replicas and
    are not being removed. Your usable callback
    checks whether each candidate is healthy, speaks the expected protocol and
    can accept work. For example, a producer can pause sending when its last
    available consumer goes offline during a rollout.

    The minimum counts graph nodes: several Pods behind one node count as one
    peer. Have the callback read health information your application already
    maintains so each check completes quickly.
    """

    def __init__(
        self,
        name: str,
        publish: Callable[[ConstraintAssessment], None],
        *,
        usable: Callable[[Mapping[str, Any]], bool],
        minimum: int = 1,
    ) -> None:
        """
        Configure how many independently usable logical destinations an action requires.

        Args:
            name (str): Constraint identity.
            publish (Callable[[ConstraintAssessment], None]): Application assessment sink.
            usable (Callable[[Mapping[str, Any]], bool]): Application readiness, compatibility and permission check.
            minimum (int): Minimum usable logical peers; must be positive.

        Raises:
            ValueError: The minimum is not a positive integer.
            TypeError: The peer predicate is not callable.
        """
        super().__init__(name, publish)
        if type(minimum) is not int or minimum < 1:
            raise ValueError("minimum must be a positive integer")
        if not callable(usable):
            raise TypeError("usable must be callable")
        self._usable, self._minimum = usable, minimum

    def evaluate(self, current: Environment) -> ConstraintAssessment:
        """
        Count available candidates that pass application admission checks.

        Args:
            current (Environment): Fresh authorized neighborhood.

        Returns:
            ConstraintAssessment: Whether enough usable destinations remain for the protected action.
        """
        if not current.available:
            return ConstraintAssessment(self.name, "unknown", "Cannot assess peers from unavailable topology")

        # Count logical peers that pass the application's usability predicate,
        # not every replica address as if it were an independent service.
        count = sum(bool(self._usable(peer)) for peer in current.candidates)
        return ConstraintAssessment(
            self.name, "satisfied" if count >= self._minimum else "blocked", f"Usable logical peers: {count}; required: {self._minimum}"
        )


class ConnectionPermissionStrategy(ConstraintStrategy):
    """
    Check whether Polyad currently permits a requested temporary connection.

    For example, a producer discovers a new consumer and requests permission to
    send it work. Polyad tracks that request in a connection receipt: a record
    with a unique ID, status and expiry time. Supply a function that returns the
    chosen receipt's ID through the receipt argument.

    This check passes when the SDK observes that request as Active, meaning the
    operator has admitted the connection. It blocks while approval is pending,
    or after permission expires or is revoked. Unavailable graph information
    produces an unknown result. The SDK removes expired and revoked records
    from service.view, its current view of the service's surroundings.

    Your application checks this result before sending work, alongside its usual
    health and protocol checks for the consumer. Use AdaptiveService.connect()
    and respond() to request or approve connections, and your application's
    HTTP, gRPC or other transport code to communicate with the consumer.
    """

    def __init__(self, name: str, publish: Callable[[ConstraintAssessment], None], *, receipt: Callable[[], str | None]) -> None:
        """
        Choose which temporary connection request to check.

        Args:
            name (str): Name for this check, such as consumer-permission.
            publish (Callable[[ConstraintAssessment], None]): Callback that receives each result for application state or logging.
            receipt (Callable[[], str | None]): Function returning the chosen connection receipt's unique ID, or None before requesting it.

        Raises:
            TypeError: The receipt selector is not callable.
        """
        super().__init__(name, publish)
        if not callable(receipt):
            raise TypeError("receipt must be callable")
        self._receipt = receipt

    def evaluate(self, current: Environment) -> ConstraintAssessment:
        """
        Check whether the selected connection is active and its permission is still valid.

        Args:
            current (Environment): Current service.view, including connection records whose permissions have not expired.

        Returns:
            ConstraintAssessment: Satisfied when permission is active, blocked when absent or pending, or unknown without graph information.
        """
        if not current.available:
            return ConstraintAssessment(self.name, "unknown", "Cannot assess permission from unavailable topology")
        uid = self._receipt()
        receipt = current.connections.get(uid) if uid else None
        if receipt is None:
            return ConstraintAssessment(self.name, "blocked", "Selected connection has no observed unexpired receipt")

        # Discovering a receipt is not permission to use it. Only the observed
        # Active phase confirms that the connection has completed admission.
        phase = receipt.get("status", {}).get("phase")
        return ConstraintAssessment(self.name, "satisfied" if phase == "Active" else "blocked", f"Connection phase: {phase or 'unknown'}")
