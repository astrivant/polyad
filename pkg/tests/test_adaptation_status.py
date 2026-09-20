"""
Exercise authorized SDK adaptation lifecycle persistence.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from attrs import evolve

from polyad.api.workloads.adaptation import report_adaptation
from polyad.exceptions.api import Conflict
from polyad_types import AdaptationReport
from tests.test_operator import FakeAPI, resource


def report(phase="Running", invocation="call"):
    """
    Build one current report for the test graph.
    """
    return AdaptationReport(
        "pipeline",
        "uid-pipeline",
        "consumer",
        "uid-consumer",
        1,
        "Daemon",
        "source",
        invocation,
        "TopologyStrategy",
        phase,
        datetime.now(UTC).isoformat(),
    )


def test_adaptation_status_brackets_concurrent_invocations():
    """
    Keep health progressing until every independently identified call terminates.
    """

    async def run():
        api = FakeAPI(resource("Graph", "pipeline", {"nodes": []}), resource("Daemon", "consumer", {}))
        first = report(invocation="first")
        second = report(invocation="second")
        await report_adaptation(api, "test", first, None)
        await report_adaptation(api, "test", second, None)
        definition = api.objects[("Daemon", "test", "consumer")]
        status = definition["status"]["adaptation"]
        assert status["inProgress"] and set(status["invocations"]) == {"first", "second"}
        assert definition["status"]["progressing"]

        # Finishing one invocation must not clear Progressing while another adaptation is active.
        await report_adaptation(api, "test", evolve(first, phase="Succeeded", observedAt=datetime.now(UTC).isoformat()), None)
        assert api.objects[("Daemon", "test", "consumer")]["status"]["adaptation"]["inProgress"]
        await report_adaptation(api, "test", evolve(second, phase="Failed", observedAt=datetime.now(UTC).isoformat()), None)
        definition = api.objects[("Daemon", "test", "consumer")]
        status = definition["status"]["adaptation"]
        assert not status["inProgress"] and status["invocations"] == {}
        assert not definition["status"]["progressing"]
        assert status["lastTransition"]["phase"] == "Failed"

    asyncio.run(run())


def test_adaptation_status_fences_identity_time_and_transition_order():
    """
    Reject replacement graphs, stale reports and terminal transitions without a start.
    """

    async def run():
        api = FakeAPI(resource("Graph", "pipeline", {"nodes": []}), resource("Daemon", "consumer", {}))
        with pytest.raises(Conflict):
            await report_adaptation(api, "test", evolve(report(), graphUid="replacement"), None)
        with pytest.raises(ValueError, match="stale"):
            await report_adaptation(api, "test", evolve(report(), observedAt=(datetime.now(UTC) - timedelta(minutes=2)).isoformat()), None)
        with pytest.raises(Conflict, match="not active"):
            await report_adaptation(api, "test", report("Succeeded"), None)

    asyncio.run(run())
