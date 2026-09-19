"""
Feature-grouped HTTP endpoints sharing one operator application and server.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from polyad_types.api.requests import CompositionItem as CompositionItem
from polyad_types.api.requests import CompositionRequest as CompositionRequest

if TYPE_CHECKING:
    from typing import Any

    from polyad.api.composition.app import create_app as create_app
    from polyad.api.composition.builder import APIBuilder as APIBuilder
    from polyad.api.http.limits import RateLimitPolicy as RateLimitPolicy

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
    modules = {"create_app": "composition.app", "APIBuilder": "composition.builder", "RateLimitPolicy": "http.limits"}
    if name not in modules:
        raise AttributeError(name)
    value = getattr(import_module(f"polyad.api.{modules[name]}"), name)
    globals()[name] = value
    return value
