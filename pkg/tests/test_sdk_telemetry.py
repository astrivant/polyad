"""
Verify SDK traces and metrics with in-memory OpenTelemetry providers and no exporters.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from polyad_sdk import Client, Environment, ProcessPlan, ProcessSupervisor, Telemetry
from tests.test_sdk import runtime as runtime


@pytest.fixture
def telemetry():
    """
    Own deterministic telemetry providers outside the SDK's lifecycle.
    """
    spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
    tracer = TracerProvider(shutdown_on_exit=False)
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    meter = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    instrument = Telemetry(tracer_provider=tracer, meter_provider=meter)
    try:
        yield instrument, spans, reader
    finally:
        tracer.shutdown()
        meter.shutdown()


def test_operation_records_duration_and_error_type_without_payload(telemetry):
    """
    Collect operation outcomes while keeping exception contents out of exported spans.
    """
    instrument, spans, reader = telemetry
    with instrument.operation("application.work"):
        pass
    with pytest.raises(ValueError):
        with instrument.operation("application.work"):
            raise ValueError("sensitive request body")
    exported = spans.get_finished_spans()
    assert exported[1].attributes["error.type"] == "ValueError"
    assert not exported[1].events and "sensitive" not in str(exported[1].to_json())
    metrics = {
        metric.name: metric
        for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert {point.attributes["outcome"] for point in metrics["polyad.sdk.operations"].data.data_points} == {"success", "error"}
    assert sum(point.count for point in metrics["polyad.sdk.operation.duration"].data.data_points) == 2
    instrument.close()
    with instrument.operation("still.application.owned"):
        pass
    assert len(spans.get_finished_spans()) == 3


def test_service_stages_use_shared_instrumentation(runtime, telemetry):
    """
    Instrument strategy delivery and adaptation without changing checkpoint semantics.
    """
    service, _, _, *_ = runtime
    instrument, spans, _ = telemetry
    service.telemetry = instrument
    service.refresh()
    names = [span.name for span in spans.get_finished_spans()]
    assert names == ["adaptation.strategy", "adaptation.apply", "adaptation.delivery"]
    assert service.cursor is not None and len(service.changes) == 1


def test_client_requests_propagate_current_trace_and_omit_credentials(telemetry):
    """
    An HTTP request gets a child span and W3C parent without exporting URL, body or bearer token.
    """
    instrument, spans, _ = telemetry
    client = Client("https://operator.example", "secret-token", telemetry=instrument)
    client._opener = MagicMock()
    with instrument.operation("application.decision"):
        client._open("POST", "/private-id", {"private": "request-payload"})
    request = client._opener.open.call_args.args[0]
    assert request.get_header("Traceparent")
    child, parent = spans.get_finished_spans()
    assert child.parent.span_id == parent.context.span_id
    assert all(secret not in child.to_json() for secret in ("secret-token", "private-id", "request-payload"))


def test_child_context_extraction_retains_parent_trace(telemetry):
    """
    Child startup can explicitly attach to its parent's propagated trace context.
    """
    instrument, spans, _ = telemetry
    with instrument.operation("parent"):
        environment = instrument.propagation_environment()
        with instrument.tracer.start_as_current_span("child", context=instrument.parent_context(environment)):
            pass
    child, parent = spans.get_finished_spans()
    assert child.context.trace_id == parent.context.trace_id and child.parent.span_id == parent.context.span_id
    assert set(environment) <= {"TRACEPARENT", "TRACESTATE"}


def test_explicitly_disabled_otlp_does_not_create_providers(monkeypatch):
    """
    Standard SDK disablement prevents both exporters, even with export requested explicitly.
    """
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    disabled = Telemetry.otlp(service_name="example")
    assert not disabled._owned
    disabled.close()


def test_plan_reconciliation_retains_proposing_strategy_trace(telemetry):
    """
    Delayed reconciliation preserves the proposal's causal parent and normal blocking is not an error.
    """
    instrument, spans, _ = telemetry
    owner = ProcessSupervisor(
        [ProcessPlan("empty", ())],
        view=lambda: Environment(None, {}, {}, False, "expired"),
        activate=lambda _: None,
        max_processes=1,
        telemetry=instrument,
    )
    try:
        with instrument.operation("adaptation.strategy"):
            owner.propose("empty")
        results = []
        thread = threading.Thread(target=lambda: results.append(owner.reconcile()))
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive() and results[0].state == "Blocked"
        parent, plan = spans.get_finished_spans()
        assert plan.parent.span_id == parent.context.span_id
        assert plan.attributes["plan.state"] == "Blocked" and plan.status.status_code.name != "ERROR"
    finally:
        owner.close()


def test_otlp_creation_uses_private_providers_and_projected_identity(monkeypatch):
    """
    Explicit configuration wires both signals and shuts them down without replacing global providers.
    """
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http import metric_exporter, trace_exporter
    from opentelemetry.sdk.metrics import export

    from polyad_sdk import PodContext, WorkloadContext
    from polyad_types import ServiceEndpoint

    spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda: spans)
    monkeypatch.setattr(metric_exporter, "OTLPMetricExporter", MagicMock())
    monkeypatch.setattr(export, "PeriodicExportingMetricReader", lambda _: reader)
    global_tracer, global_meter = trace.get_tracer_provider(), metrics.get_meter_provider()
    context = WorkloadContext(
        ServiceEndpoint("", "app", "Graph", "pipeline", "uid", "consumer"), pod=PodContext(name="consumer-0", namespace="app")
    )
    instrument = Telemetry.otlp(service_name="consumer", context=context)
    providers = tuple(instrument._owned)
    with instrument.operation("startup"):
        pass
    assert providers[0].force_flush()
    recorded = spans.get_finished_spans()[0]
    assert recorded.resource.attributes["service.name"] == "consumer"
    assert recorded.resource.attributes["k8s.pod.name"] == "consumer-0"
    assert reader.get_metrics_data() is not None
    instrument.close()
    instrument.close()
    assert not instrument._owned
    assert trace.get_tracer_provider() is global_tracer and metrics.get_meter_provider() is global_meter
