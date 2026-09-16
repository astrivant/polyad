"""
Validate shipped Helm values and require schemas for every supplied nested value.
"""

from __future__ import annotations

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
            *CHART.glob("values-*.reference.yaml"),
            *(ROOT / "examples").rglob("*values.yaml"),
            *(ROOT / "examples/deployment-profiles").glob("*.yaml"),
            *(ROOT / "tests/data").rglob("*values.yaml"),
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
    for keyword in ("anyOf", "oneOf"):
        if keyword in node:
            validator = jsonschema.Draft7Validator(root)
            candidates = [
                list(type_gaps(value, candidate, root, path))
                for candidate in node[keyword]
                if validator.evolve(schema=candidate).is_valid(value)
            ]
            yield from min(candidates, key=len) if candidates else [path]
            return
    if not any(key in node for key in ("type", "const", "enum")):
        yield path
        return
    if isinstance(value, dict):
        for key, child in value.items():
            schema = node.get("properties", {}).get(key, node.get("additionalProperties", {}))
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
    canonical = json.loads((CHART / "values.schema.json").read_text())
    jsonschema.Draft7Validator.check_schema(canonical)
    registry = Registry().with_resource("values.schema.json", Resource.from_contents(canonical))
    value = yaml.load(source, Loader=UniqueLoader)
    jsonschema.Draft7Validator(schema, registry=registry).validate(value)
    coverage = canonical if schema_path.name == "values.reference.schema.json" else schema
    gaps = list(type_gaps(value, coverage, coverage))
    if gaps:
        raise ValueError("missing nested type schemas: " + ", ".join(gaps))


def main() -> int:
    """
    Report all invalid values files in one run for pre-commit and CI.

    Returns:
        int: Nonzero when any shipped values file has a type or schema error.
    """
    failed = False
    paths = value_files()
    for path in paths:
        try:
            validate(path)
        except (ValueError, OSError, jsonschema.ValidationError, jsonschema.SchemaError, yaml.YAMLError) as error:
            print(f"{path.relative_to(ROOT)}: {error}")
            failed = True
    if not failed:
        print(f"Validated types and schema coverage in {len(paths)} Helm values files")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
