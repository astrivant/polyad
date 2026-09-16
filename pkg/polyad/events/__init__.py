"""
Bounded, replayable graph observations for downstream services.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    from polyad.events.builder import EventAPIBuilder as EventAPIBuilder
    from polyad.events.store import EventStore as EventStore

__all__ = ["EventAPIBuilder", "EventStore"]


def __getattr__(name: str) -> Any:
    """
    Keep event storage imports independent of the streaming HTTP application.

    Args:
        name (str): Public export requested by an importer.

    Returns:
        Any: Original public object, cached after its first import.

    Raises:
        AttributeError: The requested name is not a public export.
    """
    modules = {"EventAPIBuilder": "builder", "EventStore": "store"}
    if name not in modules:
        raise AttributeError(name)
    value = getattr(import_module(f"polyad.events.{modules[name]}"), name)
    globals()[name] = value
    return value
