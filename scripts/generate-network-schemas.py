"""
Generate graph networking and capacity CRD properties from the public attrs models.
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from polyad.compiler.asts import CapacityStatus
from polyad.compiler.passes.schema import structural_schema
from polyad.graph.activation import ActivationPolicy
from polyad.graph.capacity import CapacityPlan
from polyad.graph.network import NetworkAccess, NetworkPort
from polyad.graph.replication import Replication

if TYPE_CHECKING:
    from typing import Any

ROOT = ("spec", "versions", 0, "schema", "openAPIV3Schema", "properties", "spec", "properties")


def refresh(source: str, path: tuple[str | int, ...], name: str, schema: dict[str, Any]) -> str:
    """
    Replace or append a generated property while retaining unrelated source formatting.

    Args:
        source (str): Original CRD YAML.
        path (tuple[str | int, ...]): Path to the containing properties mapping.
        name (str): Generated property name.
        schema (dict[str, Any]): Structural schema produced from an attrs model.

    Returns:
        str: Updated CRD YAML.
    """
    document = yaml.safe_load(source)
    node = yaml.compose(source)
    for part in path:
        document = document[part]
        node = node.value[part] if isinstance(part, int) else next(value for key, value in node.value if key.value == part)
    if document.get(name) == schema:
        return source
    indent = node.value[0][0].start_mark.column
    found = next(((key, value) for key, value in node.value if key.value == name), None)
    if found:
        key, value = found
        start = key.start_mark.index - indent
        end = value.end_mark.index - value.end_mark.column
    else:
        start = end = node.value[0][0].start_mark.index - indent
    rendered = textwrap.indent(yaml.safe_dump({name: schema}, sort_keys=False, width=100), " " * indent)
    return source[:start] + rendered + source[end:]


def main() -> int:
    """
    Regenerate schemas or report model drift without modifying files.

    Returns:
        int: Nonzero when check mode discovers stale schemas.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[1] / "charts/polyad/crds"
    changed = []
    for kind in ("graphs", "polygraphs", "rewrites", "graphrules"):
        path = directory / f"{kind}.yaml"
        source = path.read_text()
        props = ROOT + (("topology", "properties") if kind == "rewrites" else ())
        updated = refresh(source, props, "network", structural_schema(NetworkAccess))
        if kind == "graphrules":
            updated = refresh(
                updated,
                props,
                "scope",
                {
                    "type": "string",
                    "enum": ["Boundary", "Subtree"],
                    "default": "Subtree",
                    "description": "Boundary applies locally; Subtree propagates. Namespace rules select every boundary.",
                },
            )
        if kind != "graphrules":
            updated = refresh(updated, props, "capacity", structural_schema(CapacityPlan))
            if kind != "rewrites":
                status_props = ROOT[:-2] + ("status", "properties")
                updated = refresh(updated, status_props, "capacity", structural_schema(CapacityStatus))
            updated = refresh(
                updated,
                (*props, "connections", "items", "properties"),
                "ports",
                {
                    "type": "array",
                    "description": "Destination transport grants; omitted connections remain data-flow declarations.",
                    "items": structural_schema(NetworkPort),
                },
            )
        if updated != source:
            changed.append(kind)
            if not args.check:
                path.write_text(updated)
    for kind in ("graphs", "polygraphs", "workloads", "ephemerals", "daemons"):
        path = directory / f"{kind}.yaml"
        source = path.read_text()
        updated = refresh(source, ROOT, "activation", structural_schema(ActivationPolicy))
        if updated != source:
            changed.append(kind)
            if not args.check:
                path.write_text(updated)
    path = directory / "replicagroups.yaml"
    source = path.read_text()
    schema = structural_schema(Replication)
    for name, value in {
        "replicas": 1,
        "minReplicas": 0,
        "maxReplicas": 32,
        "templateOnly": False,
        "inheritReplicas": True,
        "suspend": False,
    }.items():
        schema["properties"][name]["default"] = value
    schema["x-kubernetes-validations"] = [
        {"rule": "self.minReplicas <= self.replicas && self.replicas <= self.maxReplicas", "message": "replicas must respect group bounds"},
        {
            "rule": "!has(oldSelf.replicaSource) || !oldSelf.inheritReplicas || self.replicas == oldSelf.replicas || !self.inheritReplicas",
            "message": "disable inheritReplicas before scaling an individual generated instance",
        },
        {
            "rule": "!has(oldSelf.replicaSource) || (has(self.replicaSource) && self.replicaSource == oldSelf.replicaSource)",
            "message": "replica source identity is immutable",
        },
    ]
    updated = refresh(source, ROOT[:-2], "spec", schema)
    if updated != source:
        changed.append("replicagroups")
        if not args.check:
            path.write_text(updated)
    if changed:
        print(("Stale" if args.check else "Regenerated") + " network and capacity schemas: " + ", ".join(changed))
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
