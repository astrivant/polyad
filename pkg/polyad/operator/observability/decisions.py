"""
Describe operator decisions with bounded identities and OpenTelemetry attributes.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)
BLOCKED = {"Blocked", "Rejected", "Invalid", "Failed", "NoAllowedLayout", "ComputationLimited"}
THROUGHPUT_REASONS = {
    "WaitingForSample": "Waiting for a fresh application throughput report.",
    "WaitingForTrafficSample": "Waiting for complete current replica measurements with usable capacity; traffic percentages are retained.",
    "StaleSample": "The throughput report is stale or targets a different graph revision; no layout change is allowed.",
    "Stabilizing": "Application demand must remain sustained before a layout change is considered.",
    "Satisfied": "Application throughput meets the selected demand tier; connections are retained.",
    "BelowDemandThreshold": "Demand is below the first configured tier; connections are retained.",
    "ThroughputShortfall": "Cheeger and traffic targets are met despite the throughput shortfall; routing and connections are retained.",
    "ComputationLimited": "Cheeger computation could not certify a layout within its configured budgets; no change is allowed.",
    "NoAllowedLayout": "No approved layout satisfies both the throughput target and the current graph constraints.",
    "Recommended": "Approved connection or traffic changes meet the target; Observe mode leaves the graph unchanged.",
    "TemporaryConnectionsActive": "Active temporary connections defer the proposed layout change.",
    "CoolingDown": "The cooldown or rolling change budget defers the proposed layout change.",
    "Applied": "Applied approved connection or traffic changes after fresh graph rules and adaptation checks passed.",
}
decision_context: ContextVar[dict[str, Any] | None] = ContextVar("decision_context", default=None)


def decision(
    event: str,
    message: str,
    *,
    obj: dict[str, Any] | None = None,
    key: tuple[str, str, str] | None = None,
    outcome: str,
    reason: str,
    level: int = logging.INFO,
    attributes: dict[str, Any] | None = None,
) -> None:
    """
    Emit a readable explanation with explicitly selected, payload-free attributes.

    Args:
        event (str): Stable event name under the polyad namespace.
        message (str): Human-readable explanation, never a serialized object or external error body.
        obj (dict[str, Any] | None): Resource used only for identity, generation and target cluster.
        key (tuple[str, str, str] | None): Kind, namespace and name when no document is available.
        outcome (str): Applied, allowed, deferred, blocked or another stable decision outcome.
        reason (str): Machine-readable reason for the decision.
        level (int): Python severity for this occurrence.
        attributes (dict[str, Any] | None): Explicit non-sensitive decision details.

    Returns:
        None: Standard Python handlers receive one structured record.
    """
    if not logger.isEnabledFor(level):
        return
    details = {
        "polyad.target.cluster": os.environ.get("POLYAD_CLUSTER_NAME", ""),
        **(decision_context.get() or {}),
        "polyad.decision.outcome": outcome,
        "polyad.decision.reason": reason,
        **(attributes or {}),
    }
    if obj is not None:
        meta = obj["metadata"]
        key = obj["kind"], meta.get("namespace", ""), meta["name"]
        for field in ("uid", "generation"):
            if field in meta:
                details[f"polyad.resource.{field}"] = meta[field]
        if cluster := obj.get("spec", {}).get("cluster"):
            details["polyad.target.cluster"] = cluster
    if key is not None:
        details.update({"polyad.resource.kind": key[0], "k8s.namespace.name": key[1], "polyad.resource.name": key[2]})
    logger.log(
        level,
        message,
        extra={"event_name": event, "polyad_attributes": {key: value for key, value in details.items() if value is not None}},
        stacklevel=2,
    )


def status_decisions(obj: dict[str, Any], values: dict[str, Any]) -> None:
    """
    Log meaningful committed transitions, excluding heartbeat and metric-only updates.

    Args:
        obj (dict[str, Any]): Resource with status preceding the acknowledged write.
        values (dict[str, Any]): Newly committed status fields.

    Returns:
        None: Unchanged decisions do not produce repeated INFO records.
    """
    before = obj.get("status", {})
    for field in ("phase", "replicas", "desiredReplicas", "scaleCurrent"):
        if field not in values or before.get(field) == values[field]:
            continue
        value = values[field]
        decision(
            "polyad.resource.transition",
            f"{obj['kind']} {field} changed from {before.get(field, 'unobserved')} to {value}.",
            obj=obj,
            outcome="blocked" if value in BLOCKED else "observed",
            reason=f"{field}_changed",
            level=logging.WARNING if value in BLOCKED else logging.INFO,
            attributes={"polyad.status.field": field, "polyad.status.value": value},
        )
    for section in ("throughput", "capacity", "nodes"):
        if section not in values:
            continue
        previous = before.get(section) or {}
        current = values[section] or {}
        entries = {"boundary": current} if section == "throughput" else current.get("nodes", {}) if section == "capacity" else current
        old_entries = (
            {"boundary": previous} if section == "throughput" else previous.get("nodes", {}) if section == "capacity" else previous
        )
        for name, state in entries.items():
            old = old_entries.get(name) or {}
            if not state:
                continue
            fields = (
                ("phase", "recommendedLayout", "mode", "target", "proposedTraffic")
                if section == "throughput"
                else ("phase", "ready", "failed", "completed")
            )
            if all(old.get(field) == state.get(field) for field in fields):
                continue
            phase = state.get("phase") or ("Failed" if state.get("failed") else "Ready" if state.get("ready") else "Waiting")
            attributes: dict[str, Any] = {"polyad.decision.section": section, "polyad.node.name": name, "polyad.status.phase": phase}
            for field in ("mode", "recommendedLayout", "currentCheeger", "proposedCheeger"):
                if state.get(field) is not None:
                    attributes[f"polyad.throughput.{field}"] = state[field]
            if section == "throughput":
                attributes.update({f"polyad.throughput.target.{field}": value for field, value in (state.get("target") or {}).items()})
                for route in state.get("proposedTraffic", []):
                    for destination in route["destinations"]:
                        attributes[f"polyad.traffic.{route['name']}.{destination['target']}.weight"] = destination["weight"]
            decision(
                "polyad.policy.transition",
                THROUGHPUT_REASONS.get(phase, f"Throughput policy: {phase}.")
                if section == "throughput"
                else f"{section.capitalize()} decision for {name}: {phase}.",
                obj=obj,
                outcome="blocked" if phase in BLOCKED else "applied" if phase == "Applied" else "observed",
                reason=phase,
                level=logging.WARNING if phase in BLOCKED else logging.INFO,
                attributes=attributes,
            )
