"""
Package shared model, Polyad manifest and Helm schemas as offline JSON artifacts.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import inspect
import json
import pkgutil
import re
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import TYPE_CHECKING, Literal, TypeVar, Union, get_args, get_origin, get_type_hints
from urllib.request import urlopen

import yaml
from attrs import NOTHING, fields, has

import polyad_types
from polyad_types.events.codec import CURSOR_PATTERN
from polyad_types.events.models import EVENT_MODELS
from polyad_types.resources.base import Resource
from polyad_types.resources.common import AST, GROUP
from polyad_types.resources.kubernetes import ConfigMap

if TYPE_CHECKING:
    from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "pkg/polyad-schemas/polyad_schemas"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE_ID = "https://github.com/astrivant/polyad/raw/main/pkg/polyad-schemas/polyad_schemas/"
CATALOG = ROOT / "schemas/sources.json"
CHART_SCHEMAS = ROOT / "charts/polyad/schemas"
RESOURCE_CHART_SCHEMAS = ROOT / "charts/polyad-crds/schemas"


def upstream_catalog() -> dict[str, Any]:
    """
    Read the central upstream pins and reject divergence from bundled chart dependencies.

    Returns:
        dict[str, Any]: Catalog with release, source, digest and license provenance.
    """
    catalog = json.loads(CATALOG.read_text())
    chart = yaml.safe_load((ROOT / "charts/polyad/Chart.yaml").read_text())
    dependencies = {item["name"]: item["version"] for item in chart["dependencies"]}
    for provider in catalog["providers"]:
        pin = provider.get("dependency")
        if pin and dependencies.get(pin["name"]) != pin["version"]:
            raise ValueError(f"{provider['name']} schema pin differs from Chart.yaml; update schemas/sources.json and refresh upstream")
    return catalog


def refresh_upstream() -> None:
    """
    Download explicitly pinned CRDs and licenses before writing their verified snapshot updates.

    Returns:
        None: Network access occurs only for an explicit refresh; failed reads leave snapshots unchanged.
    """
    catalog = upstream_catalog()
    fetched: dict[str, bytes] = {}
    parsed: dict[str, list[Any]] = {}
    updates: dict[Path, bytes] = {}
    for provider in catalog["providers"]:
        for entry in [*provider["resources"], provider["license"]]:
            source = entry["source"]
            if not source.startswith("https://raw.githubusercontent.com/") or f"/{provider['release']}/" not in source:
                raise ValueError(f"upstream source must name its pinned GitHub release: {source}")
            if source not in fetched:
                with urlopen(source, timeout=30) as response:
                    fetched[source] = response.read()
            content = fetched[source]
            if "kind" in entry:
                if source not in parsed:
                    parsed[source] = list(yaml.safe_load_all(content))
                schemas = [
                    version["schema"]["openAPIV3Schema"]
                    for crd in parsed[source]
                    if crd
                    and crd.get("kind") == "CustomResourceDefinition"
                    and crd["spec"]["group"] == entry["group"]
                    and crd["spec"]["names"]["kind"] == entry["kind"]
                    for version in crd["spec"]["versions"]
                    if version["name"] == entry["version"] and version["served"]
                ]
                if len(schemas) != 1:
                    raise ValueError(f"expected one served upstream schema for {entry['kind']} {entry['group']}/{entry['version']}")
                content = (json.dumps(schemas[0], indent=2) + "\n").encode()
            updates[ROOT / entry["path"]] = content
            entry["sha256"] = hashlib.sha256(content).hexdigest()
    for path, content in updates.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    CATALOG.write_text(json.dumps(catalog, indent=2) + "\n")


def checked_source(entry: dict[str, Any]) -> bytes:
    """
    Verify that an offline snapshot still matches the reviewed catalog digest.

    Args:
        entry (dict[str, Any]): Catalog resource or license entry.

    Returns:
        bytes: Verified source; manual edits or incomplete refreshes raise ValueError.
    """
    content = (ROOT / entry["path"]).read_bytes()
    if hashlib.sha256(content).hexdigest() != entry["sha256"]:
        raise ValueError(f"source digest mismatch: {entry['path']}; use --refresh-upstream after reviewing the source pin")
    return content


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
        "$id": BASE_ID + "models/models.schema.json",
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
    Generate models, local and pinned upstream CRDs, and self-contained Helm contracts.

    Returns:
        dict[str, dict[str, Any]]: Stable artifact names mapped to generated documents.
    """
    documents = {"models": model_schema()}
    for path in sorted((ROOT / "charts/polyad-crds/crds").glob("*.yaml")):
        crd = yaml.safe_load(path.read_text())
        group = crd["spec"]["group"]
        kind = crd["spec"]["names"]["kind"]
        for version in crd["spec"]["versions"]:
            if not version["served"]:
                continue
            name, schema = manifest_schema(kind, group, version["name"], version["schema"]["openAPIV3Schema"])
            schema["$comment"] = f"Generated from {path.relative_to(ROOT)}; do not edit this artifact."
            documents[name] = schema
    for provider in upstream_catalog()["providers"]:
        for entry in provider["resources"]:
            name, schema = manifest_schema(entry["kind"], entry["group"], entry["version"], json.loads(checked_source(entry)))
            schema["$comment"] = (
                f"Generated from {entry['source']} ({provider['release']}); source SHA-256 {entry['sha256']}. "
                f"Apache-2.0; see {provider['name']}-LICENSE. Do not edit this artifact."
            )
            documents[name] = schema
    canonical = json.loads((ROOT / "charts/polyad/values.schema.json").read_text())
    documents["helm-values"] = {**canonical, "$id": BASE_ID + "helm/helm-values.schema.json", "title": "Polyad merged Helm values"}
    overlay = json.loads((ROOT / "charts/polyad/values.reference.schema.json").read_text())
    overlay = rewrite_refs(overlay, "values.schema.json#", "#/definitions/helmValues")
    overlay.setdefault("definitions", {})["helmValues"] = rewrite_refs(canonical, "#", "#/definitions/helmValues")
    documents["helm-reference"] = {**overlay, "$id": BASE_ID + "helm/helm-reference.schema.json", "title": "Polyad partial Helm values"}
    resource_values = json.loads((ROOT / "charts/polyad-crds/values.schema.json").read_text())
    documents["helm-crds-values"] = {**resource_values, "$id": BASE_ID + "helm/helm-crds-values.schema.json"}
    resource_overlay = json.loads((ROOT / "charts/polyad-crds/values.reference.schema.json").read_text())
    resource_overlay = rewrite_refs(resource_overlay, "values.schema.json#", "#/definitions/resourceValues")
    resource_overlay.setdefault("definitions", {})["resourceValues"] = rewrite_refs(resource_values, "#", "#/definitions/resourceValues")
    documents["helm-crds-reference"] = {**resource_overlay, "$id": BASE_ID + "helm/helm-crds-reference.schema.json"}
    return documents


def manifest_schema(kind: str, group: str, version: str, source: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    Build one manifest contract for both Python consumers and chart validation.

    Args:
        kind (str): Kubernetes resource kind.
        group (str): API group used to disambiguate kinds such as Gateway.
        version (str): Served API version.
        source (dict[str, Any]): Canonical structural OpenAPI schema.

    Returns:
        tuple[str, dict[str, Any]]: Public artifact name and normalized manifest schema.
    """
    name = f"{kind.lower()}.{version}" if group == GROUP else f"{kind.lower()}.{group}.{version}"
    schema = json_schema(source)
    properties = schema.setdefault("properties", {})
    properties.setdefault("apiVersion", {"type": "string"})["const"] = f"{group}/{version}"
    properties.setdefault("kind", {"type": "string"})["const"] = kind
    properties.setdefault("metadata", {"type": "object"})
    schema["required"] = sorted(set(schema.get("required", [])) | {"apiVersion", "kind", "metadata"})
    return name, {"$schema": DIALECT, "$id": BASE_ID + "resources/" + name + ".schema.json", "title": f"{group} {kind} {version}", **schema}


def outputs() -> dict[Path, str]:
    """
    Derive both distribution copies and their license notices from the same source graph.

    Returns:
        dict[Path, str]: Generated package, chart and license files.
    """
    rendered = {}
    for name, document in artifacts().items():
        category = "models" if name == "models" else "helm" if name.startswith("helm-") else "resources"
        rendered[OUTPUT / category / f"{name}.schema.json"] = json.dumps(document, indent=2, allow_nan=False) + "\n"
        properties = document.get("properties", {})
        if "const" in properties.get("apiVersion", {}) and "const" in properties.get("kind", {}):
            group, version = properties["apiVersion"]["const"].split("/")
            filename = f"{properties['kind']['const'].lower()}-{group.split('.')[0]}-{version}.json"
            chart_schema = {**document, "$schema": "http://json-schema.org/draft-07/schema#"}
            rendered[CHART_SCHEMAS / filename] = json.dumps(chart_schema, indent=2, allow_nan=False) + "\n"
            if group in {GROUP, "dragonflydb.io"}:
                rendered[RESOURCE_CHART_SCHEMAS / filename] = rendered[CHART_SCHEMAS / filename]
    for provider in upstream_catalog()["providers"]:
        license_text = checked_source(provider["license"]).decode()
        for directory in (OUTPUT / "resources", CHART_SCHEMAS):
            rendered[directory / f"{provider['name']}-LICENSE"] = license_text
    license_text = (ROOT / "charts/polyad/LICENSE.dragonfly-operator").read_text()
    for directory in (OUTPUT / "resources", CHART_SCHEMAS, RESOURCE_CHART_SCHEMAS):
        rendered[directory / "dragonfly-operator-LICENSE"] = license_text
    return rendered


def main() -> int:
    """
    Write importable artifacts or detect stale and obsolete generated schemas.

    Returns:
        int: Nonzero when check mode discovers differences.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Report drift without modifying artifacts or accessing the network.")
    modes.add_argument(
        "--refresh-upstream", action="store_true", help="Fetch explicitly pinned upstream CRDs and licenses before generation."
    )
    args = parser.parse_args()
    if args.refresh_upstream:
        refresh_upstream()
    generated = outputs()
    changed = []
    for path, rendered in generated.items():
        if not path.exists() or path.read_text() != rendered:
            changed.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(rendered)
    managed = [
        *OUTPUT.rglob("*.schema.json"),
        *OUTPUT.rglob("*-LICENSE"),
        *CHART_SCHEMAS.glob("*.json"),
        *CHART_SCHEMAS.glob("*-LICENSE"),
        *RESOURCE_CHART_SCHEMAS.glob("*.json"),
        *RESOURCE_CHART_SCHEMAS.glob("*-LICENSE"),
    ]
    for path in managed:
        if path.name != "events.schema.json" and path not in generated:
            changed.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.unlink()
    if changed:
        print(("Stale" if args.check else "Regenerated") + " schema artifacts: " + ", ".join(changed))
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
