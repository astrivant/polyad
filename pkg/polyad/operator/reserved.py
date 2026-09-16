"""
Observe externally managed operator Deployments inside the reserved graph hierarchy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad.events.visibility import INTERNAL
from polyad.operator.graph_status import observed
from polyad.operator.rule_state import check_live_rules
from polyad_types.resources import GROUP
from polyad_types.topology import topology

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.api import API
    from polyad.operator.controller import Controller

DEPLOYMENT = f"{GROUP}/observed-operator-deployment"


async def members(api: API, obj: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Read a reserved group's Deployment without acquiring lifecycle ownership.

    Args:
        api (API): Reader for the group's cluster and namespace.
        obj (dict[str, Any]): Reserved Graph with its operator Deployment binding.

    Returns:
        list[dict[str, Any]]: Observation copies labeled for graph metrics, never for mutation.
    """
    meta = obj["metadata"]
    name = meta.get("annotations", {}).get(DEPLOYMENT)
    if not name:
        return []
    graph = topology(obj["spec"], obj["kind"])
    if (
        obj["kind"] != "Graph"
        or meta.get("labels", {}).get(INTERNAL) != "true"
        or len(graph.nodes) != 1
        or graph.nodes[0].kind != "Daemon"
        or graph.mode != "persistent"
        or graph.throughput is not None
    ):
        raise ValueError("operator Deployment observations require an internal persistent single-Daemon Graph")
    deployment = await api.get("Deployment", meta["namespace"], name)
    if deployment is None:
        return []
    if deployment["metadata"].get("labels", {}).get(INTERNAL) != "true":
        raise ValueError("reserved operator Graph cannot observe a non-internal Deployment")
    # Only the returned observation is decorated. The native Deployment retains
    # Helm or OperatorPool ownership and is never adopted or deleted by this Graph.
    deployment["metadata"]["labels"] = {
        **deployment["metadata"].get("labels", {}),
        f"{GROUP}/node": graph.nodes[0].name,
    }
    return [deployment]


async def reconcile(controller: Controller, obj: dict[str, Any]) -> None:
    """
    Publish fresh operator group readiness while preserving its bootstrap owner.

    Args:
        controller (Controller): Existing guarded graph controller.
        obj (dict[str, Any]): Reserved observation Graph instance.

    Returns:
        None: Only the Graph's status is changed.
    """
    children = await members(controller.api, obj)
    reports = await check_live_rules(controller.api, obj)
    node = obj["spec"]["nodes"][0]
    suspended = obj["spec"].get("suspend", False)
    states = {node["name"]: observed(children[0])} if children and not suspended else {}
    state = states.get(node["name"], {})
    native = children[0].get("status", {}) if children else {}
    await controller.status(
        obj,
        {
            "phase": "Suspended" if suspended else "Failed" if state.get("failed") else "Ready" if state.get("ready") else "Waiting",
            "ready": state.get("ready", False),
            "completed": False,
            "failed": state.get("failed", False),
            "observedGeneration": obj["metadata"].get("generation", 1),
            "nodes": states,
            "structuralRules": reports,
            "workloads": {
                node["name"]: {
                    "kind": "Daemon",
                    "definition": node["ref"],
                    "values": {
                        "executions": len(children),
                        "readyExecutions": int(state.get("ready", False)),
                        "replicas": native.get("replicas", 0),
                        "readyReplicas": native.get("readyReplicas", 0),
                    },
                }
            },
        },
    )
