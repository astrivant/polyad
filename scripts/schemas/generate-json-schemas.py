"""
Package shared model, Polyad manifest and Helm schemas as offline JSON artifacts.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import inspect
import json
import pkgutil
import re
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import TYPE_CHECKING, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

import yaml
from attrs import NOTHING, fields, has

import polyad_types
from polyad_types.event_codec import CURSOR_PATTERN
from polyad_types.event_models import EVENT_MODELS
from polyad_types.resources.common import AST, GROUP
from polyad_types.resources.resources import ConfigMap, Resource

if TYPE_CHECKING:
    from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "pkg/polyad-types/polyad_types/schemas"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE_ID = "https://github.com/astrivant/polyad/raw/main/pkg/polyad-types/polyad_types/schemas/"


def json_schema(node: Any) -> Any:
    """
    Translate structural OpenAPI keywords while retaining Kubernetes validation annotations.

    Args:
        node (Any): CRD schema subtree or model field constraints.

    Returns:
        Any: JSON Schema shape; Kubernetes CEL remains an unevaluated annotation.
    """
    if isinstance(node, list):
        return [json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    result = {key: json_schema(value) for key, value in node.items() if key != "nullable"}
    for bound in ("Minimum", "Maximum"):
        exclusive = f"exclusive{bound}"
        if isinstance(result.get(exclusive), bool):
            enabled = result.pop(exclusive)
            if enabled:
                result[exclusive] = result.pop(bound.lower())
    if result.get("x-kubernetes-int-or-string") and "anyOf" not in result:
        result["anyOf"] = [{"type": "integer"}, {"type": "string"}]
    return {"anyOf": [result, {"type": "null"}]} if node.get("nullable") else result


def merge_constraints(shape: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]:
    """
    Preserve inferred nested item types when field metadata supplies additional limits.

    Args:
        shape (dict[str, Any]): Inferred JSON Schema for a field.
        constraints (dict[str, Any]): Explicit schema metadata on that field.

    Returns:
        dict[str, Any]: Combined field schema without mutating either input.
    """
    result = copy.deepcopy(shape)
    for key, value in constraints.items():
        result[key] = merge_constraints(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def model_schema() -> dict[str, Any]:
    """
    Describe every public shared attrs model using its wire fields and explicit constraints.

    Returns:
        dict[str, Any]: Definition library; callers select a model through schema_for.
    """
    from typing import Any as AnyValue

    definitions: dict[str, Any] = {}

    def lower(annotation: Any) -> dict[str, Any]:
        if annotation is AnyValue or annotation is object:
            return {}
        if annotation is type(None):
            return {"type": "null"}
        if isinstance(annotation, TypeVar):
            if annotation.__constraints__:
                return {"anyOf": [lower(item) for item in annotation.__constraints__]}
            return lower(annotation.__bound__ or AnyValue)
        origin, arguments = get_origin(annotation), get_args(annotation)
        if origin in (UnionType, Union):
            return {"anyOf": [lower(item) for item in arguments]}
        if origin is Literal:
            return {"enum": list(arguments)}
        if origin in (tuple, list, set, frozenset):
            if origin is tuple and arguments[-1:] != (Ellipsis,):
                return {
                    "type": "array",
                    "prefixItems": [lower(item) for item in arguments],
                    "minItems": len(arguments),
                    "maxItems": len(arguments),
                }
            result = {"type": "array", "items": lower(arguments[0])}
            if origin in (set, frozenset):
                result["uniqueItems"] = True
            return result
        if origin is dict and arguments[0] is str:
            return {"type": "object", "additionalProperties": lower(arguments[1])}
        primitives = {str: "string", int: "integer", float: "number", bool: "boolean"}
        if annotation in primitives:
            return {"type": primitives[annotation]}
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return {"enum": [member.value for member in annotation]}
        if not isinstance(annotation, type) or not has(annotation):
            raise TypeError(f"unsupported public model schema annotation: {annotation}")
        name = f"{annotation.__module__}.{annotation.__qualname__}"
        if name not in definitions:
            is_ast = issubclass(annotation, AST)
            result = {"type": "object", "additionalProperties": is_ast, "properties": {}}
            definitions[name] = result
            result["description"] = " ".join((inspect.getdoc(annotation) or name).split("\n\nAttributes:", 1)[0].split())
            descriptions = {
                key: value
                for base in reversed(annotation.__mro__)
                for key, value in re.findall(r"^\s+(\w+) \([^\n]+\): ([^\n]+)", base.__doc__ or "", re.MULTILINE)
            }
            hints = get_type_hints(annotation)
            required = []
            for attribute in fields(annotation):
                if is_ast and attribute.name == "extra":
                    continue
                value = merge_constraints(lower(hints[attribute.name]), json_schema(attribute.metadata.get("schema", {})))
                if attribute.name in descriptions:
                    value.setdefault("description", descriptions[attribute.name])
                result["properties"][attribute.name] = value
                if attribute.default is NOTHING:
                    required.append(attribute.name)
            if issubclass(annotation, Resource) and hasattr(annotation, "resource_type"):
                descriptor = annotation.resource_type
                result["properties"].update(apiVersion={"const": descriptor.api_version}, kind={"const": descriptor.kind})
                result["properties"]["metadata"].update(required=["name"], properties={"name": {"type": "string", "minLength": 1}})
                required.extend(("apiVersion", "kind"))
            if annotation is ConfigMap:
                result["properties"]["spec"] = False
            if annotation in EVENT_MODELS.values():
                required = ["id", "event", "data"]
                if annotation.__name__ in {"GraphEvent", "TopologyEvent", "ConnectionEvent"}:
                    result["properties"]["id"]["pattern"] = f"^{CURSOR_PATTERN}$"
            if required:
                result["required"] = required
        return {"$ref": f"#/$defs/{name}"}

    for module_info in sorted(pkgutil.walk_packages(polyad_types.__path__, "polyad_types."), key=lambda item: item.name):
        module = importlib.import_module(module_info.name)
        for name, model in sorted(vars(module).items()):
            if not name.startswith("_") and isinstance(model, type) and model.__module__ == module.__name__ and has(model):
                lower(model)
    return {
        "$schema": DIALECT,
        "$id": BASE_ID + "models.schema.json",
        "title": "Polyad shared model definitions",
        "description": (
            "Serialized shapes and explicit field constraints. Select a definition; constructor and live admission checks still apply."
        ),
        "$defs": dict(sorted(definitions.items())),
    }


def rewrite_refs(node: Any, prefix: str, replacement: str) -> Any:
    """
    Rebase local references when embedding the Helm schema in the overlay artifact.

    Args:
        node (Any): JSON Schema document or subtree.
        prefix (str): Existing reference prefix.
        replacement (str): Bundled local reference prefix.

    Returns:
        Any: Independent document with references resolved entirely within one artifact.
    """
    if isinstance(node, list):
        return [rewrite_refs(item, prefix, replacement) for item in node]
    if not isinstance(node, dict):
        return node
    return {
        key: replacement + value[len(prefix) :] if key == "$ref" and value.startswith(prefix) else rewrite_refs(value, prefix, replacement)
        for key, value in node.items()
    }


def artifacts() -> dict[str, dict[str, Any]]:
    """
    Generate shared models, all first-party CRD versions and self-contained Helm contracts.

    Returns:
        dict[str, dict[str, Any]]: Stable artifact names mapped to generated documents.
    """
    documents = {"models": model_schema()}
    for path in sorted((ROOT / "charts/polyad/crds").glob("*.yaml")):
        crd = yaml.safe_load(path.read_text())
        if crd["spec"]["group"] != GROUP:
            continue
        kind = crd["spec"]["names"]["kind"]
        for version in crd["spec"]["versions"]:
            if not version["served"]:
                continue
            name = f"{kind.lower()}.{version['name']}"
            schema = json_schema(version["schema"]["openAPIV3Schema"])
            schema["properties"]["apiVersion"]["const"] = f"{GROUP}/{version['name']}"
            schema["properties"]["kind"]["const"] = kind
            schema["required"] = sorted(set(schema.get("required", [])) | {"apiVersion", "kind", "metadata"})
            documents[name] = {
                "$schema": DIALECT,
                "$id": BASE_ID + name + ".schema.json",
                "title": f"Polyad {kind} {version['name']}",
                **schema,
            }
    canonical = json.loads((ROOT / "charts/polyad/values.schema.json").read_text())
    documents["helm-values"] = {**canonical, "$id": BASE_ID + "helm-values.schema.json", "title": "Polyad merged Helm values"}
    overlay = json.loads((ROOT / "charts/polyad/values.reference.schema.json").read_text())
    overlay = rewrite_refs(overlay, "values.schema.json#", "#/definitions/helmValues")
    overlay.setdefault("definitions", {})["helmValues"] = rewrite_refs(canonical, "#", "#/definitions/helmValues")
    documents["helm-reference"] = {**overlay, "$id": BASE_ID + "helm-reference.schema.json", "title": "Polyad partial Helm values"}
    return documents


def main() -> int:
    """
    Write importable artifacts or detect stale and obsolete generated schemas.

    Returns:
        int: Nonzero when check mode discovers differences.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report drift without modifying packaged artifacts.")
    args = parser.parse_args()
    documents = artifacts()
    changed = []
    for name, document in documents.items():
        path = OUTPUT / f"{name}.schema.json"
        rendered = json.dumps(document, indent=2, allow_nan=False) + "\n"
        if not path.exists() or path.read_text() != rendered:
            changed.append(path.name)
            if not args.check:
                path.write_text(rendered)
    for path in OUTPUT.glob("*.schema.json"):
        if path.name != "events.schema.json" and path.name.removesuffix(".schema.json") not in documents:
            changed.append(path.name)
            if not args.check:
                path.unlink()
    if changed:
        print(("Stale" if args.check else "Regenerated") + " packaged schemas: " + ", ".join(changed))
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
