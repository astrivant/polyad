"""
Verify Daemon service contracts remain distinct from adaptation progress.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from attrs import evolve

from polyad.api.composition.builder import APIBuilder
from polyad.api.http.errors import Conflict
from polyad.api.workloads.adaptation import report_adaptation
from polyad.api.workloads.service_level import report_service_level
from polyad_types import AdaptationReport, ServiceLevelPolicy, ServiceLevelReport, to_dict
from tests.test_operator import FakeAPI, resource


def sample(observed: datetime, **changes: object) -> ServiceLevelReport:
    """
    Build one current service-level observation.

    Args:
        observed (datetime): End of the observation window.
        changes (object): Field overrides for the baseline compliant sample.

    Returns:
        ServiceLevelReport: Fenced report for the test Daemon.
    """
    baseline: dict[str, object] = {
        "graph": "pipeline",
        "graphUid": "uid-pipeline",
        "target": "consumer",
        "targetUid": "uid-consumer",
        "targetGeneration": 1,
        "node": "source",
        "observedAt": observed.isoformat(),
        "durationSeconds": 10.0,
        "serving": True,
        "capabilities": ("consume",),
        "eligibleRequests": 1000,
        "successfulRequests": 1000,
        "requestsWithinLatencyObjective": 999,
        "latencyP99Seconds": 0.2,
        "completedPerSecond": 50.0,
    }
    baseline.update(changes)
    return ServiceLevelReport(**baseline)  # type: ignore[arg-type]


def objects() -> tuple[dict[str, object], dict[str, object]]:
    """
    Build an authorized graph and Daemon with an explicit contract.

    Returns:
        tuple[dict[str, object], dict[str, object]]: Graph and Daemon test resources.
    """
    graph = resource("Graph", "pipeline", {"nodes": []})
    daemon = resource(
        "Daemon",
        "consumer",
        {
            "serviceLevel": {
                "requiredCapabilities": ["consume"],
                "availability": 0.999,
                "latencyP99Seconds": 0.5,
                "minimumThroughputPerSecond": 40,
                "adaptation": {"maximumDurationSeconds": 30, "maximumUnavailableSeconds": 2, "maximumFailedAttempts": 0},
            }
        },
    )
    return graph, daemon


def test_service_level_policy_validates_objectives() -> None:
    """
    Reject impossible ratios, duplicate capabilities and unbounded windows.

    Returns:
        None: Assertions cover public model validation.
    """
    with pytest.raises(ValueError):
        ServiceLevelPolicy(availability=1.1)
    with pytest.raises(ValueError):
        ServiceLevelPolicy(requiredCapabilities=("consume", "consume"))
    with pytest.raises(ValueError):
        ServiceLevelPolicy(windowSeconds=1)


def test_service_level_http_route_uses_the_typed_report() -> None:
    """
    Expose the separately authorized report through the shared application API.

    Returns:
        None: Assertions verify HTTP deserialization and acknowledgement.
    """
    received: list[ServiceLevelReport] = []

    def accept(report: ServiceLevelReport) -> dict[str, str]:
        received.append(report)
        return {"state": "Compliant"}

    app = APIBuilder(service_level=accept).with_handlers(lambda _: {}, lambda *_: None).with_bearer_token("secret").build().test_client()
    report = sample(datetime.now(UTC))
    response = app.post("/v1/service-level", json=to_dict(report), headers={"Authorization": "Bearer secret"})
    assert response.status_code == 202 and response.json == {"state": "Compliant"}
    assert received == [report]


def test_reports_quantify_compliant_degraded_and_unavailable_service() -> None:
    """
    Distinguish objective failure from loss of the minimum service capability.

    Returns:
        None: Assertions verify persisted state and window counters.
    """

    async def run() -> None:
        now = datetime.now(UTC) - timedelta(seconds=30)
        api = FakeAPI(*objects())
        accepted = await report_service_level(api, "test", sample(now), None)
        status = accepted["serviceLevel"]
        assert status["state"] == "Compliant" and status["contractSatisfied"]
        assert status["availability"] == 1 and status["errorBudgetRemaining"] == 1

        degraded = sample(
            now + timedelta(seconds=10),
            successfulRequests=990,
            requestsWithinLatencyObjective=900,
            latencyP99Seconds=0.8,
            completedPerSecond=30.0,
        )
        status = (await report_service_level(api, "test", degraded, None))["serviceLevel"]
        assert status["state"] == "Degraded" and not status["contractSatisfied"]
        assert {item["objective"] for item in status["violations"]} >= {
            "availability",
            "latencyP99Seconds",
            "minimumThroughputPerSecond",
        }
        unavailable = sample(now + timedelta(seconds=20), serving=False, capabilities=(), unavailableSeconds=10.0)
        status = (await report_service_level(api, "test", unavailable, None))["serviceLevel"]
        assert status["state"] == "Unavailable"
        assert status["counters"]["eligibleRequests"] == 3000

    asyncio.run(run())


def test_adaptation_progress_and_sla_state_are_independent() -> None:
    """
    Keep a compliant service progressing, then expose an exceeded transition budget.

    Returns:
        None: Assertions verify orthogonal lifecycle and service status.
    """

    async def run() -> None:
        now = datetime.now(UTC) - timedelta(seconds=3)
        api = FakeAPI(*objects())
        adaptation = AdaptationReport(
            "pipeline",
            "uid-pipeline",
            "consumer",
            "uid-consumer",
            1,
            "Daemon",
            "source",
            "call",
            "TopologyStrategy",
            "Running",
            (now - timedelta(seconds=45)).isoformat(),
        )
        await report_adaptation(api, "test", adaptation, None)
        definition = api.objects[("Daemon", "test", "consumer")]
        assert definition["status"]["progressing"]
        status = (await report_service_level(api, "test", sample(now), None))["serviceLevel"]
        assert status["state"] == "Degraded"
        assert status["violations"][-1]["objective"] == "adaptation.maximumDurationSeconds"

        terminal = evolve(adaptation, phase="Failed", observedAt=datetime.now(UTC).isoformat())
        await report_adaptation(api, "test", terminal, None)
        definition = api.objects[("Daemon", "test", "consumer")]
        assert not definition["status"]["progressing"]
        assert definition["status"]["serviceLevel"]["state"] == "Degraded"
        assert definition["status"]["adaptation"]["statistics"]["failed"] == 1

    asyncio.run(run())


def test_service_level_reports_are_identity_and_order_fenced() -> None:
    """
    Reject replayed samples and replacement Daemon identities.

    Returns:
        None: Assertions cover optimistic observation fencing.
    """

    async def run() -> None:
        now = datetime.now(UTC) - timedelta(seconds=3)
        api = FakeAPI(*objects())
        report = sample(now)
        await report_service_level(api, "test", report, None)
        with pytest.raises(Conflict, match="monotonically"):
            await report_service_level(api, "test", report, None)
        with pytest.raises(Conflict, match="Daemon incarnation"):
            await report_service_level(
                api, "test", evolve(report, targetUid="replacement", observedAt=(now + timedelta(seconds=1)).isoformat()), None
            )

    asyncio.run(run())
