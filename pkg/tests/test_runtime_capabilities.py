"""
Exercise Helm-selected startup in isolated interpreters to detect transitive imports.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.test_chart import render

STARTUP = """
import asyncio, json, sys
from unittest.mock import Mock, patch
import kopf
from polyad.operator.lifecycle import handlers
from polyad.operator.observability.metrics import WriteBacklog
from polyad.operator.lifecycle.roles import serves
from polyad.operator.observability.tracing import configure_tracing, shutdown_tracing

async def main():
    configure_tracing()
    api = Mock()
    api.writes = WriteBacklog()
    with patch.object(handlers, 'API', return_value=api):
        if any(serves(f) for f in ('API', 'EVENTS', 'CONNECTIONS', 'METRICS')):
            from polyad.api.server import APIServer
            with patch.object(APIServer, 'start'):
                await handlers.startup(kopf.OperatorSettings())
        else:
            await handlers.startup(kopf.OperatorSettings())
    result = {
        'modules': sorted(sys.modules),
        'ports': handlers.http.ports if handlers.http else {},
        'websockets': handlers.http.websockets if handlers.http else False,
        'publishes_events': handlers.events is not None,
        'controller': handlers.controller is not None,
        'postgres': handlers.state is not None,
    }
    for task in handlers.background:
        task.cancel()
    handlers.queue.task.cancel()
    await asyncio.gather(*handlers.background, handlers.queue.task, return_exceptions=True)
    if handlers.http:
        await handlers.http.close()
    if handlers.events:
        await handlers.events.close()
    await handlers.shared.close()
    if handlers.state:
        await handlers.state.close()
    shutdown_tracing()
    print(json.dumps(result))

asyncio.run(main())
"""


def probe(environment, code=STARTUP):
    """
    Use a clean process so imports from other tests cannot hide unwanted loading.
    """
    clean = {key: value for key, value in os.environ.items() if not key.startswith(("POLYAD_", "OTEL_"))}
    clean.update(POLYAD_CACHE_URL="redis://localhost:6379/0", POLYAD_NAMESPACE="test")
    clean.update(environment)
    result = subprocess.run([sys.executable, "-c", code], env=clean, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def environment_for(obj):
    """
    Preserve the rendered chart's literal capability flags without mounting real credentials.
    """
    pod = obj["spec"]["template"]
    items = pod["spec"]["containers"][0]["env"]
    assert len({item["name"] for item in items}) == len(items)
    return {item["name"]: item["value"] for item in items if "value" in item and not item["name"].endswith("_FILE")}


def assert_absent(modules, *prefixes):
    """
    Reject loading a disabled package or any of its submodules.
    """
    assert not [name for name in modules if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)]


@pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm")
def test_minimal_helm_startup_avoids_optional_services():
    """
    The default execution role retains graph scheduling without optional service imports.
    """
    objects = render("metrics.enabled=false", "api.enabled=false", "events.enabled=false", "connections.enabled=false")
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    environment = environment_for(deployment)
    assert environment["POLYAD_API_ENABLED"] == environment["POLYAD_EVENTS_ENABLED"] == "false"
    result = probe(environment)
    assert result["controller"] and not result["ports"] and not result["publishes_events"]
    assert_absent(
        result["modules"],
        "flask",
        "waitress",
        "websockets",
        "hypercorn",
        "flask_httpauth",
        "flask_limiter",
        "psycopg",
        "psycopg_pool",
        "prometheus_client",
        "opentelemetry.sdk",
        "opentelemetry.exporter",
        "polyad.operator.clusters.root",
        "polyad.operator.coordination.dragonfly",
        "polyad.events.store",
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm")
def test_split_helm_components_only_import_their_endpoint_families():
    """
    One shared values file loads HTTP in gateway/telemetry and event producers in executors.
    """
    objects = render(
        "ha=true",
        "architecture.mode=Distributed",
        "api.enabled=true",
        "events.enabled=true",
        "events.websockets.enabled=true",
        "metrics.enabled=true",
        "connections.enabled=true",
        "authentication.mode=Disabled",
    )
    expected = {"gateway": {"composition", "events", "connections"}, "telemetry": {"metrics"}, "executor": set()}
    for component, ports in expected.items():
        daemon = next(obj for obj in objects if obj["kind"] == "Daemon" and obj["metadata"]["name"] == f"test-{component}")
        result = probe(environment_for(daemon))
        assert set(result["ports"]) == ports
        assert result["controller"] == (component == "executor")
        assert result["publishes_events"] == (component in {"gateway", "executor"})
        assert result["websockets"] == (component == "gateway")
        # The mocked start never serves sockets: imports remain lazy until transport startup.
        assert_absent(result["modules"], "hypercorn", "websockets")
        assert_absent(result["modules"], "psycopg", "flask_limiter", "flask_httpauth", "opentelemetry.sdk")
        if component == "executor":
            assert_absent(result["modules"], "flask", "waitress", "prometheus_client")
        if component == "gateway":
            assert_absent(result["modules"], "polyad.metrics.builder", "prometheus_client")
        if component == "telemetry":
            assert_absent(result["modules"], "polyad.api.builder", "polyad.events.builder", "polyad.api.connections.app")


@pytest.mark.parametrize("backend", ["Builtin", "FlaskHTTPAuth"])
@pytest.mark.parametrize("limited", [False, True])
def test_selected_authentication_and_limits_load_only_required_extensions(backend, limited):
    """
    Backend and quota flags select imports while preserving authenticated HTTP behavior.
    """
    result = probe(
        {
            "POLYAD_COMPONENT": "gateway",
            "POLYAD_API_ENABLED": "true",
            "POLYAD_API_TOKEN": "test-token",
            "POLYAD_AUTH_BACKEND": backend,
            "POLYAD_API_RATE_LIMIT_ENABLED": str(limited).lower(),
        }
    )
    assert ("flask_httpauth" in result["modules"]) == (backend == "FlaskHTTPAuth")
    assert ("flask_limiter" in result["modules"]) == limited
    assert_absent(result["modules"], "psycopg", "psycopg_pool", "polyad.auth.lanes", "polyad.auth.store")


def test_enabled_database_and_tracing_are_available_without_loading_http():
    """
    Optional state and trace implementations load when enabled, independently of listeners.
    """
    result = probe(
        {
            "POLYAD_POSTGRES_ENABLED": "true",
            "POLYAD_POSTGRES_DSN": "postgresql://unused",
            "POLYAD_TRACING_ENABLED": "true",
            "OTEL_TRACES_SAMPLER": "always_off",
            "POLYAD_EVENT_PUBLICATION_ENABLED": "true",
        }
    )
    assert result["postgres"] and result["publishes_events"]
    assert {"psycopg", "psycopg_pool", "opentelemetry.sdk.trace", "opentelemetry.exporter.otlp.proto.http.trace_exporter"} <= set(
        result["modules"]
    )
    assert_absent(result["modules"], "flask", "waitress", "prometheus_client")


def test_lazy_public_exports_preserve_import_identity():
    """
    Existing consumer imports resolve to the same builders after deferring package exports.
    """
    probe(
        {},
        """
import json
from polyad.api import APIBuilder, create_app, RateLimitPolicy
from polyad.api.builder import APIBuilder as Builder
from polyad.events import EventAPIBuilder, EventStore
from polyad.events.store import EventStore as Store
from polyad.metrics import MetricsAPIBuilder
from polyad.metrics.builder import MetricsAPIBuilder as Metrics
assert APIBuilder is Builder and EventStore is Store and MetricsAPIBuilder is Metrics
print(json.dumps(True))
""",
    )


@pytest.mark.parametrize("stored", [False, True])
def test_named_key_database_is_independent_of_state_storage(tmp_path, stored):
    """
    A projected authentication DSN alone selects drivers without enabling graph persistence.
    """
    from tests.test_authentication import registry

    registry(tmp_path)
    environment = {
        "POLYAD_COMPONENT": "gateway",
        "POLYAD_API_ENABLED": "true",
        "POLYAD_AUTH_CONFIG_FILE": str(tmp_path / "config.json"),
        "POLYAD_API_RATE_LIMIT_ENABLED": "false",
    }
    if stored:
        dsn = tmp_path / "uri"
        dsn.write_text("postgresql://unused")
        environment["POLYAD_AUTH_DATABASE_DSN_FILE"] = str(dsn)
    result = probe(environment)
    assert not result["postgres"]
    assert ("psycopg" in result["modules"]) == stored
    assert ("polyad.auth.store" in result["modules"]) == stored
    assert "polyad.auth.lanes" in result["modules"]


def test_observer_imports_only_its_http_family():
    """
    Read replicas avoid importing composition, metrics and streaming HTTP builders.
    """
    result = probe(
        {},
        """
import json, sys
import polyad.operator.observer
print(json.dumps(sorted(sys.modules)))
""",
    )
    assert "polyad.api.observations" in result
    assert_absent(result, "polyad.api.builder", "polyad.events.builder", "polyad.metrics.builder", "psycopg", "opentelemetry.sdk")
