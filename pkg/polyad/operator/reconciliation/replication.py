"""
Reconcile replica groups through the same leased graph scheduler as other boundaries.
"""

from __future__ import annotations

import copy
import hashlib
from typing import TYPE_CHECKING

from polyad.exceptions.reconciliation import Pending
from polyad.metrics.workloads import current_observation, observation_time
from polyad.operator.clusters.remote_scaling import INTENT, approved_intent, remote_revision
from polyad.operator.observability.graph_status import observed
from polyad_types.graphs.replication import Replication, replica_topology
from polyad_types.resources import AUXILIARY_KINDS, GROUP
from polyad_types.serialization import converter

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad.operator.reconciliation.controller import Controller

__all__ = (
    "effective_spec",
    "reconcile_group",
    "replica_selector",
)


def replica_selector(uid: str) -> str:
    """
    Return the label key inherited by every pod belonging to this group incarnation.

    Args:
        uid (str): Group UID.

    Returns:
        str: Label key usable by the Kubernetes scale selector.
    """
    return f"{GROUP}/replicas-{hashlib.sha256(uid.encode()).hexdigest()[:16]}"


async def effective_spec(api: API, obj: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
    """
    Resolve the live inherited count for both structural and network projections.

    Args:
        api (API): Kubernetes read adapter.
        obj (dict[str, Any]): Persisted ReplicaGroup instance.

    Returns:
        tuple[dict[str, Any], int | None]: Effective replication specification and source generation.
    """

    spec = copy.deepcopy(obj["spec"])
    policy = converter.structure(spec, Replication)
    generation = None
    intent = approved_intent(obj)
    if intent:
        from polyad.events.visibility import public_observation

        if not await public_observation(api, obj):
            raise ValueError("remote scaling cannot control reserved operator graphs or unresolved ancestry")
        spec["replicas"] = intent["replicas"]
    if policy.replicaSource and policy.inheritReplicas:
        source = await api.get("ReplicaGroup", obj["metadata"]["namespace"], policy.replicaSource.name)
        if not source or source["metadata"]["uid"] != policy.replicaSource.uid or source["metadata"].get("deletionTimestamp"):
            raise Pending("replica source incarnation is unavailable")
        if not source["spec"].get("templateOnly"):
            raise ValueError("replicaSource must refer to a reusable group definition")
        if source["spec"].get("replicaSource"):
            raise ValueError("replica sources cannot themselves inherit another source")
        source_spec, _ = await effective_spec(api, source)
        count = converter.structure(source_spec, Replication).replicas
        if not policy.minReplicas <= count <= policy.maxReplicas:
            raise ValueError("inherited replicas exceed this instance's bounds")
        spec["replicas"] = count
        generation = source["metadata"]["generation"]
    return spec, generation


async def reconcile_group(controller: Controller, obj: dict[str, Any]) -> None:
    """
    Resolve shared replica intent and publish scale observations after ordered reconciliation.

    Args:
        controller (Controller): Controller holding the group's root-family lease.
        obj (dict[str, Any]): Fresh ReplicaGroup document.

    Returns:
        None: Child writes and status updates complete before the queue advances.
    """
    meta = obj["metadata"]
    policy = converter.structure(obj["spec"], Replication)
    revision = remote_revision(obj)

    # CEL health checks can compare the annotation directly without reproducing its hash.
    observed_intent = meta.get("annotations", {}).get(INTENT, "")
    source_revision = ""
    if policy.replicaSource and policy.inheritReplicas:
        source = await controller.api.get("ReplicaGroup", meta["namespace"], policy.replicaSource.name)
        source_revision = remote_revision(source) if source else ""
    resolved, source_generation = await effective_spec(controller.api, obj)
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
            and item.get("status", {}).get("sourceRemoteScaleRevision", "") == revision
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
                "desiredReplicas": resolved.get("replicas", policy.replicas),
                "remoteScaleRevision": revision,
                "observedRemoteScaleIntent": observed_intent,
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
    effective["spec"] = resolved
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
        source_current = True
        if policy.replicaSource and policy.inheritReplicas:
            latest_source = await controller.api.get("ReplicaGroup", meta["namespace"], policy.replicaSource.name)
            source_current = bool(
                latest_source
                and latest_source["metadata"]["uid"] == policy.replicaSource.uid
                and latest_source["metadata"]["generation"] == source_generation
                and remote_revision(latest_source) == source_revision
            )
        if (
            latest
            and latest["metadata"]["uid"] == meta["uid"]
            and latest["metadata"]["generation"] == meta["generation"]
            and remote_revision(latest) == revision
        ):
            await controller.status(
                latest,
                {
                    "replicas": len(children),
                    "desiredReplicas": count,
                    "readyReplicas": sum(observed(item)["ready"] or observed(item)["completed"] for item in children),
                    "labelSelector": f"{replica_selector(meta['uid'])}=true",
                    "sourceGeneration": source_generation,
                    "sourceRemoteScaleRevision": source_revision,
                    "remoteScaleRevision": revision,
                    "observedRemoteScaleIntent": observed_intent,
                    "scaleObservedAt": observation_time(obj.get("status", {}).get("scaleObservedAt")),
                    "scaleCurrent": reconciled and source_current,
                    "instanceCount": 1,
                    "totalReplicas": len(children),
                },
            )
