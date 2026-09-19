"""
Persist SDK strategy lifecycle where GitOps health checks can observe it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from polyad.api.http.errors import Conflict, Forbidden
from polyad.events.visibility import observation_ancestry, permitted_observation, public_observation
from polyad_types.resources import ObjectMeta, StatusPatch

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad_types import AdaptationReport, GraphAccess


async def report_adaptation(api: API, namespace: str, report: AdaptationReport, grants: tuple[GraphAccess, ...] | None) -> dict[str, Any]:
    """
    Merge one graph-authorized, generation-fenced transition into definition status.

    Args:
        api (API): Ordered Kubernetes intake adapter.
        namespace (str): Fixed API listener namespace.
        report (AdaptationReport): Fenced definition and invocation transition.
        grants (tuple[GraphAccess, ...] | None): Named graph grants, or legacy namespace access.

    Returns:
        dict[str, Any]: Current definition generation and adaptation status.
    """
    graph = await api.get(report.graphKind, namespace, report.graph)
    reserved_name = os.environ.get("POLYAD_SELF_GRAPH", "")
    reserved = (os.environ.get("POLYAD_NAMESPACE", namespace), reserved_name) if reserved_name else None
    if graph is None or not await public_observation(api, graph, reserved_graph=reserved):
        raise Forbidden("adaptation target is unavailable")
    identity = {"kind": graph["kind"], **graph["metadata"]}
    if grants is not None and not permitted_observation(identity, await observation_ancestry(api, graph), grants):
        raise Forbidden("credential does not authorize this graph tree")
    if graph["metadata"]["uid"] != report.graphUid or graph["metadata"].get("deletionTimestamp"):
        raise Conflict("adaptation graph incarnation changed")
    obj = await api.get(report.targetKind, namespace, report.target)
    if obj is None or not await public_observation(api, obj, reserved_graph=reserved):
        raise Forbidden("adaptation definition is unavailable")
    meta = obj["metadata"]
    if meta["uid"] != report.targetUid or meta["generation"] != report.targetGeneration or meta.get("deletionTimestamp"):
        raise Conflict("adaptation definition incarnation or generation changed")
    observed = datetime.fromisoformat(report.observedAt.replace("Z", "+00:00"))
    if abs((datetime.now(UTC) - observed).total_seconds()) > 60:
        raise ValueError("adaptation report is stale or from the future")

    current = obj.get("status", {}).get("adaptation", {})
    invocations = dict(current.get("invocations", {})) if current.get("observedGeneration") == meta["generation"] else {}
    statistics = (
        dict(current.get("statistics", {}))
        if current.get("observedGeneration") == meta["generation"]
        else {"attempts": 0, "succeeded": 0, "failed": 0, "durationSeconds": 0.0}
    )
    previous = invocations.get(report.invocationId)
    if report.phase == "Running":
        value = {"node": report.node, "strategy": report.strategy, "startedAt": report.observedAt}
        if previous is not None and (previous.get("node"), previous.get("strategy")) != (report.node, report.strategy):
            raise Conflict("adaptation invocation identity was already used")
        if previous is None:
            invocations[report.invocationId] = value
            statistics["attempts"] = statistics.get("attempts", 0) + 1
    else:
        if previous is None:
            raise Conflict("adaptation invocation is not active")
        if (previous.get("node"), previous.get("strategy")) != (report.node, report.strategy):
            raise Conflict("adaptation invocation identity does not match")
        started = datetime.fromisoformat(previous["startedAt"].replace("Z", "+00:00"))
        statistics["durationSeconds"] = statistics.get("durationSeconds", 0.0) + max(0.0, (observed - started).total_seconds())
        statistics["succeeded" if report.phase == "Succeeded" else "failed"] = (
            statistics.get("succeeded" if report.phase == "Succeeded" else "failed", 0) + 1
        )
        invocations.pop(report.invocationId)
    if len(invocations) > 64:
        raise ValueError("at most 64 adaptation invocations may be active")
    adaptation = {
        "observedGeneration": meta["generation"],
        "inProgress": bool(invocations),
        "invocations": invocations,
        "statistics": statistics,
        "lastTransition": {
            "invocationId": report.invocationId,
            "node": report.node,
            "strategy": report.strategy,
            "phase": report.phase,
            "observedAt": report.observedAt,
        },
    }
    await api.request(
        "PATCH",
        report.targetKind,
        namespace,
        report.target,
        StatusPatch(
            metadata=ObjectMeta(resourceVersion=meta["resourceVersion"]),
            status={"observedGeneration": meta["generation"], "progressing": bool(invocations), "adaptation": adaptation},
        ),
        status=True,
    )
    return {"targetUid": report.targetUid, "generation": meta["generation"], "progressing": bool(invocations), "adaptation": adaptation}
