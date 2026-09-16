"""
Reconcile replica groups through the same leased graph scheduler as other boundaries.
"""

from __future__ import annotations

import copy
import hashlib
from typing import TYPE_CHECKING

from polyad.compiler.asts import AUXILIARY_KINDS, GROUP
from polyad.graph.replication import Replication, replica_topology
from polyad.graph.topology import converter
from polyad.metrics.workloads import current_observation, observation_time
from polyad.operator.graph_status import observed

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.controller import Controller


def replica_selector(uid: str) -> str:
    """
    Return the label key inherited by every pod belonging to this group incarnation.

    Args:
        uid (str): Group UID.

    Returns:
        str: Label key usable by the Kubernetes scale selector.
    """
    return f"{GROUP}/replicas-{hashlib.sha256(uid.encode()).hexdigest()[:16]}"


async def reconcile_group(controller: Controller, obj: dict[str, Any]) -> None:
    """
    Resolve shared replica intent and publish scale observations after ordered reconciliation.

    Args:
        controller (Controller): Controller holding the group's root-family lease.
        obj (dict[str, Any]): Fresh ReplicaGroup document.

    Returns:
        None: Child writes and status updates complete before the queue advances.
    """
    from polyad.operator.controller import Pending

    meta = obj["metadata"]
    policy = converter.structure(obj["spec"], Replication)
    if policy.templateOnly:
        instances = (await controller.api.request("GET", "ReplicaGroup", meta["namespace"])).get("items", [])
        instances = [
            item
            for item in instances
            if not item["spec"].get("templateOnly")
            and item["spec"].get("inheritReplicas", True)
            and (item["spec"].get("replicaSource") or {}).get("uid") == meta["uid"]
            and not item["metadata"].get("deletionTimestamp")
        ]
        current = all(
            item.get("status", {}).get("scaleCurrent", False)
            and item.get("status", {}).get("sourceGeneration") == meta["generation"]
            and item.get("status", {}).get("observedGeneration") == item["metadata"]["generation"]
            and current_observation(item.get("status", {}).get("scaleObservedAt"))
            for item in instances
        )
        counts = [item.get("status", {}).get("replicas", 0) for item in instances]
        await controller.status(
            obj,
            {
                "observedGeneration": meta["generation"],
                "replicas": max(counts, default=0),
                "desiredReplicas": policy.replicas,
                "labelSelector": f"{replica_selector(meta['uid'])}=true",
                "scaleObservedAt": observation_time(obj.get("status", {}).get("scaleObservedAt")),
                "scaleCurrent": current,
                "instanceCount": len(instances),
                "totalReplicas": sum(counts),
                "readyReplicas": sum(item.get("status", {}).get("readyReplicas", 0) for item in instances),
            },
        )
        return
    effective = copy.deepcopy(obj)
    source_generation = None
    if policy.replicaSource and policy.inheritReplicas:
        source = await controller.api.get("ReplicaGroup", meta["namespace"], policy.replicaSource.name)
        if not source or source["metadata"]["uid"] != policy.replicaSource.uid or source["metadata"].get("deletionTimestamp"):
            raise Pending("replica source incarnation is unavailable")
        if not source["spec"].get("templateOnly"):
            raise ValueError("replicaSource must refer to a reusable group definition")
        count = converter.structure(source["spec"], Replication).replicas
        if not policy.minReplicas <= count <= policy.maxReplicas:
            raise ValueError("inherited replicas exceed this instance's bounds")
        effective["spec"]["replicas"] = count
        source_generation = source["metadata"]["generation"]
    count = effective["spec"].get("replicas", policy.replicas)
    effective["spec"] = replica_topology(effective["spec"])
    reconciled = False
    try:
        await controller.graph(effective)
        reconciled = True
    finally:
        # A cached replica count is never a substitute for observing terminating children.
        children = [item for item in await controller.api.owned(meta["namespace"], meta["uid"]) if item["kind"] not in AUXILIARY_KINDS]
        latest = await controller.api.get("ReplicaGroup", meta["namespace"], meta["name"])
        if latest and latest["metadata"]["uid"] == meta["uid"] and latest["metadata"]["generation"] == meta["generation"]:
            await controller.status(
                latest,
                {
                    "replicas": len(children),
                    "desiredReplicas": count,
                    "readyReplicas": sum(observed(item)["ready"] or observed(item)["completed"] for item in children),
                    "labelSelector": f"{replica_selector(meta['uid'])}=true",
                    "sourceGeneration": source_generation,
                    "scaleObservedAt": observation_time(obj.get("status", {}).get("scaleObservedAt")),
                    "scaleCurrent": reconciled,
                    "instanceCount": 1,
                    "totalReplicas": len(children),
                },
            )
