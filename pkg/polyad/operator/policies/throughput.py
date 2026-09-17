"""
Soul searching: relate application demand to Cheeger targets and bounded topology changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from polyad.compiler.passes.traffic import step_weights
from polyad.graph.cheeger import CheegerIncomplete
from polyad.graph.rules import graph_cheeger, relation_graph
from polyad.graph.temporary import active_entries
from polyad.operator.coordination.contracts import expires_before
from polyad.operator.policies.cheeger import computation_limits
from polyad.operator.policies.rule_state import check_live_rules
from polyad.operator.policies.rules import RuleViolation
from polyad_types.codec import converter, to_dict
from polyad_types.resources import BOUNDARY_KINDS, GROUP
from polyad_types.throughput import ThroughputSample
from polyad_types.topology import topology

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad.operator.reconciliation.controller import Controller
    from polyad_types.rules import Cheeger
    from polyad_types.topology import Topology
    from polyad_types.traffic import TrafficWeights

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
    return await asyncio.to_thread(
        graph_cheeger,
        relation_graph(graph, "connections"),
        graph.throughput.cheegerComputation if graph.throughput else None,
        limits=computation_limits(),
    )


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
    Consume fresh samples and atomically commit approved parameters with their change budget.

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
    try:
        current = await expansion(graph)
    except ValueError as error:
        diagnostic = error.result.report() if isinstance(error, CheegerIncomplete) else {"reason": str(error)}
        await controller.status(
            obj,
            {
                "throughput": {
                    "mode": policy.mode,
                    "phase": "ComputationLimited",
                    "currentCheeger": None,
                    "target": None,
                    "recommendedLayout": None,
                    "proposedCheeger": None,
                    "observedAt": None,
                    "offeredPerSecond": None,
                    "completedPerSecond": None,
                    "demandValue": None,
                    "demandSignal": policy.demand.name if policy.demand else "offeredPerSecond",
                    "demandUnit": policy.demand.unit if policy.demand else f"{policy.unit}/second",
                    "currentTraffic": converter.unstructure(graph.traffic),
                    "targetTraffic": [],
                    "proposedTraffic": [],
                    "currentCapacity": None
                    if graph.capacity is None
                    else {"lookaheadStages": graph.capacity.lookaheadStages, "maxPods": graph.capacity.maxPods},
                    "targetCapacity": None,
                    "proposedCapacity": None,
                    "computation": diagnostic,
                }
            },
        )
        return False
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
        "currentCheeger": current,
        "computation": None,
        "observedAt": None,
        "target": None,
        "recommendedLayout": None,
        "proposedCheeger": None,
        "offeredPerSecond": None,
        "completedPerSecond": None,
        "demandValue": None,
        "demandSignal": policy.demand.name if policy.demand else "offeredPerSecond",
        "demandUnit": policy.demand.unit if policy.demand else f"{policy.unit}/second",
        "currentTraffic": converter.unstructure(graph.traffic),
        "targetTraffic": [],
        "proposedTraffic": [],
        "currentCapacity": None
        if graph.capacity is None
        else {"lookaheadStages": graph.capacity.lookaheadStages, "maxPods": graph.capacity.maxPods},
        "targetCapacity": None,
        "proposedCapacity": None,
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
        try:
            demand_value = policy.demand_value(sample)
        except ValueError:
            demand_value = None
        if valid and demand_value is not None:
            status.update(
                observedAt=sample.observedAt, offeredPerSecond=sample.offeredPerSecond, completedPerSecond=sample.completedPerSecond
            )
            status["demandValue"] = demand_value
            tier = next((item for item in reversed(policy.tiers) if demand_value >= item.threshold), None)
            target = to_dict(tier.cheeger) if tier else None
            status["target"] = target
            status["targetTraffic"] = converter.unstructure(tier.trafficWeights) if tier else []
            status["targetCapacity"] = to_dict(tier.capacity) if tier and tier.capacity else None
            target_identity = {"cheeger": target, "traffic": status["targetTraffic"], "capacity": status["targetCapacity"]}
            traffic_targets: tuple[TrafficWeights, ...] | None = tier.trafficWeights if tier else ()
            if tier and policy.trafficMode == "Headroom":
                from polyad.operator.policies.traffic import headroom_targets

                traffic_targets = await headroom_targets(controller, obj, sample)
                status["targetTraffic"] = converter.unstructure(traffic_targets or ())
                # Require sustained direction, while allowing measured magnitudes to vary.
                current_weights = {
                    route.name: {destination.target: destination.weight for destination in route.destinations} for route in graph.traffic
                }
                target_identity = {
                    "cheeger": target,
                    "capacity": status["targetCapacity"],
                    "trafficMode": "Headroom",
                    "direction": {
                        item.route: {
                            name: (value > current_weights[item.route][name]) - (value < current_weights[item.route][name])
                            for name, value in item.weights.items()
                        }
                        for item in traffic_targets or ()
                    },
                }
            shortfall = (
                tier is not None
                and demand_value > 0
                and sample.offeredPerSecond > 0
                and sample.completedPerSecond < sample.offeredPerSecond * policy.shortfallRatio
            )
            demand = tier is not None and demand_value > 0 and policy.trigger == "Demand"
            rebalance = bool(
                policy.trafficMode == "Headroom"
                and traffic_targets
                and demand_value > 0
                and step_weights(graph, traffic_targets, policy.maxWeightStep) != graph.traffic
            )
            if tier and policy.trafficMode == "Headroom" and traffic_targets is None:
                state.update(since=0, count=0)
                status["phase"] = "WaitingForTrafficSample"
            elif not shortfall and not demand and not rebalance:
                state.update(since=0, count=0, target=target_identity, observed=observed)
                status["phase"] = "Satisfied" if tier else "BelowDemandThreshold"
            else:
                if (
                    state.get("target") != target_identity
                    or observed - state.get("observed", 0) > policy.sampleMaxAgeSeconds
                    or not state.get("since")
                ):
                    state.update(since=observed, count=0)
                if observed > state.get("observed", 0):
                    state["count"] = min(state.get("count", 0) + 1, policy.minSamples)
                state.update(target=target_identity, observed=observed)
                assert tier is not None
                status["phase"] = "Stabilizing"
                if observed - state["since"] >= policy.sustainedSeconds and state["count"] >= policy.minSamples:
                    status["phase"] = (
                        ("ThroughputShortfall" if shortfall else "Satisfied")
                        if within(status["currentCheeger"], tier.cheeger)
                        else "NoAllowedLayout"
                    )
                    if (shortfall or demand) and not within(status["currentCheeger"], tier.cheeger):
                        for layout in policy.layouts:
                            proposal = {**obj["spec"], "connections": converter.unstructure(layout.connections)}
                            try:
                                value = await expansion(topology(proposal, obj["kind"]))
                            except ValueError as error:
                                status.update(
                                    phase="ComputationLimited",
                                    computation=error.result.report() if isinstance(error, CheegerIncomplete) else {"reason": str(error)},
                                )
                                continue
                            if not within(value, tier.cheeger):
                                continue
                            try:
                                await check_live_rules(controller.api, obj, candidate=proposal, candidate_is_logical=True)
                            except RuleViolation:
                                continue
                            status.update(phase="Recommended", recommendedLayout=layout.name, proposedCheeger=value, computation=None)
                            candidate = proposal
                            break
                    traffic = step_weights(graph, traffic_targets or (), policy.maxWeightStep)
                    blocked = False
                    if traffic != graph.traffic and (candidate is not None or within(current, tier.cheeger)):
                        proposal = {**(candidate or obj["spec"]), "traffic": converter.unstructure(traffic)}
                        try:
                            topology(proposal, obj["kind"])
                            await check_live_rules(controller.api, obj, candidate=proposal, candidate_is_logical=True)
                        except (ValueError, RuleViolation):
                            blocked = True
                            candidate = None
                            status.update(phase="NoAllowedLayout", recommendedLayout=None, proposedCheeger=None)
                        else:
                            candidate = proposal
                            status.update(
                                phase="Recommended",
                                proposedTraffic=proposal["traffic"],
                                proposedCheeger=status["proposedCheeger"] or current,
                            )
                    if tier.capacity and not blocked and (candidate is not None or within(current, tier.cheeger)):
                        if os.environ.get("POLYAD_CAPACITY_ENABLED", "false").lower() != "true" or tier.capacity.maxPods > int(
                            os.environ.get("POLYAD_CAPACITY_MAX_PODS", "1024")
                        ):
                            candidate = None
                            status.update(phase="CapacityUnavailable", recommendedLayout=None, proposedCheeger=None, proposedTraffic=[])
                        elif status["targetCapacity"] != status["currentCapacity"]:
                            proposal = {
                                **(candidate or obj["spec"]),
                                "capacity": {**obj["spec"]["capacity"], **status["targetCapacity"]},
                            }
                            try:
                                topology(proposal, obj["kind"])
                                await check_live_rules(controller.api, obj, candidate=proposal, candidate_is_logical=True)
                            except (ValueError, RuleViolation):
                                candidate = None
                                status.update(phase="NoAllowedLayout", recommendedLayout=None, proposedCheeger=None, proposedTraffic=[])
                            else:
                                candidate = proposal
                                status.update(
                                    phase="Recommended",
                                    proposedCapacity=status["targetCapacity"],
                                    proposedCheeger=status["proposedCheeger"] if status["proposedCheeger"] is not None else current,
                                )
                    if policy.trafficMode == "Headroom" and traffic_targets is None:
                        candidate = None
                        status.update(phase="WaitingForTrafficSample", recommendedLayout=None, proposedCheeger=None, proposedTraffic=[])
                    if candidate and policy.mode == "Adapt":
                        if active_entries(obj):
                            status["phase"] = "TemporaryConnectionsActive"
                        elif clock - state.get("lastChange", 0) < policy.cooldownSeconds or len(changes) >= policy.maxChangesPerHour:
                            status["phase"] = "CoolingDown"
                        else:
                            status["phase"] = "Applied"
        else:
            status["phase"] = "WaitingForDemandSignal" if valid else "StaleSample"
            state.update(since=0, count=0)
    state["decision"] = {key: value for key, value in status.items() if key not in {"currentTraffic", "proposedTraffic", "targetTraffic"}}
    changed = bool(status["phase"] == "Applied")
    if changed:
        # Recheck the whole live family immediately before the fenced write.
        await check_live_rules(controller.api, obj, candidate=candidate, candidate_is_logical=True)
        if await capacity_revision(controller.api, obj) != capacity:
            return False
        if policy.trafficMode == "Headroom" and await headroom_targets(controller, obj, sample) != traffic_targets:
            return False
        if clock - observed + time.monotonic() - started > policy.sampleMaxAgeSeconds:
            return False
        expires_before(datetime.fromtimestamp(observed + policy.sampleMaxAgeSeconds, UTC))
        state.update(changes=[*changes, clock], lastChange=clock, since=0, count=0, generation=meta["generation"] + 1)
    if state != previous:
        body: dict[str, Any] = {
            "metadata": {"resourceVersion": meta["resourceVersion"], "annotations": {STATE: json.dumps(state, allow_nan=False)}}
        }
        if changed:
            assert candidate is not None
            body["spec"] = {
                key: candidate[key]
                for key in ("connections", "traffic", "capacity")
                if key in candidate and candidate[key] != obj["spec"].get(key)
            }
        obj = await controller.api.request("PATCH", obj["kind"], meta["namespace"], meta["name"], body)
    await controller.status(obj, {"throughput": status})
    return changed
