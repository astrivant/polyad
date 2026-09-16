"""
HTTP APIs for composition, activation and temporary graph connections.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from polyad_types.requests import CompositionItem as CompositionItem
from polyad_types.requests import CompositionRequest as CompositionRequest

if TYPE_CHECKING:
    from typing import Any

    from polyad.api.app import create_app as create_app
    from polyad.api.builder import APIBuilder as APIBuilder
    from polyad.api.limits import RateLimitPolicy as RateLimitPolicy

__all__ = ["APIBuilder", "CompositionItem", "CompositionRequest", "RateLimitPolicy", "create_app"]


def __getattr__(name: str) -> Any:
    """
    Load HTTP exports only when consumers request them.

    Args:
        name (str): Public export requested by an importer.

    Returns:
        Any: Original public object, cached after its first import.

    Raises:
        AttributeError: The requested name is not a public export.
    """
    modules = {"create_app": "app", "APIBuilder": "builder", "RateLimitPolicy": "limits"}
    if name not in modules:
        raise AttributeError(name)
    value = getattr(import_module(f"polyad.api.{modules[name]}"), name)
    globals()[name] = value
    return value
