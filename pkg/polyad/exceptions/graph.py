"""
Define graph computation failures with their diagnostic certificates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The certificate imports this exception; annotations must not load it back.
    from polyad.graph.cheeger import CheegerResult

__all__ = ("CheegerIncomplete",)


class CheegerIncomplete(ValueError):
    """
    Refuse to return a partial cut search as an exact Cheeger constant.
    """

    def __init__(self, result: CheegerResult) -> None:
        """
        Preserve the search certificate for callers that expose decision diagnostics.

        Args:
            result (CheegerResult): Incomplete search result.
        """
        self.result = result
        super().__init__(f"Cheeger computation incomplete: {result.reason} after {result.evaluatedCuts} cuts")
