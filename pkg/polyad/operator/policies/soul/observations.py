"""
Observe exact structure, live execution capacity and revision-fenced traffic reports.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from polyad.compiler.passes.traffic import capacity_weights
from polyad.graph.cheeger import CheegerIncomplete, compute_cheeger
from polyad.graph.rules import relation_graph
from polyad.operator.policies.cheeger import computation_limits
from polyad.operator.policies.soul.contracts import STATE, Search
from polyad_types.graphs.topology import topology
from polyad_types.resources import BOUNDARY_KINDS
from polyad_types.serialization import converter, to_dict

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad.operator.reconciliation.controller import Controller
    from polyad_types.api.throughput import ThroughputSample
    from polyad_types.graphs.topology import Topology
    from polyad_types.networking.traffic import TrafficWeights

__all__ = (
    "capacity_revision",
    "expansion",
    "headroom_targets",
    "observe",
)


async def observe(api: API, obj: dict[str, Any], graph: Topology, *, now: datetime | None = None) -> Search:
    """
    Measure the current boundary and restore its stabilization history without writing.

    Args:
        api (API): Fresh family reader.
        obj (dict[str, Any]): Current Graph or PolyGraph document.
        graph (Topology): Parsed boundary with an enabled throughput policy.
        now (datetime | None): Clock override for deterministic reconciliation tests.

    Returns:
        Search: Observations and history, or a ComputationLimited status with no exact current value.
    """
    policy = graph.throughput
    assert policy is not None
    meta = obj["metadata"]
    status: dict[str, Any] = {
        "mode": policy.mode,
        "phase": "WaitingForSample",
        "observedGeneration": meta["generation"],
        "currentComputation": None,
        "candidateComputations": [],
        "currentCheeger": None,
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
    search = Search(graph, policy, status)
    calculations: list[dict[str, Any]] = []
    try:
        search.current = await expansion(graph, calculations)
    except ValueError as error:
        diagnostic = error.result.report() if isinstance(error, CheegerIncomplete) else {"reason": str(error)}
        status.update(phase="ComputationLimited", currentComputation=diagnostic, computation=diagnostic)
        return search
    status.update(currentCheeger=search.current, currentComputation=calculations[0])

    search.clock = (now or datetime.now(UTC)).timestamp()
    annotations = meta.get("annotations", {})
    search.previous = json.loads(annotations.get(STATE, "{}"))
    state = dict(search.previous)

    # A changed policy or generation invalidates accumulated stabilization
    # evidence, but recent mutation history still constrains adaptation frequency.
    fingerprint = hashlib.sha256(json.dumps(to_dict(policy), sort_keys=True).encode()).hexdigest()

    # Keep rolling change budgets even when the policy or topology is edited.
    changes = [stamp for stamp in state.get("changes", []) if search.clock - stamp < 3600]
    if state.get("policy") != fingerprint or state.get("generation") != meta["generation"]:
        state = {"changes": changes, "lastChange": state.get("lastChange", 0)}
    state.update(policy=fingerprint, generation=meta["generation"], changes=changes)
    search.capacity = await capacity_revision(api, obj)

    # Demand observed before an execution-capacity change cannot prove the new
    # layout is still underprovisioned. Require evidence from after that change.
    if search.previous.get("capacity") and search.previous["capacity"] != search.capacity:
        state.update(since=0, count=0, settledAfter=search.clock)
    state["capacity"] = search.capacity
    search.state = state
    return search


async def expansion(graph: Topology, reports: list[dict[str, Any]] | None = None) -> float:
    """
    Keep exact cut enumeration off the operator's event loop.

    Args:
        graph (Topology): Current or proposed boundary topology.
        reports (list[dict[str, Any]] | None): Optional destination for the calculation certificate.

    Returns:
        float: Exact Cheeger constant of its connections projection.
    """
    result = await asyncio.to_thread(
        compute_cheeger,
        relation_graph(graph, "connections"),
        graph.throughput.cheegerComputation if graph.throughput else None,
        limits=computation_limits(),
    )
    if reports is not None:
        reports.append(result.report())

    # This caller needs a numeric structural measurement, not merely a policy
    # decision. Do not promote a reduced cut's upper bound to an exact constant.
    if not result.exact:
        raise CheegerIncomplete(result)
    assert result.upperBound is not None
    return result.upperBound


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


async def headroom_targets(controller: Controller, obj: dict[str, Any], sample: ThroughputSample) -> tuple[TrafficWeights, ...] | None:
    """
    Convert complete, revision-fenced replica measurements into bounded percentage targets.

    Args:
        controller (Controller): Family-leased reader for current replica identities.
        obj (dict[str, Any]): Graph containing the configured routes.
        sample (ThroughputSample): Fresh aggregate and per-replica measurements from one window.

    Returns:
        tuple[TrafficWeights, ...] | None: Desired splits, or None for missing, replaced or unusable replica measurements.
    """
    from polyad.operator.policies.traffic import target_selector
    from polyad.operator.reconciliation.controller import Pending

    graph = topology(obj["spec"], obj["kind"])
    reports = {(item.route, item.target): item for item in sample.traffic}
    result = []
    for route in graph.traffic:
        capacities = {}
        for destination in route.destinations:
            report = reports.get((route.name, destination.target))
            if report is None:
                return None
            executions: dict[str, Any] = {}
            try:
                await target_selector(controller, obj, destination.target, executions=executions)
            except Pending:
                return None
            execution = executions[destination.target]
            if execution["metadata"]["uid"] != report.targetUid or execution["metadata"].get("generation", 1) != report.generation:
                return None
            capacities[destination.target] = report.completedPerSecond + report.headroomPerSecond
        weights = capacity_weights(route, capacities)
        if weights is None:
            return None
        result.append(weights)
    return tuple(result)
