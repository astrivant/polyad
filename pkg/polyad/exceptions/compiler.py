"""
Define failures caused by stale mutation compilation inputs.
"""

from __future__ import annotations

__all__ = ("PreconditionFailed",)


class PreconditionFailed(ValueError):
    """
    Require refreshed state before admitting a mutation with stale observations.
    """
