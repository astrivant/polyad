"""
Run Soul searching's observe, recommend, admit and commit stages in one place.

This is the algorithm's high-level entry point. Recommendations combine Cheeger
targets, traffic percentages and capacity preparation; admission keeps hard
GraphRules, observation freshness and shared change budgets authoritative.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from polyad.graph.temporary import active_entries
from polyad.operator.coordination.contracts import expires_before
from polyad.operator.policies.rule_state import check_live_rules
from polyad.operator.policies.soul.contracts import STATE
from polyad.operator.policies.soul.observations import capacity_revision, headroom_targets, observe
from polyad.operator.policies.soul.planning import propose
from polyad_types.graphs.topology import topology

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.reconciliation.controller import Controller

__all__ = ("search_soul",)


async def search_soul(controller: Controller, obj: dict[str, Any], *, now: datetime | None = None) -> bool:
    """
    Recommend an approved profile and atomically apply it only after live admission.

    Args:
        controller (Controller): Existing family-leased execution adapter.
        obj (dict[str, Any]): Fresh Graph or PolyGraph document.
        now (datetime | None): Clock override for deterministic reconciliation tests.

    Returns:
        bool: Whether parameters changed and the caller must refresh before other actions.
    """
    graph = topology(obj["spec"], obj["kind"])
    policy = graph.throughput
    if policy is None or graph.suspend or graph.templateOnly or obj.get("status", {}).get("phase") in {"Stopped", "Completed"}:
        return False

    # Observe the current boundary and restart stabilization if its capacity changed.
    started = time.monotonic()
    search = await observe(controller.api, obj, graph, now=now)
    if search.current is None:
        await controller.status(obj, {"throughput": search.status})
        return False

    # Propose a complete approved profile after fresh, sustained application demand.
    proposal = await propose(controller, obj, search)
    state, status, clock = search.state, search.status, search.clock
    changes = state["changes"]
    changed = False

    # Recommendation mode records a decision without mutating the graph.
    # Adapt mode additionally enforces cooldowns and temporary-connection safety.
    if proposal is not None and policy.mode == "Adapt":
        if active_entries(obj):
            status["phase"] = "TemporaryConnectionsActive"
        elif clock - state.get("lastChange", 0) < policy.cooldownSeconds or len(changes) >= policy.maxChangesPerHour:
            status["phase"] = "CoolingDown"
        else:
            # Admit against the whole live family immediately before the fenced write.
            await check_live_rules(controller.api, obj, candidate=proposal.spec, candidate_is_logical=True)

            # Revalidate capacity and demand freshness immediately before writing;
            # planning may have taken long enough for the underlying evidence to change.
            if await capacity_revision(controller.api, obj) != search.capacity:
                return False
            if policy.trafficMode == "Headroom" and await headroom_targets(controller, obj, proposal.sample) != proposal.traffic_targets:
                return False
            if clock - proposal.observed + time.monotonic() - started > policy.sampleMaxAgeSeconds:
                return False
            expires_before(datetime.fromtimestamp(proposal.observed + policy.sampleMaxAgeSeconds, UTC))
            state.update(changes=[*changes, clock], lastChange=clock, since=0, count=0, generation=obj["metadata"]["generation"] + 1)
            status["phase"] = "Applied"
            changed = True

    # Commit parameters and their change budget together, then publish the decision.
    state["decision"] = {key: value for key, value in status.items() if key not in {"currentTraffic", "proposedTraffic", "targetTraffic"}}

    # Persist decision memory together with any spec change under the observed
    # resourceVersion, so concurrent updates cannot silently overwrite each other.
    if state != search.previous:
        meta = obj["metadata"]
        body: dict[str, Any] = {
            "metadata": {"resourceVersion": meta["resourceVersion"], "annotations": {STATE: json.dumps(state, allow_nan=False)}}
        }
        if changed:
            assert proposal is not None
            body["spec"] = {
                key: proposal.spec[key]
                for key in ("connections", "traffic", "capacity")
                if key in proposal.spec and proposal.spec[key] != obj["spec"].get(key)
            }
        obj = await controller.api.request("PATCH", obj["kind"], meta["namespace"], meta["name"], body)
    await controller.status(obj, {"throughput": status})
    return changed
