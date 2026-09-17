"""
Load shipped JSON Schemas without an operator, validator or network connection.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import TYPE_CHECKING, cast

from polyad_types.resources.common import VERSION

if TYPE_CHECKING:
    from typing import Any


def available_schemas() -> tuple[str, ...]:
    """
    List the schema names included in this installed distribution.

    Returns:
        tuple[str, ...]: Sorted names accepted by load_schema, without filename suffixes.
    """
    return tuple(
        sorted(item.name.removesuffix(".schema.json") for item in files(__package__).iterdir() if item.name.endswith(".schema.json"))
    )


def load_schema(name: str) -> dict[str, Any]:
    """
    Read an independent schema document from package resources, including zipped wheels.

    Args:
        name (str): Name from available_schemas, such as events, models or helm-values.

    Returns:
        dict[str, Any]: Fresh JSON Schema; unknown names raise ValueError.
    """
    if name not in available_schemas():
        raise ValueError(f"unknown packaged schema: {name!r}; choose from {', '.join(available_schemas())}")
    return cast("dict[str, Any]", json.loads(files(__package__).joinpath(f"{name}.schema.json").read_text(encoding="utf-8")))


def schema_for(model: type) -> dict[str, Any]:
    """
    Select a shared model's serialized shape with all references bundled locally.

    Args:
        model (type): Public attrs model from polyad_types; generic classes use their declared bounds.

    Returns:
        dict[str, Any]: Draft 2020-12 model schema; unrecognized models raise ValueError.
    """
    schema = load_schema("models")
    name = f"{model.__module__}.{model.__qualname__}"
    if name not in schema["$defs"]:
        raise ValueError(f"no packaged model schema for {name}")
    return {"$schema": schema["$schema"], "title": name, "$ref": f"#/$defs/{name}", "$defs": schema["$defs"]}


def resource_schema(kind: str, version: str = VERSION) -> dict[str, Any]:
    """
    Select a Polyad manifest schema generated from the chart's CRD contract.

    Args:
        kind (str): Polyad Kubernetes kind, such as Graph, PolyGraph or Daemon.
        version (str): Served CRD version included with this package.

    Returns:
        dict[str, Any]: Draft 2020-12 schema; CEL and Kubernetes extensions remain annotations.
    """
    return load_schema(f"{kind.lower()}.{version}")
