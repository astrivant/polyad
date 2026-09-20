"""
Define SDK API response failures without importing transport implementations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

__all__ = ("APIError",)


class APIError(RuntimeError):
    """
    Expose HTTP status and JSON error details without embedding credentials.
    """

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        """
        Retain the server response for explicit retry decisions.

        Args:
            status (int): HTTP status code.
            body (dict[str, Any]): Parsed response body.
        """
        self.status, self.body = status, body
        super().__init__(f"Polyad API returned HTTP {status}")
