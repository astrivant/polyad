"""
Expose cached scheduler observations for monitoring and scaling integrations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    from polyad.api.metrics.builder import MetricsAPIBuilder as MetricsAPIBuilder

__all__ = ("MetricsAPIBuilder",)


def __getattr__(name: str) -> Any:
    """
    Load the HTTP metrics builder only when explicitly requested.

    Args:
        name (str): Public export requested by an importer.

    Returns:
        Any: Original metrics builder.

    Raises:
        AttributeError: The requested name is not a public export.
    """
    if name != "MetricsAPIBuilder":
        raise AttributeError(name)
    from polyad.api.metrics.builder import MetricsAPIBuilder

    globals()[name] = MetricsAPIBuilder
    return MetricsAPIBuilder
