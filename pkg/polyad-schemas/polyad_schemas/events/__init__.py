"""
Load the transport-neutral event envelope and payload contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_schemas._catalog import load_schema

if TYPE_CHECKING:
    from typing import Any


def event_schema() -> dict[str, Any]:
    """
    Read the packaged JSON Schema for all supported event syntax trees.

    Returns:
        dict[str, Any]: Independent Draft 2020-12 schema including nested payload definitions.
    """
    return load_schema("events")
