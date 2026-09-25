"""
Exercise operator decisions through readable console output and real OTel log records.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
from unittest.mock import AsyncMock, Mock

import pytest
from kubernetes.client.exceptions import ApiException
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON

from polyad.exceptions.policies import PolicyViolation
from polyad.operator.observability import logging as diagnostics
from polyad.operator.observability import tracing
from polyad.operator.observability.decisions import decision, decision_context
from polyad.operator.policies.graph_policies import check_policies
from polyad.operator.reconciliation.controller import Controller
from tests.test_chart import render
from tests.test_operator import FakeAPI, resource
from tests.test_replication import group
from tests.test_root_control_plane import ManagementAPI, manager, scale_request


@pytest.mark.parametrize("sampled", [False, True])
def test_decision_logs_have_otel_context_and_survive_trace_sampling(monkeypatch, caplog, sampled):
    """
    Export one structured event, preserve its readable body and omit resource payloads.
    """
    exporter = InMemoryLogRecordExporter()
    factory = Mock(return_value=exporter)
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter", factory)
    monkeypatch.setenv("POLYAD_LOGS_ENABLED", "true")
    monkeypatch.setenv("POLYAD_TRACING_ENABLED", "false")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "operator-test")
    monkeypatch.setenv("POLYAD_POD_CLUSTER", "west")
    monkeypatch.setenv("POLYAD_POD_UID", "pod-123")
    provider = TracerProvider(sampler=ALWAYS_ON if sampled else ALWAYS_OFF, shutdown_on_exit=False)
    monkeypatch.setattr(tracing, "_provider", provider)
    obj = resource("OperatorPool", "west", {"cluster": "west", "password": "private-payload"})
    output = io.StringIO()
    console = logging.StreamHandler(output)
    console.setFormatter(diagnostics.DecisionFormatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("polyad")
    logger.addHandler(console)
    try:
        with caplog.at_level(logging.INFO, logger="polyad"):
            diagnostics.configure_log_export()
            diagnostics.configure_log_export()
            factory.assert_called_once()
            with tracing.span("pool.reconcile") as active:
                context = active.get_span_context()
                decision(
                    "polyad.remote_scale.conflict",
                    "A local edit takes precedence over this remote request.",
                    obj=obj,
                    outcome="blocked",
                    reason="local_edit_wins",
                    level=logging.WARNING,
                )
            diagnostics._provider.force_flush()
        exported = exporter.get_finished_logs()
        assert len(exported) == 1
        record = exported[0].log_record
        assert record.event_name == "polyad.remote_scale.conflict"
        assert record.severity_number.value == 13 and record.severity_text == "WARN"
        assert record.timestamp > 0 and record.observed_timestamp >= record.timestamp
        assert record.trace_id == context.trace_id and record.span_id == context.span_id
        assert bool(record.trace_flags.sampled) is sampled
        assert record.attributes["polyad.decision.reason"] == "local_edit_wins"
        assert record.attributes["polyad.resource.uid"] == obj["metadata"]["uid"]
        assert record.attributes["polyad.target.cluster"] == "west"
        assert exported[0].resource.attributes["service.name"] == "operator-test"
        assert exported[0].resource.attributes["k8s.cluster.name"] == "west"
        assert exported[0].resource.attributes["k8s.pod.uid"] == "pod-123"
        assert exported[0].resource.attributes["service.instance.id"] == tracing.telemetry_resource().attributes["service.instance.id"]
        assert f"trace_id={context.trace_id:032x}" in output.getvalue()
        assert "A local edit takes precedence" in output.getvalue()
        assert "private-payload" not in str(record.attributes) + output.getvalue()
    finally:
        diagnostics.shutdown_log_export()
        diagnostics.shutdown_log_export()
        logger.removeHandler(console)
        provider.shutdown()


@pytest.mark.parametrize("enabled,disabled", [("false", "false"), ("true", "true")])
def test_disabled_log_export_does_not_construct_an_exporter(monkeypatch, enabled, disabled):
    """
    Honor both Polyad opt-in and the SDK kill switch without allocating export workers.
    """
    monkeypatch.setenv("POLYAD_LOGS_ENABLED", enabled)
    monkeypatch.setenv("OTEL_SDK_DISABLED", disabled)
    factory = Mock()
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http._log_exporter.OTLPLogExporter", factory)
    diagnostics.configure_log_export()
    factory.assert_not_called()
    assert diagnostics._provider is None


def test_log_export_protocol_is_validated_before_starting(monkeypatch):
    """
    Reject a protocol the installed exporter cannot speak.
    """
    monkeypatch.setenv("POLYAD_LOGS_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "grpc")
    with pytest.raises(ValueError, match="http/protobuf"):
        diagnostics.configure_log_export()
    assert diagnostics._provider is None


def test_status_heartbeats_do_not_repeat_decisions_and_failed_writes_claim_no_success(caplog):
    """
    Emit transitions only after committed changes, omitting raw external status messages.
    """

    async def run():
        obj = resource("Graph", "graph")
        api = FakeAPI(obj)
        controller = Controller(api)
        await controller.status(obj, {"phase": "Waiting", "message": "private-payload"})
        latest = await api.get("Graph", "test", "graph")
        await controller.status(latest, {"phase": "Waiting", "metricsObservedAt": "now"})
        api.request = AsyncMock(side_effect=ApiException(status=409, reason="private-payload"))
        with pytest.raises(ApiException):
            await controller.status(latest, {"phase": "Ready"})

    with caplog.at_level(logging.INFO, logger="polyad"):
        asyncio.run(run())
    assert len(caplog.records) == 1
    assert "Waiting" in caplog.text and "Ready" not in caplog.text and "private-payload" not in caplog.text


def test_structural_conflict_explains_the_rejected_rule_and_boundary(caplog):
    """
    Preserve the rule and constraint explanation without dumping graph specifications.
    """
    rule = resource("GraphPolicy", "zero-nodes", {"enforcement": "Namespace", "limits": {"nodes": 0}})
    spec = {"mode": "persistent", "nodes": [{"name": "worker", "kind": "Daemon", "ref": "worker"}]}
    with caplog.at_level(logging.WARNING, logger="polyad"), pytest.raises(PolicyViolation):
        asyncio.run(check_policies(FakeAPI(rule), "test", "Graph", spec))
    record = caplog.records[-1]
    assert record.event_name == "polyad.policies.rejected"
    assert "zero-nodes" in record.getMessage() and "nodes" in record.getMessage()
    assert record.polyad_attributes["polyad.boundary.path"] == "root"


def test_local_edit_conflict_identifies_the_request_and_competing_generation(caplog):
    """
    Distinguish rejected remote requests by UID and expected versus observed generation.
    """
    target = group(2)
    request = scale_request(target, 3)
    target["metadata"]["generation"] += 1
    root, remote = ManagementAPI(request), ManagementAPI(target)
    with caplog.at_level(logging.WARNING, logger="polyad"), pytest.raises(ValueError, match="local ReplicaGroup edits"):
        asyncio.run(manager(root, remote).scale(request))
    record = caplog.records[-1]
    assert record.event_name == "polyad.remote_scale.conflict"
    assert record.polyad_attributes["polyad.resource.uid"] == request["metadata"]["uid"]
    assert record.polyad_attributes["polyad.generation.observed"] > record.polyad_attributes["polyad.generation.expected"]
    assert not remote.calls


def test_decision_context_isolated_between_concurrent_cluster_tasks(caplog):
    """
    Keep identically named graph decisions attached to the correct cluster and worker.
    """

    async def run(cluster):
        token = decision_context.set({"polyad.target.cluster": cluster, "polyad.replica.id": cluster + "-worker"})
        try:
            await asyncio.sleep(0)
            decision("polyad.test", "Checked a graph.", key=("Graph", "test", "same-name"), outcome="allowed", reason="test")
        finally:
            decision_context.reset(token)

    async def scenario():
        await asyncio.gather(run("west"), run("east"))

    with caplog.at_level(logging.INFO, logger="polyad"):
        asyncio.run(scenario())
    assert {record.polyad_attributes["polyad.target.cluster"] for record in caplog.records} == {"west", "east"}
    assert decision_context.get() is None


@pytest.mark.parametrize(("ha", "mode"), [(False, "Dense"), (True, "Dense"), (True, "Distributed")])
def test_log_only_export_reaches_operators_and_observers(ha, mode):
    """
    Render independent logs, collector credentials and Pod identity in every process template.
    """
    objects = render(
        f"ha={str(ha).lower()}",
        f"architecture.mode={mode}",
        "api.enabled=true",
        "metrics.enabled=true",
        "observer.enabled=true",
        "observer.existingSecret=observer-token",
        "global.multiCluster.clusterName=east",
        "tracing.enabled=false",
        "tracing.logs.enabled=true",
        "tracing.headersSecret=otel-credentials",
    )
    pods = [
        obj["spec"]["template"]["spec"]
        for obj in objects
        if (obj["kind"] == "Deployment" and obj["metadata"]["name"] in {"test-polyad", "test-polyad-observer"}) or obj["kind"] == "Daemon"
    ]
    assert pods
    for pod in pods:
        container = pod["containers"][0]
        env = {item["name"]: item for item in container["env"]}
        assert env["POLYAD_TRACING_ENABLED"]["value"] == "false"
        assert env["POLYAD_LOGS_ENABLED"]["value"] == "true"
        assert env["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"]["value"].endswith("/v1/logs")
        assert env["OTEL_EXPORTER_OTLP_LOGS_HEADERS"]["valueFrom"]["secretKeyRef"]["name"] == "otel-credentials"
        assert env["POLYAD_POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" not in env


def test_plain_console_configuration_remains_available_without_export(monkeypatch):
    """
    Keep startup configuration usable for demos and disconnected operators.
    """
    basic = Mock()
    monkeypatch.setattr(logging, "basicConfig", basic)
    diagnostics.configure_logging(argparse.ArgumentParser(), "INFO")
    assert isinstance(basic.call_args.kwargs["handlers"][0].formatter, diagnostics.DecisionFormatter)
