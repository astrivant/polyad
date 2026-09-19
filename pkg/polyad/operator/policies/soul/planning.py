"""
Select sustained demand targets and recommend a complete, rule-checked parameter profile.

Planning reads live constraints but never writes graph specifications or status.
Observe and Adapt use the same proposal; the controller owns admission and commit.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import TYPE_CHECKING

from polyad.compiler.passes.traffic import step_weights
from polyad.graph.cheeger import CheegerIncomplete
from polyad.operator.policies.rule_state import check_live_rules
from polyad.operator.policies.rules import RuleViolation
from polyad.operator.policies.soul.contracts import SAMPLE, Proposal
from polyad.operator.policies.soul.observations import expansion, headroom_targets
from polyad_types.api.throughput import ThroughputSample
from polyad_types.graphs.topology import topology
from polyad_types.serialization import converter, to_dict

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.policies.soul.contracts import Search
    from polyad.operator.reconciliation.controller import Controller
    from polyad_types.graphs.rules import Cheeger
    from polyad_types.graphs.topology import ThroughputTier
    from polyad_types.networking.traffic import TrafficWeights


async def propose(controller: Controller, obj: dict[str, Any], search: Search) -> Proposal | None:
    """
    Require fresh, distinct and sustained demand before proposing approved changes.

    Args:
        controller (Controller): Family-leased reader of constraints and route destinations.
        obj (dict[str, Any]): Current boundary with its latest aggregate sample.
        search (Search): Current observations and mutable stabilization history/status.

    Returns:
        Proposal | None: A complete recommendation with evidence, or None while waiting or blocked.
    """
    graph, policy, state, status = search.graph, search.policy, search.state, search.status
    raw = json.loads(obj["metadata"].get("annotations", {}).get(SAMPLE, "null"))
    if not raw:
        return None
    sample = converter.structure(raw, ThroughputSample)
    observed = datetime.fromisoformat(sample.observedAt.replace("Z", "+00:00")).timestamp()
    valid = (
        sample.graphUid == obj["metadata"]["uid"]
        and sample.generation == obj["metadata"]["generation"]
        and sample.unit == policy.unit
        and 0 <= search.clock - observed <= policy.sampleMaxAgeSeconds
        and observed >= state.get("settledAfter", 0)
    )
    try:
        demand_value = policy.demand_value(sample)
    except ValueError:
        demand_value = None
    if not valid or demand_value is None:
        status["phase"] = "WaitingForDemandSignal" if valid else "StaleSample"
        state.update(since=0, count=0)
        return None

    status.update(observedAt=sample.observedAt, offeredPerSecond=sample.offeredPerSecond, completedPerSecond=sample.completedPerSecond)
    status["demandValue"] = demand_value
    tier = next((item for item in reversed(policy.tiers) if demand_value >= item.threshold), None)
    target = to_dict(tier.cheeger) if tier else None
    status["target"] = target
    status["targetTraffic"] = converter.unstructure(tier.trafficWeights) if tier else []
    status["targetCapacity"] = to_dict(tier.capacity) if tier and tier.capacity else None
    target_identity = {"cheeger": target, "traffic": status["targetTraffic"], "capacity": status["targetCapacity"]}
    traffic_targets: tuple[TrafficWeights, ...] | None = tier.trafficWeights if tier else ()
    if tier and policy.trafficMode == "Headroom":
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
        return None
    if not shortfall and not demand and not rebalance:
        state.update(since=0, count=0, target=target_identity, observed=observed)
        status["phase"] = "Satisfied" if tier else "BelowDemandThreshold"
        return None

    if state.get("target") != target_identity or observed - state.get("observed", 0) > policy.sampleMaxAgeSeconds or not state.get("since"):
        state.update(since=observed, count=0)
    if observed > state.get("observed", 0):
        state["count"] = min(state.get("count", 0) + 1, policy.minSamples)
    state.update(target=target_identity, observed=observed)
    status["phase"] = "Stabilizing"
    if observed - state["since"] < policy.sustainedSeconds or state["count"] < policy.minSamples:
        return None

    assert tier is not None
    candidate = await candidate_spec(controller, obj, search, tier, traffic_targets, shortfall=shortfall, demand=demand)
    return None if candidate is None else Proposal(candidate, sample, observed, traffic_targets)


async def candidate_spec(
    controller: Controller,
    obj: dict[str, Any],
    search: Search,
    tier: ThroughputTier,
    traffic_targets: tuple[TrafficWeights, ...] | None,
    *,
    shortfall: bool,
    demand: bool,
) -> dict[str, Any] | None:
    """
    Combine approved layout, bounded traffic and capacity changes under live GraphRules.

    Args:
        controller (Controller): Family-leased reader for candidate validation.
        obj (dict[str, Any]): Current boundary specification.
        search (Search): Observed topology and public recommendation status.
        tier (ThroughputTier): Administrator-approved profile selected by sustained demand.
        traffic_targets (tuple[TrafficWeights, ...] | None): Validated desired route percentages.
        shortfall (bool): Whether offered work exceeds the configured completion ratio.
        demand (bool): Whether positive selected demand enables preparation before a shortfall.

    Returns:
        dict[str, Any] | None: Complete proposed specification, or None when unchanged or blocked.
    """
    graph, policy, status, current = search.graph, search.policy, search.status, search.current
    assert current is not None
    candidate = None
    status["phase"] = ("ThroughputShortfall" if shortfall else "Satisfied") if within(current, tier.cheeger) else "NoAllowedLayout"
    if (shortfall or demand) and not within(current, tier.cheeger):
        for layout in policy.layouts:
            proposal = {**obj["spec"], "connections": converter.unstructure(layout.connections)}
            candidate_reports: list[dict[str, Any]] = []
            try:
                value = await expansion(topology(proposal, obj["kind"]), candidate_reports)
            except ValueError as error:
                status.update(
                    phase="ComputationLimited",
                    computation=error.result.report() if isinstance(error, CheegerIncomplete) else {"reason": str(error)},
                )
                continue
            finally:
                status["candidateComputations"].extend({"layout": layout.name, **report} for report in candidate_reports)
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
            status.update(phase="Recommended", proposedTraffic=proposal["traffic"], proposedCheeger=status["proposedCheeger"] or current)
    if tier.capacity and not blocked and (candidate is not None or within(current, tier.cheeger)):
        if os.environ.get("POLYAD_CAPACITY_ENABLED", "false").lower() != "true" or tier.capacity.maxPods > int(
            os.environ.get("POLYAD_CAPACITY_MAX_PODS", "1024")
        ):
            candidate = None
            status.update(phase="CapacityUnavailable", recommendedLayout=None, proposedCheeger=None, proposedTraffic=[])
        elif status["targetCapacity"] != status["currentCapacity"]:
            proposal = {**(candidate or obj["spec"]), "capacity": {**obj["spec"]["capacity"], **status["targetCapacity"]}}
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
    return candidate


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
