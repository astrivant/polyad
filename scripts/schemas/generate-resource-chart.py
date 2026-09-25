"""
Derive resource-chart contracts and rendering rules from the versioned CRD definitions.
"""

from __future__ import annotations

import argparse
import copy
import json
import runpy
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts/polyad-crds"
NAMES = {"PolyGraph": "polygraphs", "GraphPolicy": "graphPolicies", "ShutdownPolicy": "shutdownPolicies", "Dragonfly": "dragonflies"}
DNS_NAME = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
TEMPLATE = {"type": "string", "pattern": r"\{\{[\s\S]*\}\}"}


def reference_fields(node: dict[str, Any], path: str, indent: int = 0) -> list[str]:
    """
    Describe every declared field, including nested list entries and optional alternatives.

    Args:
        node (dict[str, Any]): Canonical resource schema at this level.
        path (str): README-generator parameter path.
        indent (int): YAML indentation for this mapping.

    Returns:
        list[str]: Annotated YAML field catalog with illustrative placeholder values.
    """
    lines = []
    for name, field in node.get("properties", {}).items():
        full = f"{path}.{name}"
        padding = " " * indent
        required = name in node.get("required", [])
        description = "Required." if required else "Optional."
        if "default" in field:
            description += f" Default: {json.dumps(field['default'], ensure_ascii=False)}."
        else:
            description += " No CRD default; the value shown is illustrative."
        if field.get("enum"):
            description += " Choices: " + ", ".join(json.dumps(value) for value in field["enum"]) + "."
        description += " " + " ".join(field.get("description", "").split())
        lines.append(f"{padding}## @param {full} {description.strip()}")
        branch = next((part for part in field.get("anyOf", []) if part.get("type") != "null"), field)
        kind = branch.get("type")
        if kind == "object" and branch.get("properties"):
            lines.append(f"{padding}{name}:")
            lines.extend(reference_fields(branch, full, indent + 2))
        elif kind == "array" and branch.get("items", {}).get("properties"):
            lines.append(f"{padding}{name}:")
            lines.append(f"{padding}  -")
            lines.extend(reference_fields(branch["items"], full + "[]", indent + 4))
        else:
            value = field.get("default", branch.get("default"))
            if value is None:
                value = next(iter(branch.get("enum", [])), None)
            if value is None:
                value = {"object": {}, "array": [], "boolean": False, "string": "example", "integer": 1, "number": 1}.get(kind, "example")
                if kind in {"integer", "number"}:
                    value = branch.get("minimum", value)
            lines.append(f"{padding}{name}: {json.dumps(value, ensure_ascii=False)}")
        if branch.get("x-kubernetes-preserve-unknown-fields"):
            lines.append(
                f"{padding}# Native Kubernetes or application-defined fields are accepted here; consult the referenced resource API."
            )
        for constraint in branch.get("x-kubernetes-validations", []):
            lines.append(f"{padding}# Constraint: {constraint.get('message', constraint['rule'])}")
    return lines


def reference_values(key: str, entry: dict[str, Any], schema: dict[str, Any]) -> str:
    """
    Build an inert per-kind reference with a fully commented, typed field catalog.

    Args:
        key (str): Named resource map in chart values.
        entry (dict[str, Any]): Resource identity and canonical schema.
        schema (dict[str, Any]): Chart input contract used to derive README type tags.

    Returns:
        str: Safe values overlay plus the complete commented example.
    """
    annotate = runpy.run_path(str(ROOT / "scripts/validation/check-values.py"))["annotated_values"]
    example = [
        f"## @section {entry['kind']} field reference",
        f"{key}:",
        "  example:",
        *reference_fields(entry["schema"], f"{key}.example", 4),
    ]
    annotated = annotate("\n".join(example) + "\n", schema)
    return "\n".join(
        [
            "# yaml-language-server: $schema=values.reference.schema.json",
            "# Generated from the CRDs; regenerate with scripts/schemas/generate-all.py.",
            f"# {entry['kind']} field catalog. This overlay creates no instances.",
            "# Copy selected fields into your values; optional alternatives may be mutually exclusive.",
            "# Required means required within its containing object when that object is supplied.",
            "# Explicit CRD defaults are labeled; other values are placeholders, not defaults.",
            "# Names default to the map key and namespaces to .Release.Namespace.",
            "# Every field accepts tpl; e.g. '{{ .Values.graphs.pipeline.metadata.name }}'.",
            f"## @param {key} [object] Named {entry['kind']} instances; empty by default. Copy selected fields below to configure one.",
            f"{key}: {{}}",
            "",
            "# BEGIN RESOURCE EXAMPLE",
            *["# " + line for line in annotated.splitlines()],
            "# END RESOURCE EXAMPLE",
            "",
        ]
    )


def configurable(node: dict[str, Any]) -> dict[str, Any]:
    """
    Permit tpl expressions while preserving normal literal-value validation and defaults.

    Args:
        node (dict[str, Any]): Canonical CRD schema fragment.

    Returns:
        dict[str, Any]: Input contract allowing templates in place of typed fields.
    """
    result = copy.deepcopy(node)
    properties = result.get("properties", {})
    if properties:
        result["properties"] = {key: configurable(value) for key, value in properties.items()}
        result["required"] = [key for key in result.get("required", []) if "default" not in properties.get(key, {})]
        if not result.get("x-kubernetes-preserve-unknown-fields", False):
            result.setdefault("additionalProperties", False)
    if isinstance(result.get("items"), dict):
        result["items"] = configurable(result["items"])
    if isinstance(result.get("additionalProperties"), dict):
        result["additionalProperties"] = configurable(result["additionalProperties"])
    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword in result:
            result[keyword] = [configurable(branch) for branch in result[keyword]]
    return {"anyOf": [result, TEMPLATE], **({"description": node["description"]} if "description" in node else {})}


def generate() -> dict[Path, str]:
    """
    Generate a standalone values schema, shared catalog and operator dependency contract.

    Returns:
        dict[Path, str]: Complete generated artifacts keyed by their repository paths.
    """
    helpers = runpy.run_path(str(Path(__file__).with_name("generate-json-schemas.py")))
    catalog = {}
    properties: dict[str, Any] = {
        "enabled": {"type": "boolean", "description": "Enable instance rendering and, as a dependency, CRD inclusion."},
        "maxTplPasses": {
            "type": "integer",
            "minimum": 1,
            "maximum": 64,
            "description": "Bound reference resolution; cyclic or unresolved tpl expressions fail.",
        },
        "global": {"type": "object", "x-polyad-freeform": True, "description": "Shared Helm values inherited from the parent chart."},
        "variables": {
            "type": "object",
            "x-polyad-freeform": True,
            "description": "Administrator-defined inputs available to tpl expressions through .Values.variables.",
        },
    }
    lines = [
        "# yaml-language-server: $schema=values.schema.json",
        "# Generated from the CRDs by scripts/schemas/generate-resource-chart.py; do not edit.",
        "## @section Resource generation",
        "## @param enabled [boolean] Enable instance rendering; the parent uses this to include the dependency and its CRDs.",
        "enabled: true",
        "## @param maxTplPasses [integer] Maximum passes for field references; cycles fail instead of rendering unresolved values.",
        "maxTplPasses: 16",
        "## @param global [object] Shared values inherited from the parent chart; reference them with .Values.global.",
        "global: {}",
        "## @param variables [object] Shared inputs for tpl expressions; reference them with .Values.variables.",
        "variables: {}",
        "## @section Named resources",
    ]
    for path in sorted((CHART / "crds").glob("*.yaml")):
        crd = yaml.safe_load(path.read_text())
        kind = crd["spec"]["names"]["kind"]
        key = NAMES.get(kind, kind[0].lower() + kind[1:] + "s")
        version = next(item for item in crd["spec"]["versions"] if item["storage"])
        source = helpers["json_schema"](version["schema"]["openAPIV3Schema"])
        source["properties"] = {
            name: value for name, value in source.get("properties", {}).items() if name not in {"apiVersion", "kind", "status"}
        }
        source["properties"]["metadata"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "maxLength": 253, "pattern": DNS_NAME},
                "namespace": {"type": "string", "maxLength": 63, "pattern": r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"},
                "labels": {"type": "object", "additionalProperties": {"type": "string"}},
                "annotations": {"type": "object", "additionalProperties": {"type": "string"}},
            },
        }
        catalog[key] = {"kind": kind, "apiVersion": f"{crd['spec']['group']}/{version['name']}", "schema": source}
        properties[key] = {
            "type": "object",
            "description": f"{kind} instances keyed by Kubernetes name; fields accept tpl and use CRD defaults.",
            "patternProperties": {DNS_NAME: configurable(source)["anyOf"][0]},
            "additionalProperties": False,
        }
        lines.extend(
            [
                f"## @param {key} [object] {kind} instances keyed by name; spec and metadata support tpl references to other maps.",
                f"{key}: {{}}",
            ]
        )
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Polyad CRD and named resource chart values",
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    main_path = ROOT / "charts/polyad/values.schema.json"
    main_schema = json.loads(main_path.read_text())
    main_schema["properties"]["polyadResources"] = {key: value for key, value in schema.items() if key not in {"$schema", "title"}}
    main_schema["properties"]["polyadResources"]["description"] = (
        "Generated dependency contract from charts/polyad-crds; edit the CRDs and regenerate."
    )
    outputs = {
        CHART / "values.yaml": "\n".join(lines) + "\n",
        CHART / "values.schema.json": json.dumps(schema, indent=2) + "\n",
        CHART / "files/resource-catalog.json": json.dumps(catalog, indent=2) + "\n",
        main_path: json.dumps(main_schema, indent=2) + "\n",
    }
    outputs.update({CHART / f"values-{key}.reference.yaml": reference_values(key, entry, schema) for key, entry in catalog.items()})
    return outputs


def main() -> int:
    """
    Write generated resource contracts or check their consistency without changing files.

    Returns:
        int: Nonzero when check mode detects stale artifacts.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = []
    for path, content in generate().items():
        if not path.exists() or path.read_text() != content:
            changed.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
    if changed:
        print(("Stale" if args.check else "Generated") + " resource chart artifacts: " + ", ".join(changed))
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
