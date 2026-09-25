"""
Generate graph networking and capacity CRD properties from the public attrs models.
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from polyad.compiler.passes.schema import structural_schema
from polyad_types.api.requests import ConnectionRequest
from polyad_types.graphs.activation import ActivationPolicy
from polyad_types.graphs.capacity import CapacityPlan, CapacityTuning
from polyad_types.graphs.policies import CheegerComputation
from polyad_types.graphs.replication import Replication
from polyad_types.graphs.topology import GraphNode, ThroughputPolicy
from polyad_types.networking.access import NetworkAccess, NetworkPort
from polyad_types.networking.traffic import TrafficRoute, TrafficWeights
from polyad_types.resources import CapacityStatus

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


def cheeger_result_schema() -> dict[str, Any]:
    """
    Keep current, failed and candidate calculation certificates identical in persisted status.

    Returns:
        dict[str, Any]: OpenAPI schema retaining every numeric solver diagnostic and its cut witness.
    """
    return {
        "type": "object",
        "nullable": True,
        "properties": {
            "exact": {"type": "boolean"},
            "lowerBound": {"type": "number"},
            "upperBound": {"type": "number", "nullable": True},
            "cut": {"type": "array", "items": {"type": "string"}},
            "evaluatedCuts": {"type": "integer", "minimum": 0},
            "reason": {"type": "string"},
            "stage": {"type": "string"},
            "edgeChurn": {"type": "number", "minimum": 0, "maximum": 1},
            "skippedPriorityCuts": {"type": "integer", "minimum": 0},
            "durationSeconds": {"type": "number", "minimum": 0},
            "inputs": {"type": "object", "additionalProperties": {"type": "number"}},
            "scheduler": {"type": "object", "x-kubernetes-preserve-unknown-fields": True},
        },
    }


def main() -> int:
    """
    Regenerate schemas or report model drift without modifying files.

    Returns:
        int: Nonzero when check mode discovers stale schemas.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[2] / "charts/polyad-crds/crds"
    changed = []
    for kind in ("graphs", "polygraphs", "rewrites", "graphpolicies"):
        path = directory / f"{kind}.yaml"
        source = path.read_text()
        props = ROOT + (("topology", "properties") if kind == "rewrites" else ())
        updated = refresh(source, props, "network", structural_schema(NetworkAccess))
        if kind == "graphpolicies":
            updated = refresh(updated, props, "cheegerComputation", structural_schema(CheegerComputation))
            updated = refresh(
                updated,
                props,
                "scope",
                {
                    "type": "string",
                    "enum": ["Boundary", "Subtree"],
                    "default": "Subtree",
                    "description": "Boundary applies locally; Subtree propagates. Namespace policies select every boundary.",
                },
            )
        if kind != "graphpolicies":
            cluster_schema = structural_schema(GraphNode)["properties"]["cluster"]
            if kind == "graphs":
                cluster_schema["x-kubernetes-validations"] = [{"rule": "false", "message": "Cluster placement belongs on PolyGraph nodes."}]
            updated = refresh(updated, (*props, "nodes", "items", "properties"), "cluster", cluster_schema)
            updated = refresh(updated, props, "capacity", structural_schema(CapacityPlan))
            updated = refresh(updated, props, "throughput", structural_schema(ThroughputPolicy))
            updated = refresh(updated, props, "traffic", {"type": "array", "maxItems": 16, "items": structural_schema(TrafficRoute)})
            if kind != "rewrites":
                status_props = ROOT[:-2] + ("status", "properties")
                updated = refresh(updated, status_props, "capacity", structural_schema(CapacityStatus))
                updated = refresh(
                    updated,
                    status_props,
                    "throughput",
                    {
                        "type": "object",
                        "properties": {
                            "currentTraffic": {"type": "array", "items": structural_schema(TrafficRoute)},
                            "proposedTraffic": {"type": "array", "items": structural_schema(TrafficRoute)},
                            "targetTraffic": {"type": "array", "items": structural_schema(TrafficWeights)},
                            **{
                                name: {**structural_schema(CapacityTuning), "nullable": True}
                                for name in ("currentCapacity", "targetCapacity", "proposedCapacity")
                            },
                            "observedGeneration": {"type": "integer", "minimum": 1},
                            "computation": cheeger_result_schema(),
                            "currentComputation": cheeger_result_schema(),
                            "candidateComputations": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        **cheeger_result_schema()["properties"],
                                        "layout": {"type": "string"},
                                    },
                                },
                            },
                            **{
                                name: {"type": "string", "nullable": True}
                                for name in ("mode", "phase", "observedAt", "recommendedLayout", "demandSignal", "demandUnit")
                            },
                            **{
                                name: {"type": "number", "nullable": True}
                                for name in (
                                    "currentCheeger",
                                    "proposedCheeger",
                                    "offeredPerSecond",
                                    "completedPerSecond",
                                    "demandValue",
                                )
                            },
                            "target": {
                                "type": "object",
                                "nullable": True,
                                "properties": {name: {"type": "number", "nullable": True, "minimum": 0} for name in ("minimum", "maximum")},
                            },
                        },
                    },
                )
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
    for kind in ("graphs", "polygraphs", "workloads", "daemons"):
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
    path = directory / "temporaryconnections.yaml"
    source = path.read_text()
    schema = structural_schema(ConnectionRequest)
    schema["properties"]["requester"] = {
        "type": "object",
        "required": ["username", "uid"],
        "properties": {
            "cluster": {"type": "string", "maxLength": 63},
            "username": {"type": "string", "maxLength": 320, "pattern": "^system:serviceaccount:[a-z0-9-]+:[a-z0-9.-]+$"},
            "uid": {"type": "string", "minLength": 1, "maxLength": 128},
        },
    }
    schema["required"].append("requester")
    schema["x-kubernetes-validations"] = [
        {"rule": "self == oldSelf", "message": "Connection intent is immutable; a new requestId creates a new deadline."},
        {"rule": "self.source != self.target", "message": "Temporary connections require distinct endpoints."},
    ]
    updated = refresh(source, ROOT[:-2], "spec", schema)
    if updated != source:
        changed.append("temporaryconnections")
        if not args.check:
            path.write_text(updated)
    if changed:
        print(("Stale" if args.check else "Regenerated") + " network and capacity schemas: " + ", ".join(changed))
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
