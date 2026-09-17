"""
Verify trace propagation, bounded metadata, export lifecycle and deployment configuration.
"""

from __future__ import annotations

import asyncio
from threading import Thread
from unittest.mock import Mock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from polyad.api.application import Routes, create_application
from polyad.operator.adapters.kubernetes import API
from polyad.operator.observability import tracing
from tests.test_chart import render


@pytest.fixture
def spans(monkeypatch):
    """
    Collect real SDK spans without configuring a network exporter or global provider.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_provider", provider)
    yield exporter
    provider.shutdown()


def test_http_trace_parent_routes_and_sensitive_data(spans):
    """
    Continue incoming traces once, retaining route templates instead of user data.
    """
    app = create_application()
    routes = Routes("composition", app)
    routes.add_url_rule("/v1/items/<name>", view_func=lambda name: {"secret": name}, methods=["POST"])
    routes.finish()
    parent = "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
    with app.test_client() as client:
        result = client.post(
            "/v1/items/private-payload?token=private-payload",
            headers={"traceparent": parent, "Authorization": "Bearer private-payload", "baggage": "token=private-payload"},
            json={"token": "private-payload"},
        )
        assert result.status_code == 200
    recorded = spans.get_finished_spans()
    assert len(recorded) == 1
    current = recorded[0]
    assert current.name == "POST /v1/items/<name>"
    assert current.context.trace_id == int("1234567890abcdef1234567890abcdef", 16)
    assert current.parent.span_id == int("1234567890abcdef", 16)
    assert current.kind == trace.SpanKind.SERVER
    assert current.attributes["http.response.status_code"] == 200
    assert "private-payload" not in current.to_json()
    assert not trace.get_current_span().get_span_context().is_valid


def test_http_failures_unknown_paths_and_scrape_exclusion(spans):
    """
    Preserve HTTP behavior and report errors without exception messages or raw URLs.
    """
    app = create_application()
    app.config["PROPAGATE_EXCEPTIONS"] = False
    routes = Routes("composition", app)

    def fail():
        raise ValueError("private-payload")

    routes.add_url_rule("/fail", view_func=fail)
    routes.add_url_rule("/metrics", view_func=lambda: "metrics")
    routes.finish()
    with app.test_client() as client:
        assert client.get("/fail").status_code == 500
        assert client.get("/private-payload").status_code == 404
        assert client.get("/metrics").status_code == 200
    failed, missing = spans.get_finished_spans()
    assert failed.status.status_code == trace.StatusCode.ERROR
    assert failed.attributes["error.type"] == "ValueError"
    assert not failed.events
    assert "private-payload" not in failed.to_json() + missing.to_json()
    assert missing.attributes["http.response.status_code"] == 404


def test_awaited_kubernetes_operations_are_child_spans(spans):
    """
    Preserve async parentage, results and exceptions while omitting Kubernetes bodies.
    """
    api = API.__new__(API)
    api.client = Mock()
    api.client.call_api.return_value = {"data": {"token": "private-payload"}}

    async def scenario():
        with tracing.span("test.parent"):
            assert await api.request("GET", "Secret", "test", "credentials") == {"data": {"token": "private-payload"}}
            api.client.call_api.side_effect = RuntimeError("private-payload")
            with pytest.raises(RuntimeError, match="private-payload"):
                await api.request("GET", "Secret", "test", "credentials")

    asyncio.run(scenario())
    success, failure, parent = spans.get_finished_spans()
    for child in (success, failure):
        assert child.parent.span_id == parent.context.span_id
        assert child.attributes["k8s.resource.kind"] == "Secret"
        assert child.kind == trace.SpanKind.CLIENT
        assert "private-payload" not in child.to_json()
    assert failure.status.status_code == trace.StatusCode.ERROR


def test_context_crosses_http_thread_to_operator_loop(spans):
    """
    Keep submitted async work attached to the originating request across threads.
    """
    loop = asyncio.new_event_loop()
    thread = Thread(target=loop.run_forever)
    thread.start()

    @tracing.traced("operator.work")
    async def work():
        await asyncio.sleep(0)
        return "done"

    try:
        with tracing.span("http.request"):
            assert asyncio.run_coroutine_threadsafe(work(), loop).result(timeout=5) == "done"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
    child, parent = spans.get_finished_spans()
    assert child.parent.span_id == parent.context.span_id
    assert child.context.trace_id == parent.context.trace_id


def test_zero_sampling_and_protocol_validation(monkeypatch):
    """
    Honor zero root sampling and fail clearly for unsupported exporter protocols.
    """
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setenv("POLYAD_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "grpc")
    with pytest.raises(ValueError, match="http/protobuf"):
        tracing.configure_tracing()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf")
    exporter = InMemorySpanExporter()
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", lambda: exporter)
    try:
        tracing.configure_tracing()
        with tracing.span("unsampled") as active:
            assert not active.is_recording()
        tracing.shutdown_tracing()
        assert not exporter.get_finished_spans()
    finally:
        tracing.shutdown_tracing()


@pytest.mark.parametrize("enabled,disabled", [(False, False), (True, True)])
def test_disabled_tracing_creates_no_exporter(monkeypatch, enabled, disabled):
    """
    Keep default operation and the standard SDK kill switch free of trace exports.
    """
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setenv("POLYAD_TRACING_ENABLED", str(enabled).lower())
    monkeypatch.setenv("OTEL_SDK_DISABLED", str(disabled).lower())
    exporter = Mock()
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", exporter)
    tracing.configure_tracing()
    with tracing.span("disabled") as active:
        assert not active.is_recording()
    tracing.shutdown_tracing()
    exporter.assert_not_called()


def test_provider_is_created_once_and_flushes_on_shutdown(monkeypatch):
    """
    Batch completed spans, honor SDK sampling and release the single owned exporter.
    """
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setenv("POLYAD_TRACING_ENABLED", "true")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "test-operator")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
    exporter = InMemorySpanExporter()
    factory = Mock(return_value=exporter)
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", factory)
    try:
        tracing.configure_tracing()
        tracing.configure_tracing()
        factory.assert_called_once()
        with tracing.span("queued"):
            pass
        tracing.shutdown_tracing()
        tracing.shutdown_tracing()
        assert exporter.get_finished_spans()[0].resource.attributes["service.name"] == "test-operator"
    finally:
        tracing.shutdown_tracing()


@pytest.mark.parametrize(("ha", "mode"), [(False, "Dense"), (True, "Dense"), (True, "Distributed")])
def test_chart_tracing_reaches_components_and_observers(ha, mode):
    """
    Pass typed trace settings and Secret references to every operator process template.
    """
    objects = render(
        f"ha={str(ha).lower()}",
        f"architecture.mode={mode}",
        "api.enabled=true",
        "metrics.enabled=true",
        "observer.enabled=true",
        "observer.existingSecret=observer-token",
        "global.multiCluster.clusterName=east",
        "tracing.enabled=true",
        "tracing.samplingRatio=0.25",
        "tracing.headersSecret=trace-credentials",
    )
    pods = [
        obj["spec"]["template"]["spec"]
        for obj in objects
        if (obj["kind"] == "Deployment" and obj["metadata"]["name"] in {"test-polyad", "test-polyad-observer"}) or obj["kind"] == "Daemon"
    ]
    assert len(pods) == (5 if mode == "Distributed" else 2)
    for pod in pods:
        env = {item["name"]: item for item in pod["containers"][0]["env"]}
        assert env["POLYAD_TRACING_ENABLED"]["value"] == "true"
        assert env["OTEL_TRACES_SAMPLER_ARG"]["value"] == "0.25"
        assert env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"]["valueFrom"]["secretKeyRef"] == {
            "name": "trace-credentials",
            "key": "headers",
        }
