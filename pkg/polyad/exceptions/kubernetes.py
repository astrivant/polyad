"""
Define Kubernetes-compatible write admission failures.
"""

from __future__ import annotations

from kubernetes.client.exceptions import ApiException

__all__ = ("WriteConflict",)


class WriteConflict(ApiException):  # type: ignore[misc]  # The Kubernetes client exception has no type stubs.
    """
    Return queued decisions to reconciliation without dispatching their effects.
    """

    def __init__(self, reason: str, *, status: int = 409) -> None:
        """
        Carry a stable conflict reason without retaining request payloads.

        Args:
            reason (str): Machine-readable explanation for refusing dispatch.
            status (int): Retryable conflict or backpressure HTTP status.
        """
        self.conflict_reason = reason
        super().__init__(status=status, reason="Queued write refused; refresh observations and reconcile current desired state")
