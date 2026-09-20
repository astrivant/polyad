"""
Persist generation-fenced service observations and evaluate Daemon objectives.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from polyad.api.http.errors import Conflict, Forbidden
from polyad.events.visibility import observation_ancestry, permitted_observation, public_observation
from polyad_types.resources import ObjectMeta, StatusPatch
from polyad_types.serialization import converter, to_dict

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad_types import GraphAccess, ServiceLevelReport
    from polyad_types.api.service_level import ServiceLevelPolicy


def _evaluate(
    policy: ServiceLevelPolicy, report: ServiceLevelReport, current: dict[str, Any], adaptation: dict[str, Any]
) -> dict[str, Any]:
    """
    Fold one sample into a fixed accounting window and evaluate current compliance.

    Args:
        policy (ServiceLevelPolicy): Daemon-owned minimum service contract.
        report (ServiceLevelReport): Fresh non-overlapping observation.
        current (dict[str, Any]): Previously persisted service-level status.
        adaptation (dict[str, Any]): Generation-current SDK strategy lifecycle status.

    Returns:
        dict[str, Any]: Bounded current state, counters, ratios and violations.
    """
    observed = datetime.fromisoformat(report.observedAt.replace("Z", "+00:00"))
    previous = current.get("lastObservedAt")
    start = observed - timedelta(seconds=report.durationSeconds)

    # Reject overlapping windows before accumulating counters; counting the same
    # requests twice would distort both availability and error-budget consumption.
    if previous:
        previous_observed = datetime.fromisoformat(previous.replace("Z", "+00:00"))
        if previous_observed >= observed:
            raise Conflict("service-level observations must advance monotonically")
        if previous_observed > start:
            raise Conflict("service-level observation windows must not overlap")
    period_start = datetime.fromisoformat(current.get("periodStart", report.observedAt).replace("Z", "+00:00"))
    if current.get("observedGeneration") != report.targetGeneration or (observed - period_start).total_seconds() >= policy.windowSeconds:
        period_start = start
        counters = {
            "eligibleRequests": 0,
            "successfulRequests": 0,
            "requestsWithinLatencyObjective": 0,
            "unavailableSeconds": 0.0,
            "observedSeconds": 0.0,
        }
    else:
        counters = dict(current.get("counters", {}))
    counters["eligibleRequests"] = counters.get("eligibleRequests", 0) + report.eligibleRequests
    counters["successfulRequests"] = counters.get("successfulRequests", 0) + report.successfulRequests
    counters["requestsWithinLatencyObjective"] = counters.get("requestsWithinLatencyObjective", 0) + report.requestsWithinLatencyObjective
    counters["unavailableSeconds"] = counters.get("unavailableSeconds", 0.0) + report.unavailableSeconds
    counters["observedSeconds"] = counters.get("observedSeconds", 0.0) + report.durationSeconds

    # Aggregate request counts before forming ratios. Averaging per-window
    # percentages would incorrectly give tiny and busy windows equal weight.
    eligible = counters["eligibleRequests"]
    availability = counters["successfulRequests"] / eligible if eligible else float(report.serving)
    latency_compliance = counters["requestsWithinLatencyObjective"] / eligible if eligible else float(report.serving)
    missing = sorted(set(policy.requiredCapabilities) - set(report.capabilities))
    violations: list[dict[str, Any]] = []

    def violate(objective: str, observed_value: Any, allowed: Any) -> None:
        violations.append({"objective": objective, "observed": observed_value, "allowed": allowed})

    unavailable = not report.serving or bool(missing)
    if not report.serving:
        violate("serving", False, True)
    if missing:
        violate("requiredCapabilities", missing, list(policy.requiredCapabilities))
    if availability < policy.availability:
        violate("availability", availability, policy.availability)
    if policy.latencyP99Seconds is not None and (report.latencyP99Seconds is None or report.latencyP99Seconds > policy.latencyP99Seconds):
        violate("latencyP99Seconds", report.latencyP99Seconds, policy.latencyP99Seconds)
    if policy.minimumThroughputPerSecond is not None and (
        report.completedPerSecond is None or report.completedPerSecond < policy.minimumThroughputPerSecond
    ):
        violate("minimumThroughputPerSecond", report.completedPerSecond, policy.minimumThroughputPerSecond)
    if adaptation.get("inProgress") and report.unavailableSeconds > policy.adaptation.maximumUnavailableSeconds:
        violate("adaptation.maximumUnavailableSeconds", report.unavailableSeconds, policy.adaptation.maximumUnavailableSeconds)
    statistics = adaptation.get("statistics", {}) if adaptation.get("observedGeneration") == report.targetGeneration else {}
    if statistics.get("failed", 0) > policy.adaptation.maximumFailedAttempts:
        violate("adaptation.maximumFailedAttempts", statistics["failed"], policy.adaptation.maximumFailedAttempts)
    active_durations = [
        max(0.0, (observed - datetime.fromisoformat(value["startedAt"].replace("Z", "+00:00"))).total_seconds())
        for value in adaptation.get("invocations", {}).values()
        if value.get("startedAt")
    ]
    if active_durations and max(active_durations) > policy.adaptation.maximumDurationSeconds:
        violate("adaptation.maximumDurationSeconds", max(active_durations), policy.adaptation.maximumDurationSeconds)

    # Distinguish inability to serve the required capability from degraded
    # service that still serves requests but misses another contract objective.
    state = "Unavailable" if unavailable else "Degraded" if violations else "Compliant"
    allowed_failures = max(1e-12, 1 - policy.availability)
    consumed = (1 - availability) / allowed_failures
    return {
        "observedGeneration": report.targetGeneration,
        "state": state,
        "contractSatisfied": not violations,
        "observedAt": report.observedAt,
        "lastObservedAt": report.observedAt,
        "sampleDeadline": (observed + timedelta(seconds=policy.sampleMaxAgeSeconds)).isoformat(),
        "periodStart": period_start.isoformat(),
        "windowSeconds": policy.windowSeconds,
        "objectives": to_dict(policy),
        "sample": to_dict(report),
        "counters": counters,
        "availability": availability,
        "latencyCompliance": latency_compliance,
        "errorBudgetRemaining": max(0.0, 1.0 - consumed),
        "violations": violations,
    }


async def report_service_level(
    api: API, namespace: str, report: ServiceLevelReport, grants: tuple[GraphAccess, ...] | None
) -> dict[str, Any]:
    """
    Authorize, evaluate and persist one Daemon service-level observation.

    Args:
        api (API): Ordered Kubernetes intake adapter.
        namespace (str): Fixed API listener namespace.
        report (ServiceLevelReport): Fenced observation from the application.
        grants (tuple[GraphAccess, ...] | None): Named graph grants, or legacy namespace access.

    Returns:
        dict[str, Any]: Current service-level status acknowledgement.
    """
    graph = await api.get(report.graphKind, namespace, report.graph)
    reserved_name = os.environ.get("POLYAD_SELF_GRAPH", "")
    reserved = (os.environ.get("POLYAD_NAMESPACE", namespace), reserved_name) if reserved_name else None
    if graph is None or not await public_observation(api, graph, reserved_graph=reserved):
        raise Forbidden("service-level target is unavailable")
    identity = {"kind": graph["kind"], **graph["metadata"]}
    if grants is not None and not permitted_observation(identity, await observation_ancestry(api, graph), grants):
        raise Forbidden("credential does not authorize this graph tree")
    if graph["metadata"]["uid"] != report.graphUid or graph["metadata"].get("deletionTimestamp"):
        raise Conflict("service-level graph incarnation changed")
    obj = await api.get("Daemon", namespace, report.target)
    if obj is None or not await public_observation(api, obj, reserved_graph=reserved):
        raise Forbidden("service-level Daemon is unavailable")
    meta = obj["metadata"]
    if meta["uid"] != report.targetUid or meta["generation"] != report.targetGeneration or meta.get("deletionTimestamp"):
        raise Conflict("service-level Daemon incarnation or generation changed")
    from polyad_types.api.service_level import ServiceLevelPolicy

    configured = obj.get("spec", {}).get("serviceLevel")
    if not isinstance(configured, dict):
        raise ValueError("service-level reporting requires Daemon spec.serviceLevel")
    policy = converter.structure(configured, ServiceLevelPolicy)
    observed = datetime.fromisoformat(report.observedAt.replace("Z", "+00:00"))
    if not 0 <= (datetime.now(UTC) - observed).total_seconds() <= policy.sampleMaxAgeSeconds:
        raise ValueError("service-level observation is stale or from the future")
    status = obj.get("status", {})
    current = status.get("serviceLevel", {})
    instances = dict(current.get("instances", {})) if current.get("observedGeneration") == meta["generation"] else {}

    # One reusable Daemon definition may serve several graph nodes. Keep their
    # windows separate, then expose the worst instance state on the definition.
    instance_key = f"{report.graphUid}/{report.node}"
    value = _evaluate(policy, report, instances.get(instance_key, {}), status.get("adaptation", {}))
    instances[instance_key] = value
    if len(instances) > 256:
        raise ValueError("a Daemon service contract supports at most 256 observed graph nodes")
    precedence = {"Compliant": 0, "Degraded": 1, "Unavailable": 2}
    aggregate_state = max((entry["state"] for entry in instances.values()), key=precedence.__getitem__)
    service_level = {
        **value,
        "state": aggregate_state,
        "contractSatisfied": all(entry["contractSatisfied"] for entry in instances.values()),
        "instances": instances,
    }
    await api.request(
        "PATCH",
        "Daemon",
        namespace,
        report.target,
        StatusPatch(
            metadata=ObjectMeta(resourceVersion=meta["resourceVersion"]),
            status={"observedGeneration": meta["generation"], "serviceLevel": service_level},
        ),
        status=True,
    )
    return {"targetUid": report.targetUid, "generation": meta["generation"], "serviceLevel": service_level}
