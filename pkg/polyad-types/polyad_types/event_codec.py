"""
Decode event syntax trees with strict primitive types and closed modeled objects.
"""

from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING, cast

from attrs import fields, has
from cattrs import Converter
from cattrs.errors import CattrsError
from cattrs.gen import make_dict_structure_fn

from polyad_types.event_models import EVENT_MODELS

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from polyad_types.event_models import EventAST

CURSOR_PATTERN = r"(?:0|[1-9][0-9]{0,19})-(?:0|[1-9][0-9]{0,19})"
_converter = Converter(forbid_extra_keys=True, detailed_validation=False)


def _primitive(value: Any, kind: type) -> Any:
    if type(value) is not kind and not (kind is float and type(value) is int):
        raise ValueError(f"expected {kind.__name__} in event")
    if kind is float and not math.isfinite(cast("float", value)):
        raise ValueError("event numbers must be finite")
    return value


for _kind in (str, int, float, bool):
    _converter.register_structure_hook(_kind, _primitive)


def _model(kind: type) -> Callable[[Any, Any], Any]:
    generated: Callable[..., Any] = make_dict_structure_fn(kind, _converter)

    def structure(value: Any, target: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("event model requires a JSON object")
        result = generated(value, target)
        for attribute in fields(kind):
            constraints = attribute.metadata.get("schema", {})
            item = getattr(result, attribute.name)
            if ("minimum" in constraints and item < constraints["minimum"]) or ("maximum" in constraints and item > constraints["maximum"]):
                raise ValueError(f"event {attribute.name} is outside its allowed range")
        return result

    return structure


_converter.register_structure_hook_factory(has, _model)


def decode_event(document: dict[str, Any]) -> EventAST:
    """
    Select the event AST by its discriminator and validate fields without primitive coercion.

    Args:
        document (dict[str, Any]): Transport-neutral id/event/data envelope.

    Returns:
        EventAST: Independent typed event; invalid or unknown fields raise ValueError or TypeError.
    """
    if not isinstance(document, dict) or set(document) != {"id", "event", "data"}:
        raise ValueError("event requires exactly id, event and data")
    name, cursor = document["event"], document["id"]
    if not isinstance(name, str) or name not in EVENT_MODELS or not isinstance(cursor, str):
        raise ValueError("unsupported event type or cursor")
    if name in {"graph", "topology", "connection"} and not re.fullmatch(CURSOR_PATTERN, cursor):
        raise ValueError("observation requires a Redis stream cursor")
    if name in {"reset", "unavailable", "heartbeat"} and cursor:
        raise ValueError("control events cannot advance the cursor")
    # Also reject NaN, infinity and non-JSON values inside explicitly extensible policy objects.
    document = json.loads(json.dumps(document, allow_nan=False))
    try:
        return cast("EventAST", _converter.structure(document, EVENT_MODELS[name]))
    except (CattrsError, KeyError) as error:
        raise ValueError(f"invalid {name} event fields") from error
