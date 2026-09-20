"""
Describe graph neighbors and observed execution membership without workload payloads.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from cattrs.errors import CattrsError

from polyad.graph.temporary import overlay
from polyad.operator.reconciliation.replication import effective_spec
from polyad_types.graphs.topology import topology
from polyad_types.resources import AUXILIARY_KINDS, GROUP

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API

__all__ = (
    "neighbors",
    "topology_snapshot",
)


async def topology_snapshot(api: API, obj: dict[str, Any], children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """
    Read desired connections and actual children independently of lifecycle status updates.

    Args:
        api (API): Fresh Kubernetes reader under graph-family ownership.
        obj (dict[str, Any]): Persisted graph boundary whose neighbors are being observed.
        children (list[dict[str, Any]] | None): Optional freshly verified local and remote child inventory.

    Returns:
        dict[str, Any]: Canonical, revisioned snapshot with no Pod templates or credentials.
    """
    from polyad.operator.reconciliation.controller import Pending

    meta = obj["metadata"]
    spec = obj["spec"]
    nodes: dict[str, dict[str, Any]] = {}
    connections = []
    valid = True
    try:
        if obj["kind"] == "ReplicaGroup":
            spec, _ = await effective_spec(api, obj)
        graph = topology(overlay(obj, spec), obj["kind"])
        for vertex in graph.nodes:
            cluster = getattr(vertex, "cluster", None)
            nodes[vertex.name] = {
                "name": vertex.name,
                "kind": vertex.kind,
                "ref": vertex.ref,
                **({"cluster": cluster} if cluster else {}),
                "desired": True,
                "requires": sorted(
                    [{"node": edge.node, "condition": edge.condition} for edge in vertex.requires],
                    key=lambda edge: (edge["node"], edge["condition"]),
                ),
                "executions": [],
            }
        connections = [
            {
                "source": edge.source,
                "target": edge.target,
                "ports": [
                    {"port": port.port, "protocol": port.protocol}
                    for port in sorted(set(edge.ports), key=lambda port: (port.protocol, port.port))
                ],
            }
            for edge in graph.connections
        ]
    except (ValueError, TypeError, KeyError, CattrsError, Pending):
        # Invalid intent must invalidate a cached neighbor declaration, while
        # still reporting the execution resources that actually remain present.
        valid = False
    for child in children if children is not None else await api.owned(meta["namespace"], meta["uid"]):
        if child["kind"] in AUXILIARY_KINDS:
            continue
        child_meta = child["metadata"]
        labels = child_meta.get("labels", {})
        name = labels.get(f"{GROUP}/node")
        if not name:
            continue
        node = nodes.setdefault(name, {"name": name, "desired": False, "requires": [], "executions": []})
        execution = {
            "kind": child["kind"],
            "name": child_meta["name"],
            "uid": child_meta["uid"],
            "runtimeNode": labels.get(f"{GROUP}/runtime-node", name),
            "terminating": bool(child_meta.get("deletionTimestamp")),
        }
        if cluster := child_meta.get("annotations", {}).get(f"{GROUP}/remote-cluster"):
            execution.update(cluster=cluster, namespace=child_meta["namespace"])
        if child["kind"] == "DaemonSet":
            execution["replicas"] = child.get("status", {}).get("desiredNumberScheduled", 0)
        elif child["kind"] in {"Deployment", "StatefulSet"}:
            execution["replicas"] = child.get("spec", {}).get("replicas", 1)
        node["executions"].append(execution)
    for node in nodes.values():
        node["executions"].sort(key=lambda child: (child["kind"], child["name"], child["uid"]))

    # Canonicalization prevents reordered declarations and heartbeat revisions
    # from producing structural changes. Ports retain their transport meaning.
    edges = {json.dumps(edge, sort_keys=True, separators=(",", ":")) for edge in connections}
    content = {
        "valid": valid,
        "templateOnly": bool(spec.get("templateOnly")),
        "terminating": bool(meta.get("deletionTimestamp")),
        "nodes": [nodes[name] for name in sorted(nodes)],
        "connections": [json.loads(edge) for edge in sorted(edges)],
    }
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > 4 * 1024 * 1024 - 4096:
        content = {**content, "valid": False, "error": "topology snapshot exceeds 4 MiB", "nodes": [], "connections": []}
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return {
        "graph": {"kind": obj["kind"], **{key: meta[key] for key in ("namespace", "name", "uid")}},
        "revision": hashlib.sha256((meta["uid"] + "\0" + encoded).encode()).hexdigest(),
        **content,
    }


def neighbors(snapshot: dict[str, Any], name: str) -> dict[str, Any]:
    """
    Select one logical node and its directed data-flow neighbors and dependencies.

    Args:
        snapshot (dict[str, Any]): Complete boundary snapshot.
        name (str): Logical node name, including a stable replica ordinal.

    Returns:
        dict[str, Any]: Node plus incoming, outgoing, dependency and dependent descriptions.
    """
    nodes = {node["name"]: node for node in snapshot["nodes"]}
    if name not in nodes:
        raise KeyError(name)
    selected = nodes[name]
    return {
        **{key: value for key, value in snapshot.items() if key not in {"nodes", "connections"}},
        "node": selected,
        "incoming": [{"node": nodes[edge["source"]], "ports": edge["ports"]} for edge in snapshot["connections"] if edge["target"] == name],
        "outgoing": [{"node": nodes[edge["target"]], "ports": edge["ports"]} for edge in snapshot["connections"] if edge["source"] == name],
        "dependencies": [{"node": nodes[edge["node"]], "condition": edge["condition"]} for edge in selected["requires"]],
        "dependents": [
            {"node": node, "condition": edge["condition"]} for node in nodes.values() for edge in node["requires"] if edge["node"] == name
        ],
    }
