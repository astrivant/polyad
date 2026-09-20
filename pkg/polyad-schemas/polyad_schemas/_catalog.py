"""
Locate generated JSON artifacts by category using portable package resources.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable
    from typing import Any

__all__ = (
    "CATEGORIES",
    "available_schemas",
    "load_schema",
)


CATEGORIES = ("models", "resources", "events", "helm")


def _artifacts() -> dict[str, Traversable]:
    # Package resources also work from wheels; do not assume schemas are ordinary local files.
    return {
        item.name.removesuffix(".schema.json"): item
        for category in CATEGORIES
        for item in files("polyad_schemas").joinpath(category).iterdir()
        if item.name.endswith(".schema.json")
    }


def available_schemas() -> tuple[str, ...]:
    """
    List the schema names included in this installed distribution.

    Returns:
        tuple[str, ...]: Sorted names accepted by load_schema, without filename suffixes.
    """
    return tuple(sorted(_artifacts()))


def load_schema(name: str) -> dict[str, Any]:
    """
    Read an independent schema document from package resources, including zipped wheels.

    Args:
        name (str): Name from available_schemas, such as events, models or helm-values.

    Returns:
        dict[str, Any]: Fresh JSON Schema; unknown names raise ValueError.
    """
    artifacts = _artifacts()
    if name not in artifacts:
        raise ValueError(f"unknown packaged schema: {name!r}; choose from {', '.join(sorted(artifacts))}")

    # Decode per call so a consumer cannot mutate the next caller's validation contract.
    return cast("dict[str, Any]", json.loads(artifacts[name].read_text(encoding="utf-8")))
