"""
Derive benchmark resource schemas from the CRD chart and type the study's tuning inputs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    """
    Generate or check the benchmark chart without duplicating the resource model definitions.

    Returns:
        int: Nonzero when the generated schema is stale in check mode.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    resources = json.loads((ROOT / "charts/polyad-crds/values.schema.json").read_text())
    resources.pop("$schema", None)
    resources["properties"]["variables"] = json.loads((ROOT / "pkg/polyad-benchmarks/polyad_benchmarks/variables.schema.json").read_text())
    value = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Polyad benchmark fixtures",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "global": {"type": "object", "x-polyad-freeform": True},
            "polyadResources": resources,
        },
    }
    value["properties"].update(json.loads((ROOT / "schemas/benchmark-services.schema.json").read_text())["properties"])
    target = ROOT / "charts/polyad-benchmarks/values.schema.json"
    content = json.dumps(value, indent=2) + "\n"
    if args.check:
        if not target.exists() or target.read_text() != content:
            print("Benchmark chart schema is stale; run scripts/schemas/generate-benchmark-chart.py")
            return 1
    else:
        target.write_text(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
