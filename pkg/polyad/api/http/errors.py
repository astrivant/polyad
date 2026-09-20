"""
Share expected HTTP operation failures across API domains and listener workers.
"""

from __future__ import annotations

__all__ = (
    "Conflict",
    "Forbidden",
    "RequestError",
    "Unauthorized",
    "Unavailable",
)


class RequestError(Exception):
    """
    Preserve an expected client-facing failure across the asynchronous HTTP bridge.
    """


class Conflict(RequestError, ValueError):
    """
    Reject a request ID that already identifies different or deleting intent.
    """


class Unauthorized(RequestError):
    """
    Reject an invalid or unauthenticated service-account credential.
    """


class Forbidden(RequestError):
    """
    Reject callers outside the configured scope or Kubernetes authorization.
    """


class Unavailable(RuntimeError):
    """
    Report an uncertain submission without encouraging a new request identity.
    """
