"""
Group complete namespace scans by controller ownership without duplicating subtree counts.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from polyad.compiler.asts import GROUP
from polyad.operator.coordination import root_shard
from polyad.operator.rollup import PHASES

if TYPE_CHECKING:
    from typing import Any


def inventory(objects: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Build identity-only hierarchy records and generation-fenced status observations.

    Args:
        objects (list[dict[str, Any]]): Objects from a complete namespace rescan.

    Returns:
        dict[str, Any]: Counts and hierarchy records without workload manifests or arbitrary labels.
    """
    indexed = {(obj["kind"], obj["metadata"]["name"]): obj for obj in objects}
    records = []
    definitions = {"Workload", "Daemon", "Ephemeral", "Resource", "Gate", "ShutdownPolicy", "GraphRule"}
    counts: Counter[tuple[str, str]] = Counter()
    for obj in objects:
        meta, status = obj["metadata"], obj.get("status", {})
        role = "definition" if (obj["kind"] in definitions or obj.get("spec", {}).get("templateOnly", False)) else "instance"
        counts[obj["kind"], role] += 1
        current = obj
        seen: set[str] = set()
        depth, complete = 0, True
        parent = None
        while True:
            current_meta = current["metadata"]
            if current_meta["uid"] in seen or depth >= 64:
                complete = False
                break
            seen.add(current_meta["uid"])
            owners = [
                ref
                for ref in current_meta.get("ownerReferences", [])
                if ref.get("controller") and ref.get("apiVersion", "").startswith(f"{GROUP}/")
            ]
            if not owners:
                break
            owner = owners[0]
            if parent is None:
                parent = {key: owner[key] for key in ("kind", "name", "uid")}
            ancestor = indexed.get((owner["kind"], owner["name"]))
            if ancestor is None or ancestor["metadata"]["uid"] != owner["uid"]:
                complete = False
                break
            current = ancestor
            depth += 1
        root = {"kind": current["kind"], "name": current["metadata"]["name"], "uid": current["metadata"]["uid"]} if complete else None
        generation = meta.get("generation", 1)
        metrics = status.get("metrics") or {}
        observed = status.get("observedGeneration") == generation and metrics.get("observedGeneration") == generation
        phase = status.get("phase", "Unknown") if status.get("observedGeneration") == generation else "Unknown"
        duty_root = root
        if obj["kind"] == "Rewrite":
            target = indexed.get((obj["spec"].get("kind", "Graph"), obj["spec"]["graph"]))
            # Resolve the target's controller chain separately from the Rewrite's own ownership.
            target_seen: set[str] = set()
            while target is not None:
                target_meta = target["metadata"]
                if target_meta["uid"] in target_seen:
                    duty_root = None
                    break
                target_seen.add(target_meta["uid"])
                duty_root = {"kind": target["kind"], "name": target_meta["name"]}
                refs = [
                    ref
                    for ref in target_meta.get("ownerReferences", [])
                    if ref.get("controller") and ref.get("apiVersion", "").startswith(f"{GROUP}/")
                ]
                if not refs:
                    break
                ref = refs[0]
                target = indexed.get((ref["kind"], ref["name"]))
                if target is None or target["metadata"]["uid"] != ref["uid"]:
                    duty_root = None
                    break
            if not target_seen:
                duty_root = None
        uses = []
        if role == "instance":
            uses = [{"kind": node["kind"], "name": node["ref"]} for node in obj.get("spec", {}).get("nodes", [])]
            if obj["kind"] == "ReplicaGroup":
                template = obj["spec"]["template"]
                uses = [{"kind": template["kind"], "name": template["ref"]}]
        records.append(
            {
                "kind": obj["kind"],
                "name": meta["name"],
                "uid": meta["uid"],
                "generation": generation,
                "role": role,
                "parent": parent,
                "root": root,
                "depth": depth if complete else None,
                "hierarchyComplete": complete,
                "shard": root_shard(duty_root["kind"], meta["namespace"], duty_root["name"])
                if duty_root and obj["kind"] not in definitions
                else None,
                "phase": phase if phase in PHASES else "Unknown",
                "terminating": bool(meta.get("deletionTimestamp")),
                "statusCurrent": observed,
                "resources": metrics.get("resources") if observed else None,
                "execution": metrics.get("execution") if observed else None,
                "topology": metrics.get("topology") if observed else None,
                "rollup": metrics.get("rollup") if observed else None,
                "uses": uses,
                "workloads": {name: entry for name, entry in (status.get("workloads") or {}).items() if entry is not None},
                "workloadsObservedAt": status.get("workloadsObservedAt"),
                "metricsObservedAt": status.get("metricsObservedAt"),
                "boundarySignals": {
                    key: value
                    for part in (metrics.get("execution"), metrics.get("rollup"))
                    for key, value in (part or {}).items()
                    if type(value) in (int, float)
                }
                if observed
                else {},
                "scaling": {
                    **{
                        key: status.get(key)
                        for key in ("replicas", "desiredReplicas", "readyReplicas", "totalReplicas", "instanceCount", "sourceGeneration")
                    },
                    "source": obj["spec"].get("replicaSource") if obj["spec"].get("inheritReplicas", True) else None,
                    "current": status.get("scaleCurrent", False) and status.get("observedGeneration") == generation,
                    "observedAt": status.get("scaleObservedAt"),
                }
                if obj["kind"] == "ReplicaGroup"
                else None,
            }
        )
    return {
        "total": len(objects),
        "byKind": [{"kind": kind, "role": role, "count": count} for (kind, role), count in sorted(counts.items())],
        "objects": sorted(records, key=lambda record: (record["kind"], record["name"])),
    }
