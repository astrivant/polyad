"""
Render Flux healthCheckExprs from Polyad's supported resource registry.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from polyad.compiler.registry import RESOURCE_TYPES

CURRENT_GENERATION = "has(status.observedGeneration) && status.observedGeneration == metadata.generation"
FAILED = f"{CURRENT_GENERATION} && ((has(status.phase) && status.phase in ['Failed', 'Invalid']) || (has(status.failed) && status.failed))"
READY = (
    f"{CURRENT_GENERATION} && has(status.phase) && "
    "((status.phase == 'Completed' && has(status.completed) && status.completed) || "
    "(status.phase in ['Ready', 'Running'] && has(status.ready) && status.ready))"
)


def main() -> None:
    """
    Emit a Flux spec fragment, without changing an existing controller resource.

    Returns:
        None: Writes YAML or JSON to standard output.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON for automated validation.")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[2] / "integrations/fluxcd"
    checks = []
    for kind, descriptor in sorted(RESOURCE_TYPES.items()):
        if not descriptor.polyad:
            continue
        if descriptor.boundary:
            current = (directory / "graph-current.cel").read_text().strip()
            failed = (directory / "graph-failed.cel").read_text().strip()
            if kind == "ReplicaGroup":
                scale_current = (directory / "replica-current.cel").read_text().strip()
                current = f"({current}) && ({scale_current})"
        elif descriptor.definition:
            current, failed = "true", "false"
            if kind == "Daemon":
                failed = (
                    f"{CURRENT_GENERATION} && has(status.serviceLevel) && "
                    "has(status.serviceLevel.observedGeneration) && status.serviceLevel.observedGeneration == metadata.generation && "
                    "has(status.serviceLevel.state) && status.serviceLevel.state in ['Degraded', 'Unavailable']"
                )
        elif kind == "Rewrite":
            current, failed = f"{CURRENT_GENERATION} && has(status.applied) && status.applied", FAILED
        elif kind == "TemporaryConnection":
            current = f"{CURRENT_GENERATION} && has(status.phase) && status.phase in ['Active', 'Expired', 'Revoked']"
            failed = f"({FAILED}) || ({CURRENT_GENERATION} && has(status.phase) && status.phase == 'Rejected')"
        elif kind in {"OperatorPool", "RemoteScale", "DragonflyPool"}:
            current = f"{CURRENT_GENERATION} && has(status.phase) && status.phase == 'Ready'"
            failed = f"({FAILED}) || ({CURRENT_GENERATION} && has(status.phase) && status.phase == 'Blocked')"
        elif kind in {"Composition", "Activation"}:
            current, failed = READY, FAILED
            if kind == "Activation":
                current = f"({READY}) || ({CURRENT_GENERATION} && has(status.phase) && status.phase == 'Superseded')"
        else:
            raise ValueError(f"Flux health semantics are not defined for {kind}")
        progressing = f"has(metadata.deletionTimestamp) || ({CURRENT_GENERATION} && has(status.progressing) && status.progressing)"
        if kind == "Daemon":
            progressing = f"({progressing}) && !({failed})"
        checks.append(
            {
                "apiVersion": descriptor.api_version,
                "kind": kind,
                # Evaluated first by Flux: deletion and an explicitly published metrics transition mask failures.
                "inProgress": progressing,
                "failed": failed,
                "current": current,
            }
        )
    document = {"spec": {"healthCheckExprs": checks}}
    if args.json:
        print(json.dumps(document))
    else:
        yaml.SafeDumper.add_representer(
            str, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)
        )
        print(yaml.safe_dump(document, sort_keys=False))


if __name__ == "__main__":
    main()
