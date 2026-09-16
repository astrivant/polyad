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
    if node.get("type") != "object" or "properties" not in node:
        return {"$ref": f"values.schema.json#{pointer}"}
    result = {key: node[key] for key in ("type", "description", "additionalProperties") if key in node}
    result["properties"] = {
        name: partial(value, pointer + "/properties/" + name.replace("~", "~0").replace("/", "~1"))
        for name, value in node["properties"].items()
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
    chart = Path(__file__).resolve().parents[2] / "charts/polyad"
    canonical = json.loads((chart / "values.schema.json").read_text())
    schema = {"$schema": canonical["$schema"], **partial(canonical, "")}
    output = json.dumps(schema, indent=2) + "\n"
    path = chart / "values.reference.schema.json"
    if args.check:
        return int(not path.exists() or path.read_text() != output)
    path.write_text(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
