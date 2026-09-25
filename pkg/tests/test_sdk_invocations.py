"""
Separate SDK actors, retryable deliveries and traces within one workload identity.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from polyad.api.workloads.adaptation import report_adaptation
from polyad_sdk import AdaptationReporter, CallbackStrategy, Client, ObserveStrategy
from polyad_types.events.models import EventIdentity
from tests.test_operator import FakeAPI, resource
from tests.test_sdk import RecordingService, observation
from tests.test_sdk import runtime as runtime
from tests.test_sdk_telemetry import telemetry as telemetry


@pytest.fixture
def actors(runtime, telemetry):
    """
    Construct same-identity actors with shared clients, reporters and telemetry.
    """
    original, topology, clock, _ = runtime
    instrument, _, _ = telemetry
    context = replace(
        original.context,
        definition=EventIdentity(kind="Daemon", namespace="test", name="consumer", uid="uid-consumer"),
        definition_generation=1,
    )
    reporter = MagicMock(spec=AdaptationReporter)

    def create(strategies=None, checkpoint=None):
        return RecordingService(
            original.identity,
            original.events,
            context=context,
            adaptations=reporter,
            strategies=[ObserveStrategy()] if strategies is None else strategies,
            telemetry=instrument,
            clock=clock,
            checkpoint=checkpoint,
        )

    return create, reporter, topology


def test_same_workload_instances_have_distinct_reports_and_read_only_ids(actors):
    """
    Runtime identity is independent of shared graph, Pod, strategy and cursor identity.
    """
    create, reporter, _ = actors
    first, second = create(), create()
    assert first.identity == second.identity and first.telemetry is second.telemetry
    assert first.instance_id != second.instance_id
    assert UUID(first.instance_id).version == UUID(second.instance_id).version == 4
    assert first.delivery_id is second.delivery_id is None
    with pytest.raises(AttributeError):
        first.instance_id = second.instance_id

    first.refresh()
    second.refresh()
    reports = [call.args[0] for call in reporter.report_adaptation.call_args_list]
    assert reports[0].invocationId == reports[1].invocationId
    assert reports[2].invocationId == reports[3].invocationId
    assert reports[0].invocationId != reports[2].invocationId
    assert first.cursor == second.cursor == "0-0"
    assert first.delivery_id is second.delivery_id is None


def test_new_refresh_and_reset_deliveries_do_not_reuse_cursor_based_ids(actors):
    """
    Different snapshots and explicit baselines have distinct IDs even without a new stream cursor.
    """
    create, reporter, topology = actors
    service = create()
    deliveries = []
    service.on_change(lambda _: deliveries.append(service.delivery_id))
    service.refresh()
    topology["outgoing"][0]["node"]["executions"][0]["replicas"] = 2
    service.refresh()
    service.refresh(reset=True)
    assert service.cursor == "0-0" and len(set(deliveries)) == 3
    starts = [call.args[0] for call in reporter.report_adaptation.call_args_list if call.args[0].phase == "Running"]
    assert len({report.invocationId for report in starts}) == 3


def test_retry_preserves_report_ids_but_creates_new_trace_attempts(actors, telemetry):
    """
    Retry a failed strategy with stable business identity and distinct diagnostic spans.
    """
    create, reporter, _ = actors
    _, spans, _ = telemetry
    callback = MagicMock(side_effect=[RuntimeError("retry me"), None])
    service = create([CallbackStrategy(callback)])
    with pytest.raises(RuntimeError, match="retry me"):
        service.refresh()
    pending = service.delivery_id
    assert pending is not None
    service.refresh()
    reports = [call.args[0] for call in reporter.report_adaptation.call_args_list]
    assert [report.phase for report in reports] == ["Running", "Failed", "Running", "Succeeded"]
    assert len({report.invocationId for report in reports}) == 1
    attempts = [span for span in spans.get_finished_spans() if span.name == "adaptation.strategy"]
    assert len(attempts) == 2 and attempts[0].context.span_id != attempts[1].context.span_id
    assert {span.attributes["polyad.sdk.adaptation.delivery.id"] for span in attempts} == {pending}
    assert {span.attributes["polyad.sdk.adaptation.invocation.id"] for span in attempts} == {reports[0].invocationId}
    assert service.delivery_id is None


def test_checkpoint_retry_and_duplicate_event_do_not_repeat_completed_strategies(actors, telemetry):
    """
    Keep delivery correlation through checkpoint failure without reporting finished work twice.
    """
    create, reporter, _ = actors
    _, spans, _ = telemetry
    checkpoint = MagicMock(side_effect=[None, RuntimeError("checkpoint unavailable"), None])
    service = create(checkpoint=checkpoint)
    service.refresh()
    event = observation()
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        service.dispatch(event)
    pending = service.delivery_id
    calls = reporter.report_adaptation.call_count
    service.dispatch(event)
    service.dispatch(event)
    assert service.delivery_id is None and reporter.report_adaptation.call_count == calls
    deliveries = [span for span in spans.get_finished_spans() if span.name == "adaptation.delivery"]
    assert len(deliveries) == 3
    assert deliveries[-2].attributes["polyad.sdk.adaptation.delivery.id"] == pending
    assert deliveries[-1].attributes["polyad.sdk.adaptation.delivery.id"] == pending


def test_repeated_strategy_object_has_distinct_registration_invocations(actors, telemetry):
    """
    Registration position, not object lookup, identifies each independently retryable component.
    """
    create, reporter, _ = actors
    _, spans, _ = telemetry
    strategy = ObserveStrategy()
    create([strategy, strategy]).refresh()
    starts = [call.args[0] for call in reporter.report_adaptation.call_args_list if call.args[0].phase == "Running"]
    assert len(starts) == 2 and starts[0].invocationId != starts[1].invocationId
    attempts = [span for span in spans.get_finished_spans() if span.name == "adaptation.strategy"]
    assert [span.attributes["polyad.sdk.strategy.index"] for span in attempts] == [0, 1]


def test_shared_telemetry_and_clients_keep_concurrent_service_traces_separate(actors, telemetry):
    """
    Concurrent callbacks inherit only their actor's correlation and never label metrics with UUIDs.
    """
    create, reporter, _ = actors
    instrument, spans, reader = telemetry
    barrier = Barrier(2)
    client = Client("https://operator.example", "secret", telemetry=instrument)
    client._opener = MagicMock()

    def work(change, current):
        barrier.wait(timeout=5)
        client._open("GET", "/private")

    services = [create([CallbackStrategy(work)]) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(service.refresh) for service in services]
        for future in futures:
            future.result(timeout=10)

    exported = spans.get_finished_spans()
    requests = [span for span in exported if span.name == "api.request"]
    assert {span.attributes["polyad.sdk.service.instance.id"] for span in requests} == {service.instance_id for service in services}
    assert len({span.context.trace_id for span in requests}) == 2
    invocations = {call.args[0].invocationId for call in reporter.report_adaptation.call_args_list}
    assert {span.attributes["polyad.sdk.adaptation.invocation.id"] for span in requests} == invocations
    for request in requests:
        parent = next(span for span in exported if span.context.span_id == request.parent.span_id)
        assert parent.name == "adaptation.strategy"
        assert request.attributes["polyad.sdk.adaptation.delivery.id"] == parent.attributes["polyad.sdk.adaptation.delivery.id"]

    # Context is restored after each delivery, and the shared provider remains
    # usable without inheriting IDs from either completed service.
    with instrument.operation("outside") as outside:
        assert not any(key.startswith("polyad.sdk.") for key in outside.attributes)
    for resource_metrics in reader.get_metrics_data().resource_metrics:
        for scope in resource_metrics.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    assert not any(key.startswith("polyad.sdk.") for key in point.attributes)


def test_nested_services_retain_causality_without_inheriting_invocation_ids(actors, telemetry):
    """
    Nested actors stay in one causal trace but replace the caller's actor and delivery correlation.
    """
    create, _, _ = actors
    instrument, spans, _ = telemetry
    inner = create()
    outer = create([CallbackStrategy(lambda *_: inner.refresh())])
    with instrument.operation("application.request"):
        outer.refresh()
    exported = spans.get_finished_spans()
    assert len({span.context.trace_id for span in exported}) == 1
    deliveries = [span for span in exported if span.name == "adaptation.delivery"]
    assert {span.attributes["polyad.sdk.service.instance.id"] for span in deliveries} == {outer.instance_id, inner.instance_id}
    assert all("polyad.sdk.adaptation.invocation.id" not in span.attributes for span in deliveries)
    assert len({span.attributes["polyad.sdk.adaptation.delivery.id"] for span in deliveries}) == 2


def test_sdk_reports_keep_operator_progressing_until_both_instances_finish(actors):
    """
    Replay overlapping real SDK reports through the operator's status merge and generation fences.
    """
    create, reporter, _ = actors
    create().refresh()
    create().refresh()
    first_start, first_done, second_start, second_done = [call.args[0] for call in reporter.report_adaptation.call_args_list]

    async def replay():
        api = FakeAPI(resource("Graph", "pipeline", {"nodes": []}), resource("Daemon", "consumer", {}))
        await report_adaptation(api, "test", first_start, None)
        both = await report_adaptation(api, "test", second_start, None)
        assert both["progressing"] and len(both["adaptation"]["invocations"]) == 2
        remaining = await report_adaptation(api, "test", first_done, None)
        assert remaining["progressing"]
        assert set(remaining["adaptation"]["invocations"]) == {second_start.invocationId}
        finished = await report_adaptation(api, "test", second_done, None)
        assert not finished["progressing"] and not finished["adaptation"]["invocations"]

    asyncio.run(replay())
