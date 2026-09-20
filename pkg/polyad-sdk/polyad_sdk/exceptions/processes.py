"""
Define private subprocess reconciliation control-flow signals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polyad_sdk.processes.models import PlanResult

__all__ = ()


class _Aborted(Exception):
    """
    Unwind an uncommitted process plan without exposing internal control flow as an API.
    """

    def __init__(self, result: PlanResult) -> None:
        """
        Retain the outcome for the supervisor's rollback and reporting paths.

        Args:
            result (PlanResult): Failed, blocked or superseded proposal outcome.
        """
        self.result = result
