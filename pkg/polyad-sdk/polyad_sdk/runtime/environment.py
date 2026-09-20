"""
Capture the complete process environment once when the SDK is imported.

Applications can import env directly and supply overrides without changing
os.environ. The mapping includes credentials as well as public context; the SDK
does not log or publish it. WorkloadContext provides the typed public projection.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = (
    "env",
    "refresh_environment",
)


env: dict[str, str] = dict(os.environ)


def refresh_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """
    Deliberately replace the SDK snapshot while preserving previously imported references.

    Call during application configuration, before starting observation threads.
    Existing services retain their already constructed identity and settings.

    Args:
        environ (Mapping[str, str] | None): Replacement environment; defaults to current os.environ.

    Returns:
        dict[str, str]: The same exported env dictionary with refreshed contents.
    """

    # Copy before clearing: callers may pass the exported mapping itself. Mutate
    # that mapping in place so existing `from ... import env` references stay valid.
    snapshot = dict(os.environ if environ is None else environ)
    env.clear()
    env.update(snapshot)
    return env
