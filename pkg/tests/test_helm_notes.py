"""
Validate install notes against enabled routes without contacting a cluster.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHART = Path(__file__).resolve().parents[2] / "charts/polyad"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm and chart dependencies")


def notes(*settings):
    """
    Exercise Helm's install notes rendering with an explicitly client-only dry run.
    """
    command = ["helm", "install", "example", str(CHART), "--namespace", "apps", "--dry-run=client", "--hide-secret", "--output", "json"]
    for setting in settings:
        command.extend(["--set", setting])
    return json.loads(subprocess.check_output(command, text=True))["info"]["notes"]


def test_default_notes_do_not_advertise_disabled_apis():
    """
    A default install has health access but no public routes or API Services.
    """
    text = notes()
    assert "No external API routes are configured" in text
    assert "Composition, events and metrics Services are disabled" in text
    assert "http://127.0.0.1:8080/healthz" in text
    assert "helm get notes example -n apps" in text
    assert "/v1/" not in text and "/openapi.json" not in text


def test_internal_notes_include_enabled_services_without_credentials():
    """
    Show namespaced URLs and local access commands while keeping tokens out of notes.
    """
    text = notes(
        "api.enabled=true",
        "api.existingSecret=",
        "api.key=NOTES-TOKEN-MUST-NOT-APPEAR",
        "events.enabled=true",
        "metrics.enabled=true",
        "metrics.authentication.enabled=true",
    )
    public, internal = text.split("CLUSTER-INTERNAL ENDPOINTS")
    assert "No external API routes are configured" in public
    assert "/v1/" not in public
    for endpoint, port in (("api", 8090), ("events", 8091), ("metrics", 8092)):
        assert f"http://example-polyad-{endpoint}.apps.svc:{port}" in internal
        assert f"port-forward service/example-polyad-{endpoint} {port}:{port}" in internal
    assert "/v1/compositions/{requestId}/resources" in internal
    assert "/v1/activations/{requestId}/stop" in internal
    assert "/v1/workloads/{kind}/{name}/{metric}" in internal
    assert "Scheduler metrics (bearer authentication required)" in internal
    assert "NOTES-TOKEN-MUST-NOT-APPEAR" not in text


@pytest.mark.parametrize("tls", [False, True])
def test_created_gateway_notes_match_listener_scheme(tls):
    """
    List every configured host using the chart-created listener's actual protocol.
    """
    settings = [
        "api.enabled=true",
        "api.gateway.enabled=true",
        "api.gateway.create=true",
        "api.gateway.className=example",
        "api.gateway.hostnames[0]=api.example.com",
        "api.gateway.hostnames[1]=other.example.com",
    ]
    if tls:
        settings.append("api.gateway.tlsSecret=tls")
    public = notes(*settings).split("CLUSTER-INTERNAL ENDPOINTS")[0]
    for host in ("api.example.com", "other.example.com"):
        for path in ("/v1/compositions", "/v1/activations", "/openapi.json"):
            assert f"{'https' if tls else 'http'}://{host}{path}" in public
    assert "/v1/events" not in public and "/metrics" not in public


def test_gateway_without_hostname_uses_address_placeholder():
    """
    Do not invent a hostname before an ingress controller assigns its address.
    """
    public = notes(
        "api.enabled=true",
        "api.gateway.enabled=true",
        "api.gateway.create=true",
        "api.gateway.className=example",
    ).split("CLUSTER-INTERNAL ENDPOINTS")[0]
    assert "http://<gateway-address>/v1/activations" in public
    assert "get gateway.gateway.networking.k8s.io example-polyad-api" in public


def test_existing_gateway_notes_do_not_guess_listener_protocol():
    """
    Direct users to the correct namespace and listener on their existing Gateway.
    """
    public = notes(
        "api.enabled=true",
        "api.gateway.enabled=true",
        "api.gateway.name=shared",
        "api.gateway.namespace=ingress",
        "api.gateway.sectionName=secure",
        "api.gateway.hostnames[0]=api.example.com",
    ).split("CLUSTER-INTERNAL ENDPOINTS")[0]
    assert "<scheme>://api.example.com:<port>/v1/activations" in public
    assert "https://api.example.com" not in public
    assert "kubectl -n ingress get gateway.gateway.networking.k8s.io shared" in public
    assert "secure" in public


@pytest.mark.parametrize("composition", [False, True])
def test_istio_notes_include_events_and_their_rewritten_schema(composition):
    """
    Match public event schema rewriting and avoid advertising metrics through ingress.
    """
    public = notes(
        f"api.enabled={str(composition).lower()}",
        "events.enabled=true",
        "metrics.enabled=true",
        "mesh.enabled=true",
        "mesh.ingress.enabled=true",
        "mesh.ingress.hosts[0]=polyad.example.com",
        "mesh.ingress.tlsSecret=tls",
    ).split("CLUSTER-INTERNAL ENDPOINTS")[0]
    assert "https://polyad.example.com/v1/events" in public
    assert "https://polyad.example.com/events/openapi.json" in public
    assert ("https://polyad.example.com/v1/activations" in public) == composition
    assert ("https://polyad.example.com/openapi.json" in public) == composition
    assert "/metrics" not in public
