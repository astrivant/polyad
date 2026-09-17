"""
Place Helm-declared local services in the reserved operator hierarchy.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING

from polyad.events.visibility import INTERNAL
from polyad.operator.clusters.reserved import RESOURCES, validate_bindings
from polyad_types.resources import GROUP

if TYPE_CHECKING:
    from polyad.operator.clusters.pools import PoolManager


async def graphs(manager: PoolManager, name: str, owner: str) -> list[dict[str, str]]:
    """
    Declare observation branches from the chart's rendered service inventory.

    Args:
        manager (PoolManager): Root resource writer with ownership and version fences.
        name (str): Reserved root PolyGraph name.
        owner (str): Root deployment identity allowed to maintain these definitions.

    Returns:
        list[dict[str, str]]: Local group nodes referencing internal observation Graphs.
    """
    inventory = json.loads(os.environ.get("POLYAD_LOCAL_SERVICES", "{}"))
    if not isinstance(inventory, dict) or inventory.keys() - {"endpoints", "keda", "dragonfly", "postgresql", "mesh", "observer"}:
        raise ValueError("unsupported local service inventory group")
    nodes = []
    for group, targets in sorted(inventory.items()):
        if not isinstance(targets, list) or not 1 <= len(targets) <= 256:
            raise ValueError("local service groups require between one and 256 targets")
        bindings = {}
        for target in targets:
            validate_bindings({"target": target})
            identity = json.dumps(target, sort_keys=True)
            label = f"{target['kind'].lower()}-{target['name'].replace('.', '-')[:32]}"
            key = f"{label}-{hashlib.sha256(identity.encode()).hexdigest()[:8]}"
            bindings[key] = target
        reference = f"{name}-root-{group}"
        await manager.apply(
            manager.api,
            {
                "apiVersion": f"{GROUP}/v1alpha1",
                "kind": "Graph",
                "metadata": {
                    "name": reference,
                    "namespace": manager.namespace,
                    "labels": {INTERNAL: "true"},
                    "annotations": {RESOURCES: json.dumps(bindings, sort_keys=True)},
                },
                "spec": {
                    "templateOnly": True,
                    "mode": "persistent",
                    "nodes": [{"name": key, "kind": "Resource", "ref": target["name"]} for key, target in sorted(bindings.items())],
                },
            },
            owner,
        )
        nodes.append({"name": group, "kind": "Graph", "ref": reference})
    return nodes
