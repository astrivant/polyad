"""
Generate the packaged JSON Schema directly from public event and tuning models.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from types import UnionType
from typing import TYPE_CHECKING, Literal, Union, get_args, get_origin, get_type_hints

from attrs import NOTHING, fields, has

from polyad_types.events.codec import CURSOR_PATTERN
from polyad_types.events.envelope import EventRebalanceSettings, EventStreamSettings
from polyad_types.events.models import EVENT_MODELS

if TYPE_CHECKING:
    from typing import Any


def event_schema() -> dict[str, Any]:
    """
    Build a closed discriminated schema while preserving explicitly extensible policy dictionaries.

    Returns:
        dict[str, Any]: Draft 2020-12 JSON Schema with reusable definitions and settings constraints.
    """
    from typing import Any as AnyValue

    definitions: dict[str, Any] = {}

    def lower(annotation: Any) -> dict[str, Any]:
        if annotation is AnyValue:
            return {}
        if annotation is type(None):
            return {"type": "null"}
        origin, arguments = get_origin(annotation), get_args(annotation)
        if origin in (UnionType, Union):
            return {"anyOf": [lower(item) for item in arguments]}
        if origin is Literal:
            return {"type": "string", "enum": list(arguments)}
        if origin is tuple or origin is list:
            return {"type": "array", "items": lower(arguments[0])}
        if origin is dict:
            return {"type": "object", "additionalProperties": lower(arguments[1])}
        primitives = {str: "string", int: "integer", float: "number", bool: "boolean"}
        if annotation in primitives:
            return {"type": primitives[annotation]}
        if not isinstance(annotation, type) or not has(annotation):
            raise TypeError(f"unsupported event schema annotation {annotation}")
        name = annotation.__name__
        if name not in definitions:
            result: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": {}}
            definitions[name] = result
            hints = get_type_hints(annotation)
            descriptions = {
                key: value
                for base in reversed(annotation.__mro__)
                for key, value in re.findall(r"^\s+(\w+) \([^\n]+\): ([^\n]+)", base.__doc__ or "", re.MULTILINE)
            }
            required = []
            for attribute in fields(annotation):
                result["properties"][attribute.name] = {**lower(hints[attribute.name]), **attribute.metadata.get("schema", {})}
                if attribute.name in descriptions:
                    result["properties"][attribute.name]["description"] = descriptions[attribute.name]
                if attribute.default is NOTHING:
                    required.append(attribute.name)
            if required:
                result["required"] = required
        return {"$ref": f"#/$defs/{name}"}

    variants = [lower(model) for model in dict.fromkeys(EVENT_MODELS.values())]
    lower(EventStreamSettings)
    lower(EventRebalanceSettings)
    for name in ("GraphEvent", "TopologyEvent", "ConnectionEvent"):
        definitions[name]["properties"]["id"]["pattern"] = f"^{CURSOR_PATTERN}$"
    for model in EVENT_MODELS.values():
        definitions[model.__name__]["required"] = ["id", "event", "data"]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/astrivant/polyad/raw/main/pkg/polyad-schemas/polyad_schemas/events/events.schema.json",
        "title": "Polyad event AST",
        "description": "Transport-neutral public observations and stream controls; SSE maps id/event/data to this JSON envelope.",
        "oneOf": variants,
        "$defs": definitions,
    }


def main() -> int:
    """
    Write the shipped artifact or fail when its models have drifted.

    Returns:
        int: Nonzero only when check mode detects a stale schema.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[2] / "pkg/polyad-schemas/polyad_schemas/events/events.schema.json"
    rendered = json.dumps(event_schema(), indent=2) + "\n"
    if args.check:
        return int(not path.exists() or path.read_text() != rendered)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
