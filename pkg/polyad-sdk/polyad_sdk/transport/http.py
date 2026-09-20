"""
Share HTTP error handling and redirect restrictions across SDK transports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.request import HTTPRedirectHandler

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


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None
