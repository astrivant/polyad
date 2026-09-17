"""
Load serialized model contracts without importing their Python implementations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_schemas._catalog import load_schema

if TYPE_CHECKING:
    from typing import Any


def schema_for(model: type | str) -> dict[str, Any]:
    """
    Select a shared model's serialized shape with all references bundled locally.

    Args:
        model (type | str): Public attrs class or fully qualified name; generic classes use their declared bounds.

    Returns:
        dict[str, Any]: Draft 2020-12 model schema; unrecognized models raise ValueError.
    """
    schema = load_schema("models")
    name = model if isinstance(model, str) else f"{model.__module__}.{model.__qualname__}"
    if name not in schema["$defs"]:
        raise ValueError(f"no packaged model schema for {name}")
    return {"$schema": schema["$schema"], "title": name, "$ref": f"#/$defs/{name}", "$defs": schema["$defs"]}
