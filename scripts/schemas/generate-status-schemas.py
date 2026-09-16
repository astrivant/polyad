"""
Refresh the generated metrics property in graph CRDs without rewriting their specs.
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from polyad.compiler.passes.schema import structural_schema
from polyad_types.resources import GraphMetrics

if TYPE_CHECKING:
    from typing import Any

CRDS = ("graphs.yaml", "polygraphs.yaml", "replicagroups.yaml")
PROPERTY_PATH = ("spec", "versions", 0, "schema", "openAPIV3Schema", "properties", "status", "properties", "metrics")


def refresh(path: Path, schema: dict[str, Any], *, check: bool) -> bool:
    """
    Compare the checked-in schema or replace only its metrics subtree.

    Args:
        path (Path): Graph CRD manifest to update.
        schema (dict[str, Any]): Generated structural metrics schema.
        check (bool): Report semantic drift without writing files.

    Returns:
        bool: Whether the existing schema already matched the model.
    """
    source = path.read_text(encoding="utf-8")
    document = yaml.safe_load(source)
    node = yaml.compose(source)
    for part in PROPERTY_PATH:
        document = document[part]
        if isinstance(part, int):
            node = node.value[part]
        else:
            key, node = next((key, value) for key, value in node.value if key.value == part)
    if document == schema:
        return True
    if not check:
        # YAML marks preserve unrelated spec, status and printer-column formatting.
        indent = key.start_mark.column
        start = key.start_mark.index - indent
        end = node.end_mark.index - node.end_mark.column
        rendered = textwrap.indent(yaml.safe_dump({"metrics": schema}, sort_keys=False, width=100).rstrip() + "\n", " " * indent)
        path.write_text(source[:start] + rendered + source[end:], encoding="utf-8")
    return False


def main() -> int:
    """
    Regenerate all graph status schemas or fail if their models have drifted.

    Returns:
        int: Nonzero when check mode finds stale generated schemas.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail on schema drift without modifying manifests.")
    parser.add_argument("--crd-dir", type=Path, default=Path(__file__).resolve().parents[2] / "charts/polyad/crds")
    args = parser.parse_args()
    schema = structural_schema(GraphMetrics)
    changed = [name for name in CRDS if not refresh(args.crd_dir / name, schema, check=args.check)]
    if changed:
        print(("Stale" if args.check else "Regenerated") + " status schemas: " + ", ".join(changed))
        if args.check:
            print("Run: bash scripts/tooling/project-python.sh scripts/schemas/generate-status-schemas.py")
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
