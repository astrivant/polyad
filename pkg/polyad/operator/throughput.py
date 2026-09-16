"""
Soul searching: relate application demand to Cheeger targets and bounded topology changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from polyad.graph.rules import graph_cheeger, relation_graph
from polyad.graph.temporary import active_entries
from polyad.operator.rule_state import check_live_rules
from polyad.operator.rules import RuleViolation
from polyad_types.codec import converter, to_dict
from polyad_types.resources import BOUNDARY_KINDS, GROUP
from polyad_types.throughput import ThroughputSample
from polyad_types.topology import topology

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.api import API
    from polyad.operator.controller import Controller
    from polyad_types.rules import Cheeger
    from polyad_types.topology import Topology

SAMPLE = f"{GROUP}/throughput-sample"
STATE = f"{GROUP}/throughput-state"


def within(value: float, bounds: Cheeger) -> bool:
    """
    Compare exact edge expansion with a separate inclusive target range.

    Args:
        value (float): Measured unnormalized Cheeger constant.
        bounds (Cheeger): Application target independent of GraphRules.

    Returns:
        bool: Whether both configured bounds hold within numerical tolerance.
    """
    return (bounds.minimum is None or value + 1e-9 >= bounds.minimum) and (bounds.maximum is None or value - 1e-9 <= bounds.maximum)


async def expansion(graph: Topology) -> float:
    """
    Keep exact cut enumeration off the operator's event loop.

    Args:
        graph (Topology): Current or proposed boundary topology.

    Returns:
        float: Exact Cheeger constant of its connections projection.
    """
    return await asyncio.to_thread(graph_cheeger, relation_graph(graph, "connections"))


async def capacity_revision(api: API, obj: dict[str, Any]) -> str:
    """
    Detect local execution and nested topology changes without depending on heartbeat revisions.

    Args:
        api (API): Fresh family reader.
        obj (dict[str, Any]): Root of the measured boundary's local execution subtree.

    Returns:
        str: Stable identity for execution membership, requested capacity and node eligibility.
    """
    pending = [obj]
    identities = []
    visited: set[str] = set()
    while pending:
        parent = pending.pop()
        uid = parent["metadata"]["uid"]
        if uid in visited or len(visited) >= 256:
            raise ValueError("throughput inventory exceeds its boundary traversal budget")
        visited.add(uid)
        for child in await api.owned(parent["metadata"]["namespace"], uid):
            if child["kind"] not in BOUNDARY_KINDS | {"Job", "Deployment", "StatefulSet", "DaemonSet"}:
                continue
            meta = child["metadata"]
            identities.append(
                (
                    child["kind"],
                    meta["uid"],
                    meta.get("generation", 1),
                    bool(meta.get("deletionTimestamp")),
                    child.get("spec", {}).get("replicas", 0),
                    child.get("status", {}).get("desiredNumberScheduled", 0),
                )
            )
            if len(identities) > 4096:
                raise ValueError("throughput inventory exceeds 4096 execution objects")
            if child["kind"] in BOUNDARY_KINDS:
                pending.append(child)
    return hashlib.sha256(json.dumps(sorted(identities)).encode()).hexdigest()


async def reconcile_throughput(controller: Controller, obj: dict[str, Any], *, now: datetime | None = None) -> bool:
    """
    Consume fresh samples and atomically commit an approved layout with its change budget.

    Args:
        controller (Controller): Existing family-leased execution adapter.
        obj (dict[str, Any]): Fresh Graph or PolyGraph document.
        now (datetime | None): Clock override for deterministic reconciliation tests.

    Returns:
        bool: Whether topology changed and the caller must refresh before other actions.
    """
    graph = topology(obj["spec"], obj["kind"])
    policy = graph.throughput
    if policy is None or graph.suspend or graph.templateOnly or obj.get("status", {}).get("phase") in {"Stopped", "Completed"}:
        return False
    started = time.monotonic()
    now = now or datetime.now(UTC)
    clock = now.timestamp()
    meta = obj["metadata"]
    annotations = meta.get("annotations", {})
    previous = json.loads(annotations.get(STATE, "{}"))
    state = dict(previous)
    fingerprint = hashlib.sha256(json.dumps(to_dict(policy), sort_keys=True).encode()).hexdigest()
    # Keep rolling change budgets even when the policy or topology is edited.
    changes = [stamp for stamp in state.get("changes", []) if clock - stamp < 3600]
    if state.get("policy") != fingerprint or state.get("generation") != meta["generation"]:
        state = {"changes": changes, "lastChange": state.get("lastChange", 0)}
    state.update(policy=fingerprint, generation=meta["generation"], changes=changes)
    capacity = await capacity_revision(controller.api, obj)
    if previous.get("capacity") and previous["capacity"] != capacity:
        state.update(since=0, count=0, settledAfter=clock)
    state["capacity"] = capacity
    status: dict[str, Any] = {
        "mode": policy.mode,
        "phase": "WaitingForSample",
        "currentCheeger": await expansion(graph),
        "observedAt": None,
        "target": None,
        "recommendedLayout": None,
        "proposedCheeger": None,
        "offeredPerSecond": None,
        "completedPerSecond": None,
    }
    raw = json.loads(annotations.get(SAMPLE, "null"))
    candidate = None
    if raw:
        sample = converter.structure(raw, ThroughputSample)
        observed = datetime.fromisoformat(sample.observedAt.replace("Z", "+00:00")).timestamp()
        valid = (
            sample.graphUid == meta["uid"]
            and sample.generation == meta["generation"]
            and sample.unit == policy.unit
            and 0 <= clock - observed <= policy.sampleMaxAgeSeconds
            and observed >= state.get("settledAfter", 0)
        )
        if valid:
            status.update(
                observedAt=sample.observedAt, offeredPerSecond=sample.offeredPerSecond, completedPerSecond=sample.completedPerSecond
            )
            tier = next((item for item in reversed(policy.tiers) if sample.offeredPerSecond >= item.offeredPerSecond), None)
            target = to_dict(tier.cheeger) if tier else None
            status["target"] = target
            shortfall = (
                tier is not None
                and sample.offeredPerSecond > 0
                and sample.completedPerSecond < sample.offeredPerSecond * policy.shortfallRatio
            )
            if not shortfall:
                state.update(since=0, count=0, target=target, observed=observed)
                status["phase"] = "Satisfied" if tier else "BelowDemandThreshold"
            else:
                if (
                    state.get("target") != target
                    or observed - state.get("observed", 0) > policy.sampleMaxAgeSeconds
                    or not state.get("since")
                ):
                    state.update(since=observed, count=0)
                if observed > state.get("observed", 0):
                    state["count"] = min(state.get("count", 0) + 1, policy.minSamples)
                state.update(target=target, observed=observed)
                assert tier is not None
                status["phase"] = "Stabilizing"
                if observed - state["since"] >= policy.sustainedSeconds and state["count"] >= policy.minSamples:
                    status["phase"] = "ThroughputShortfall" if within(status["currentCheeger"], tier.cheeger) else "NoAllowedLayout"
                    if not within(status["currentCheeger"], tier.cheeger):
                        for layout in policy.layouts:
                            proposal = {**obj["spec"], "connections": converter.unstructure(layout.connections)}
                            value = await expansion(topology(proposal, obj["kind"]))
                            if not within(value, tier.cheeger):
                                continue
                            try:
                                await check_live_rules(controller.api, obj, candidate=proposal, candidate_is_logical=True)
                            except RuleViolation:
                                continue
                            status.update(phase="Recommended", recommendedLayout=layout.name, proposedCheeger=value)
                            candidate = proposal
                            break
                    if candidate and policy.mode == "Adapt":
                        if active_entries(obj):
                            status["phase"] = "TemporaryConnectionsActive"
                        elif clock - state.get("lastChange", 0) < policy.cooldownSeconds or len(changes) >= policy.maxChangesPerHour:
                            status["phase"] = "CoolingDown"
                        else:
                            status["phase"] = "Applied"
        else:
            status["phase"] = "StaleSample"
            state.update(since=0, count=0)
    state["decision"] = status
    changed = bool(status["phase"] == "Applied")
    if changed:
        # Recheck the whole live family immediately before the fenced write.
        await check_live_rules(controller.api, obj, candidate=candidate, candidate_is_logical=True)
        if await capacity_revision(controller.api, obj) != capacity:
            return False
        if clock - observed + time.monotonic() - started > policy.sampleMaxAgeSeconds:
            return False
        state.update(changes=[*changes, clock], lastChange=clock, since=0, count=0, generation=meta["generation"] + 1)
    if state != previous:
        body: dict[str, Any] = {
            "metadata": {"resourceVersion": meta["resourceVersion"], "annotations": {STATE: json.dumps(state, allow_nan=False)}}
        }
        if changed:
            assert candidate is not None
            body["spec"] = {"connections": candidate["connections"]}
        obj = await controller.api.request("PATCH", obj["kind"], meta["namespace"], meta["name"], body)
    await controller.status(obj, {"throughput": status})
    return changed
