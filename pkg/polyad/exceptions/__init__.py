"""
Expose categorized exceptions owned by polyad.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad.exceptions.api import Conflict as Conflict
from polyad.exceptions.api import Forbidden as Forbidden
from polyad.exceptions.api import RequestError as RequestError
from polyad.exceptions.api import Unauthorized as Unauthorized
from polyad.exceptions.api import Unavailable as Unavailable
from polyad.exceptions.auth import LaneFull as LaneFull
from polyad.exceptions.compiler import PreconditionFailed as PreconditionFailed
from polyad.exceptions.coordination import NotOwner as NotOwner
from polyad.exceptions.coordination import PulseDeferred as PulseDeferred
from polyad.exceptions.events import CursorExpired as CursorExpired
from polyad.exceptions.events import TopologyReplaced as TopologyReplaced
from polyad.exceptions.graph import CheegerIncomplete as CheegerIncomplete
from polyad.exceptions.policies import PolicyViolation as PolicyViolation
from polyad.exceptions.reconciliation import Pending as Pending

if TYPE_CHECKING:
    from polyad.exceptions.kubernetes import WriteConflict as WriteConflict

__all__ = (
    "CheegerIncomplete",
    "Conflict",
    "CursorExpired",
    "Forbidden",
    "LaneFull",
    "NotOwner",
    "Pending",
    "PreconditionFailed",
    "PulseDeferred",
    "RequestError",
    "PolicyViolation",
    "TopologyReplaced",
    "Unauthorized",
    "Unavailable",
    "WriteConflict",
)


def __getattr__(name: str) -> type[WriteConflict]:
    """
    Load the Kubernetes exception base only when its write-conflict type is requested.

    Args:
        name (str): Public exception requested through the package namespace.

    Returns:
        type[WriteConflict]: Canonical exception class, cached for later imports.

    Raises:
        AttributeError: The name is not a deferred public exception.
    """
    if name != "WriteConflict":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    # Other categories stay usable without initializing the Kubernetes client.
    from polyad.exceptions.kubernetes import WriteConflict

    globals()[name] = WriteConflict
    return WriteConflict
