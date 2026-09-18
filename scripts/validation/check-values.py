"""
Validate shipped Helm values and require schemas for every supplied nested value.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
import yaml
from referencing import Registry, Resource

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts/polyad"
PARAMETER = re.compile(r"^(\s*##\s*@param\s+)(\S+)\s+(?:\[([^]]+)\]\s*)?(.*)$")


def commented_example(source: str) -> str | None:
    """
    Decode the inert resource field catalog for annotation checks and README generation.

    Args:
        source (str): Reference values with a marked commented example.

    Returns:
        str | None: Uncommented example, or None for ordinary values files.

    Raises:
        ValueError: An example contains active YAML or lacks its closing marker.
    """
    marker = "# BEGIN RESOURCE EXAMPLE\n"
    if marker not in source:
        return None
    body, end, _ = source.split(marker, 1)[1].partition("# END RESOURCE EXAMPLE")
    if not end or any(not line.startswith("# ") for line in body.splitlines()):
        raise ValueError("resource examples must be fully commented and have an end marker")
    return "\n".join(line[2:] for line in body.splitlines()) + "\n"


def annotated_values(source: str, schema: dict[str, Any]) -> str:
    """
    Add schema-derived type tags while preserving authored parameter descriptions.

    Args:
        source (str): Values YAML with optional parameter comments.
        schema (dict[str, Any]): Canonical schema for this values file.

    Returns:
        str: Values with explicit type tags on each documented mapping parameter.
    """
    lines = source.splitlines()
    matches = [(index, match) for index, line in enumerate(lines) if (match := PARAMETER.match(line))]
    existing: dict[str, list[tuple[int, re.Match[str]]]] = {}
    for index, match in matches:
        existing.setdefault(match[2], []).append((index, match))
    used: set[int] = set()
    previous: dict[str, int] = {}
    inserts: dict[int, list[str]] = {}

    def types_of(definition: dict[str, Any]) -> list[str]:
        kinds = definition.get("type", [])
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not kinds:
            kinds = sorted({kind for branch in definition.get("anyOf", []) for kind in types_of(branch)})
        return kinds

    def container_branch(definition: dict[str, Any], expected: str) -> dict[str, Any]:
        if definition.get("type") == expected:
            return definition
        for branch in definition.get("anyOf", []):
            candidate = container_branch(branch, expected)
            if candidate.get("type") == expected:
                return candidate
        return definition

    def visit(node: yaml.Node, definition: dict[str, Any], path: str = "") -> None:
        while "$ref" in definition:
            reference = definition["$ref"]
            if not reference.startswith("#/"):
                return
            definition = schema
            for part in reference[2:].split("/"):
                definition = definition[part.replace("~1", "/").replace("~0", "~")]
        if "anyOf" in definition:
            expected = "object" if isinstance(node, yaml.MappingNode) else "array" if isinstance(node, yaml.SequenceNode) else None
            if expected:
                definition = container_branch(definition, expected)
        if isinstance(node, yaml.SequenceNode):
            items = definition.get("items", {})
            if isinstance(items, dict):
                for item in node.value:
                    visit(item, items, path + "[]")
            return
        if not isinstance(node, yaml.MappingNode):
            return
        for key_node, value_node in node.value:
            name = key_node.value
            child = definition.get("properties", {}).get(name)
            if child is None:
                child = next(
                    (value for pattern, value in definition.get("patternProperties", {}).items() if re.search(pattern, name)), None
                )
            if child is None and isinstance(definition.get("additionalProperties"), dict):
                child = definition["additionalProperties"]
            if child is None:
                continue  # User-defined map keys are covered by their parent object's type.
            full = f"{path}.{name}" if path else name
            resolved = child
            while "$ref" in resolved and resolved["$ref"].startswith("#/"):
                reference = resolved["$ref"]
                resolved = schema
                for part in reference[2:].split("/"):
                    resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
            types = types_of(resolved)
            tags = [kind for kind in types if kind != "null"] + (["nullable"] if "null" in types else [])
            if not tags:
                raise ValueError(f"{full} has no explicit annotation type")
            rendered = ", ".join(tags)
            candidates = [
                (index, match)
                for index, match in existing.get(full, [])
                if index not in used and previous.get(full, -1) < index < key_node.start_mark.line
            ]
            if len(candidates) > 1:
                raise ValueError(f"duplicate @param annotation for {full}")
            if candidates:
                index, match = candidates[0]
                used.add(index)
                lines[index] = f"{match[1]}{full} [{rendered}] {match[4]}"
            else:
                description = " ".join(resolved.get("description", f"Settings for {full}.").split())
                line = lines[key_node.start_mark.line]
                indentation = len(line) - len(line.lstrip())
                comment = f"{' ' * indentation}## @param {full} [{rendered}] {description}"
                inserts.setdefault(key_node.start_mark.line, []).append(comment)
            previous[full] = key_node.start_mark.line
            visit(value_node, child, full)

    document = yaml.compose(source)
    if document is not None:
        visit(document, schema)
    if stale := {match[2] for index, match in matches if index not in used}:
        raise ValueError("@param annotations without a typed value: " + ", ".join(sorted(stale)))
    return "\n".join(part for index, line in enumerate(lines) for part in [*inserts.get(index, []), line]) + "\n"


class UniqueLoader(yaml.SafeLoader):
    """
    Reject duplicate keys instead of silently discarding administrator settings.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        """
        Construct a mapping only when every key has one unambiguous value.

        Args:
            node (yaml.MappingNode): YAML mapping being decoded.
            deep (bool): Recursively construct child values.

        Returns:
            dict[Any, Any]: Mapping with unique keys.

        Raises:
            ValueError: Duplicate keys would hide a supplied value.
        """
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate YAML key at line {node.start_mark.line + 1}")
        return super().construct_mapping(node, deep=deep)


def value_files() -> list[Path]:
    """
    Discover defaults, reference overlays, examples and reusable test values.

    Returns:
        list[Path]: Stable list of authored values documents, excluding resource manifests.
    """
    return sorted(
        {
            CHART / "values.yaml",
            ROOT / "charts/polyad-crds/values.yaml",
            *(ROOT / "charts/polyad-crds").glob("values-*.reference.yaml"),
            *CHART.glob("values-*.reference.yaml"),
            *(ROOT / "examples").rglob("*values.yaml"),
            *(ROOT / "examples/deployment-profiles").glob("*.yaml"),
            *(ROOT / "pkg/tests/data").rglob("*values.yaml"),
            *(ROOT / "terraform").glob("*values.yaml"),
            ROOT / "terraform/bootstrap/values.yaml",
        }
    )


def type_gaps(value: Any, node: dict[str, Any], root: dict[str, Any], path: str = "$") -> Iterator[str]:
    """
    Detect values accepted only because a schema leaves nested objects or lists untyped.

    Args:
        value (Any): Supplied YAML value.
        node (dict[str, Any]): Schema for this value.
        root (dict[str, Any]): Canonical schema used to resolve local references.
        path (str): Human-readable value path.

    Yields:
        str: A supplied path lacking an explicit type or typed child schema.
    """
    while "$ref" in node:
        pointer = node["$ref"]
        if not pointer.startswith("#/"):
            raise ValueError(f"type coverage requires a local schema reference: {pointer}")
        node = root
        for part in pointer[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
    if node.get("x-kubernetes-preserve-unknown-fields") or node.get("x-polyad-freeform"):
        return
    for keyword in ("anyOf", "oneOf"):
        if keyword in node:
            validator = jsonschema.Draft7Validator(root)
            candidates = [
                list(type_gaps(value, candidate, root, path))
                for candidate in node[keyword]
                if all(error.validator == "required" for error in validator.evolve(schema=candidate).iter_errors(value))
            ]
            yield from min(candidates, key=len) if candidates else [path]
            return
    if not any(key in node for key in ("type", "const", "enum")):
        yield path
        return
    if isinstance(value, dict):
        for key, child in value.items():
            schema = node.get("properties", {}).get(key, node.get("additionalProperties", {}))
            for pattern, matching in node.get("patternProperties", {}).items():
                if re.search(pattern, key):
                    schema = matching
                    break
            if not isinstance(schema, dict):
                schema = {}
            yield from type_gaps(child, schema, root, f"{path}.{key}")
    elif isinstance(value, list):
        items = node.get("items")
        if not isinstance(items, dict):
            yield f"{path}[]"
        else:
            for index, child in enumerate(value):
                yield from type_gaps(child, items, root, f"{path}[{index}]")


def validate(path: Path) -> None:
    """
    Check editor schema association, YAML types and coverage without requiring Helm or a cluster.

    Args:
        path (Path): Authored values document.

    Returns:
        None: Invalid types, duplicate keys and schema gaps raise an error.

    Raises:
        ValueError: The schema directive is absent or supplied values have no declared type.
    """
    source = path.read_text()
    match = re.match(r"# yaml-language-server: \$schema=(\S+)\n", source)
    if match is None:
        raise ValueError("missing YAML editor schema directive")
    schema_path = (path.parent / match[1]).resolve()
    schema = json.loads(schema_path.read_text())
    jsonschema.Draft7Validator.check_schema(schema)
    canonical_path = (
        schema_path.with_name("values.schema.json") if schema_path.name == "values.reference.schema.json" else CHART / "values.schema.json"
    )
    canonical = json.loads(canonical_path.read_text())
    jsonschema.Draft7Validator.check_schema(canonical)
    registry = Registry().with_resource("values.schema.json", Resource.from_contents(canonical))
    value = yaml.load(source, Loader=UniqueLoader)
    jsonschema.Draft7Validator(schema, registry=registry).validate(value)
    coverage = canonical if schema_path.name == "values.reference.schema.json" else schema
    gaps = list(type_gaps(value, coverage, coverage))
    if gaps:
        raise ValueError("missing nested type schemas: " + ", ".join(gaps))
    if annotated_values(source, coverage) != source:
        raise ValueError("missing or stale @param type tags; run scripts/validation/check-values.py --fix-annotations")
    if (example := commented_example(source)) is not None and annotated_values(example, coverage) != example:
        raise ValueError("missing or stale commented-example type tags; run scripts/schemas/generate-all.py")


def main() -> int:
    """
    Report all invalid values files in one run for pre-commit and CI.

    Returns:
        int: Nonzero when any shipped values file has a type or schema error.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix-annotations", action="store_true", help="Synchronize @param type tags with the values schemas.")
    args = parser.parse_args()
    failed = False
    paths = value_files()
    for path in paths:
        try:
            if args.fix_annotations:
                source = path.read_text()
                match = re.match(r"# yaml-language-server: \$schema=(\S+)\n", source)
                if match is None:
                    raise ValueError("missing YAML editor schema directive")
                schema_path = (path.parent / match[1]).resolve()
                if schema_path.name == "values.reference.schema.json":
                    schema_path = schema_path.with_name("values.schema.json")
                updated = annotated_values(source, json.loads(schema_path.read_text()))
                if updated != source:
                    path.write_text(updated)
            validate(path)
        except (ValueError, OSError, jsonschema.ValidationError, jsonschema.SchemaError, yaml.YAMLError) as error:
            print(f"{path.relative_to(ROOT)}: {error}")
            failed = True
    if not failed:
        print(f"Validated types and schema coverage in {len(paths)} Helm values files")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
