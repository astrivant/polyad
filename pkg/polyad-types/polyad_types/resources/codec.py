"""
Structure Kubernetes documents and lower ASTs at the API serialization boundary.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, TypeVar, cast

from attrs import fields
from cattrs import Converter
from cattrs.gen import make_dict_structure_fn

from polyad_types.resources.base import Resource
from polyad_types.resources.common import AST
from polyad_types.resources.kubernetes import ConfigMap
from polyad_types.resources.registry import RESOURCE_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

__all__ = (
    "T",
    "converter",
    "encode_body",
    "from_document",
    "to_document",
)


T = TypeVar("T", bound=AST)

converter = Converter(forbid_extra_keys=True, detailed_validation=False)


def _is_ast(cls: Any) -> bool:
    return isinstance(cls, type) and issubclass(cls, AST)


def _unstructure(value: AST) -> dict[str, Any]:
    # Extensions survive round trips, but cannot impersonate typed fields or resource identity.
    names = {field.name for field in fields(type(value))} - {"extra"}
    reserved = names | ({"apiVersion", "kind", "spec"} if isinstance(value, Resource) else set())
    if reserved & value.extra.keys():
        raise ValueError("extension fields cannot override modeled resource fields")
    result = copy.deepcopy(value.extra)
    for field in fields(type(value)):
        item = getattr(value, field.name)

        # Omission preserves a field; an explicit null can delete it in a Kubernetes merge patch.
        if field.name != "extra" and (item is not None or field.metadata.get("emit_none", False)):
            result[field.name] = converter.unstructure(item)
    if isinstance(value, Resource):
        result.update(apiVersion=value.resource_type.api_version, kind=value.resource_type.kind)
    return result


def _structure_factory(cls: type[T]) -> Callable[[dict[str, Any], Any], T]:
    generated = make_dict_structure_fn(cls, converter)
    names = {field.name for field in fields(cls)} - {"extra"}

    def structure(value: dict[str, Any], _: Any) -> T:
        """
        Decode modeled fields and retain unmodeled native extensions.

        Args:
            value (dict[str, Any]): Native document to decode.

        Returns:
            T: Structured model retaining independent extension fields.
        """
        if not isinstance(value, dict):
            raise TypeError("an AST requires an object document")
        value = copy.deepcopy(value)
        if issubclass(cls, Resource):
            descriptor = cls.resource_type
            if (
                value.pop("kind", descriptor.kind) != descriptor.kind
                or value.pop("apiVersion", descriptor.api_version) != descriptor.api_version
            ):
                raise ValueError("resource kind or API version does not match its AST")
            if cls is ConfigMap and "spec" in value:
                raise ValueError("ConfigMap has data fields, not a spec")

        # Validate the modeled contract without discarding native Kubernetes extension fields.
        known = {key: item for key, item in value.items() if key in names}
        known["extra"] = {key: item for key, item in value.items() if key not in names}
        return generated(known, cls)

    return structure


converter.register_unstructure_hook_func(_is_ast, _unstructure)
converter.register_structure_hook_factory(_is_ast, _structure_factory)


def to_document(value: AST) -> dict[str, Any]:
    """
    Return an independent API document, retaining explicitly modeled merge-patch nulls.

    Args:
        value (AST): Model to serialize as a native API document.

    Returns:
        dict[str, Any]: Native document; optional fields are omitted unless marked emit_none.
    """
    return copy.deepcopy(cast("dict[str, Any]", converter.unstructure(value)))


def from_document(document: dict[str, Any], *, kind: str | None = None) -> Resource:
    """
    Dispatch by known kind, including list items whose type metadata is omitted.

    Args:
        document (dict[str, Any]): Native Kubernetes resource document.
        kind (str | None): Kubernetes resource kind.

    Returns:
        Resource: Typed resource selected from the registered Kubernetes kinds.
    """
    identity = kind or document.get("kind")
    if not isinstance(identity, str) or identity not in RESOURCE_REGISTRY:
        raise ValueError(f"unsupported resource kind: {identity}")
    return converter.structure(document, RESOURCE_REGISTRY[identity])


def encode_body(body: Any) -> Any:
    """
    Serialize model objects while preserving raw JSON patch and native payload support.

    Args:
        body (Any): Request payload, either a typed AST or native API document.

    Returns:
        Any: Serialized AST or unchanged native request body.
    """
    return to_document(body) if isinstance(body, AST) else body
