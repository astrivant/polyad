"""
Generate an editor schema for partial Helm overlays while retaining full validation for array entries.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


def partial(node: dict[str, Any], pointer: str) -> dict[str, Any]:
    """
    Make mergeable mappings partial and reference the canonical types of replacement values.

    Args:
        node (dict[str, Any]): Canonical Helm values schema fragment.
        pointer (str): JSON Pointer to that fragment in the canonical file.

    Returns:
        dict[str, Any]: Overlay schema; Helm still checks conditional requirements after merging defaults.
    """
    if "anyOf" in node:
        return {"anyOf": [partial(branch, pointer + f"/anyOf/{index}") for index, branch in enumerate(node["anyOf"])]}
    if node.get("type") != "object" or not any(key in node for key in ("properties", "patternProperties")):
        return {"$ref": f"values.schema.json#{pointer}"}
    result = {key: node[key] for key in ("type", "description", "additionalProperties") if key in node}
    result["properties"] = {
        name: partial(value, pointer + "/properties/" + name.replace("~", "~0").replace("/", "~1"))
        for name, value in node.get("properties", {}).items()
    }
    if "patternProperties" in node:
        result["patternProperties"] = {
            pattern: partial(value, pointer + "/patternProperties/" + pattern.replace("~", "~0").replace("/", "~1"))
            for pattern, value in node["patternProperties"].items()
        }
    return result


def main() -> int:
    """
    Write the generated overlay schema or fail when the committed version is stale.

    Returns:
        int: Nonzero when check mode discovers schema drift.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = False
    for name in ("polyad", "polyad-crds"):
        chart = Path(__file__).resolve().parents[2] / "charts" / name
        canonical = json.loads((chart / "values.schema.json").read_text())
        schema = {"$schema": canonical["$schema"], **partial(canonical, "")}
        output = json.dumps(schema, indent=2) + "\n"
        path = chart / "values.reference.schema.json"
        stale |= not path.exists() or path.read_text() != output
        if not args.check:
            path.write_text(output)
    return int(args.check and stale)


if __name__ == "__main__":
    raise SystemExit(main())
