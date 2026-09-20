"""
Share HTTP error handling and redirect restrictions across SDK transports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.request import HTTPRedirectHandler

# Preserve existing import paths while keeping each exception defined centrally.
from polyad_sdk.exceptions.api import APIError as APIError

if TYPE_CHECKING:
    from typing import Any

__all__ = ("APIError",)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None
