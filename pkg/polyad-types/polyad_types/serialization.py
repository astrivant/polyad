"""
Validate and serialize public models without importing the operator.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, TypeVar

from polyad_types.resources.codec import converter as converter

if TYPE_CHECKING:
    from typing import Any

T = TypeVar("T")

converter.register_structure_hook_func(lambda kind: kind is object, lambda value, _: value)


def to_dict(value: object) -> dict[str, Any]:
    """
    Serialize a model into an independent JSON object.

    Args:
        value (object): Resource, configuration, request or event model.

    Returns:
        dict[str, Any]: JSON-compatible fields, including resource kind and API version.
    """
    result = converter.unstructure(value)
    if not isinstance(result, dict):
        raise TypeError("expected an object model")
    return copy.deepcopy(result)


def from_dict(document: dict[str, Any], model: type[T]) -> T:
    """
    Decode a public model using its field types and constructor validation.

    Args:
        document (dict[str, Any]): JSON object to decode.
        model (type[T]): Resource, configuration, request or event class.

    Returns:
        T: Validated model independent of the input document.
    """
    return converter.structure(copy.deepcopy(document), model)
