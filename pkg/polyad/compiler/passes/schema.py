"""
Lower attrs observation trees to Kubernetes structural OpenAPI schemas.
"""

from __future__ import annotations

import copy
import inspect
import re
from types import UnionType
from typing import TYPE_CHECKING, Literal, Union, get_args, get_origin, get_type_hints

from attrs import NOTHING, fields, has

if TYPE_CHECKING:
    from typing import Any


def structural_schema(model: type) -> dict[str, Any]:
    """
    Inline a typed AST as a structural schema suitable for a CRD property.

    Field metadata under ``schema`` supplies descriptions and validation constraints.
    Optional annotations allow null; constructor defaults make fields optional on the
    wire without installing Kubernetes defaults. The codec's extension bag is excluded.
    Unsupported annotations and recursive models fail instead of emitting loose schemas.

    Args:
        model (type): Attrs model whose fields define the wire contract.

    Returns:
        dict[str, Any]: Independent OpenAPI schema with nested models inlined.
    """
    return _schema(model, ())


def _schema(annotation: Any, ancestors: tuple[type, ...]) -> dict[str, Any]:
    from typing import Any as AnyValue

    if annotation is AnyValue:
        return {"x-kubernetes-preserve-unknown-fields": True}
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        members = [member for member in arguments if member is not type(None)]
        if len(members) != 1 or type(None) not in arguments:
            raise TypeError(f"only nullable unions have a structural schema: {annotation}")
        return {**_schema(members[0], ancestors), "nullable": True}
    if origin is Literal:
        if not arguments or len({type(value) for value in arguments}) != 1:
            raise TypeError(f"literal values must have one primitive type: {annotation}")
        return {**_schema(type(arguments[0]), ancestors), "enum": list(arguments)}
    if origin is list or (origin is tuple and len(arguments) == 2 and arguments[1] is Ellipsis):
        return {"type": "array", "items": _schema(arguments[0], ancestors)}
    if origin is dict and arguments[0] is str:
        return {"type": "object", "additionalProperties": _schema(arguments[1], ancestors)}
    primitives = {str: "string", int: "integer", float: "number", bool: "boolean"}
    if annotation in primitives:
        return {"type": primitives[annotation]}
    if not isinstance(annotation, type) or not has(annotation):
        raise TypeError(f"unsupported structural schema annotation: {annotation}")

    # CRD structural fields are inlined, so recursive model references cannot be expanded safely.
    if annotation in ancestors:
        raise TypeError(f"recursive AST cannot be inlined: {annotation.__name__}")
    hints = get_type_hints(annotation)
    descriptions = dict(re.findall(r"^\s+(\w+) \([^\n]+\): ([^\n]+)", annotation.__doc__ or "", re.MULTILINE))
    properties, required = {}, []
    for attribute in fields(annotation):
        if attribute.name == "extra":
            continue
        value = _schema(hints[attribute.name], (*ancestors, annotation))
        constraints = copy.deepcopy(attribute.metadata.get("schema", {}))
        if attribute.name in descriptions:
            value.setdefault("description", descriptions[attribute.name])
        if "items" in constraints:
            value["items"].update(constraints.pop("items"))
        value.update(constraints)
        properties[attribute.name] = value
        if attribute.default is NOTHING:
            required.append(attribute.name)
    description = " ".join((inspect.getdoc(annotation) or annotation.__name__).split("\n\nAttributes:", 1)[0].split())
    result: dict[str, Any] = {"type": "object", "description": description, "properties": properties}
    if required:
        result["required"] = required
    return result
