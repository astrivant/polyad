"""
Retain legacy HTTP exception imports; definitions live in polyad.exceptions.api.
"""

from __future__ import annotations

from polyad.exceptions.api import Conflict as Conflict
from polyad.exceptions.api import Forbidden as Forbidden

# Preserve existing import paths while keeping each exception defined centrally.
from polyad.exceptions.api import RequestError as RequestError
from polyad.exceptions.api import Unauthorized as Unauthorized
from polyad.exceptions.api import Unavailable as Unavailable

__all__ = (
    "Conflict",
    "Forbidden",
    "RequestError",
    "Unauthorized",
    "Unavailable",
)
