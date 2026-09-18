"""
Verify collector discovery, signal routing, credentials and isolation using rendered charts.
"""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from tests.test_chart import CHART, render
from tests.test_local_services import inventory


def collectors(*settings, mode="Alloy", values_files=()):
    """
    Render the documented collector profile with test-specific cluster settings.
    """
    profile = "telemetry" if mode == "Alloy" else "prometheus-agent"
    return render(
        *settings,
        values_files=(str(CHART / f"values-{profile}.reference.yaml"), *values_files),
    )


def indexed(objects):
    """
    Index native manifests without conflating CRDs or workload namespaces.
    """
    return {(item["kind"], item["metadata"]["name"]): item for item in objects}


@pytest.mark.parametrize("mode", ["Alloy", "PrometheusAgent"])
@pytest.mark.parametrize("architecture", ["Dense", "Distributed"])
def test_collectors_cover_enabled_services_and_join_atlas(mode, architecture):
    """
    Every rendered exporter has a valid selector and collectors join the reserved inventory.
    """
    objects = collectors(
        f"architecture.mode={architecture}",
        mode=mode,
        values_files=("root-values.yaml", "local-services-values.yaml"),
    )
    resources = indexed(objects)
    services = [item for item in objects if item["kind"] == "Service" and item["metadata"]["name"].startswith("test-telemetry-")]
    assert {item["metadata"]["name"] for item in services} >= {
        "test-telemetry-operator",
        "test-telemetry-agent",
        "test-telemetry-cache",
        "test-telemetry-state",
        "test-telemetry-dragonflyoperator-0",
        "test-telemetry-kedaoperator-0",
        "test-telemetry-kedaoperator-1",
        "test-telemetry-kedaoperator-2",
        "test-telemetry-istiod-0",
        "test-telemetry-istioingress-0",
    }
    config = resources["ConfigMap", "test-telemetry"]["data"]
    text = next(iter(config.values()))
    for service in services:
        name = service["metadata"]["name"]
        if name.endswith("-otlp"):
            continue
        assert name in text
        assert service["spec"]["selector"]
    selector = resources["Service", "test-telemetry-operator"]["spec"]["selector"]
    if architecture == "Distributed":
        assert selector["polyad.astrivant.com/component"] == "telemetry"
    group = inventory(objects)["collectors"]
    assert {("StatefulSet", "test-telemetry"), ("Service", "test-telemetry-agent")} <= {(obj["kind"], obj["name"]) for obj in group}
    assert all(set(obj) == {"kind", "namespace", "name"} for obj in group)
    pod = resources["StatefulSet", "test-telemetry"]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"]
    assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"]
    assert any("[$(POD_IP)]" in arg for arg in pod["containers"][0]["args"])
    if mode == "PrometheusAgent":
        assert "--agent" in pod["containers"][0]["args"]
        assert len(yaml.safe_load(text)["scrape_configs"]) == len(services)
        assert "test-telemetry-otlp" not in {item["metadata"]["name"] for item in services}
    else:
        assert "loki.source.kubernetes" in text
        assert "otelcol.receiver.otlp" in text
        assert "clustering { enabled = true }" in text


def test_metrics_credentials_and_trace_routing_use_secret_references():
    """
    Scrape credentials and exporter credentials remain mounted files, not ConfigMap data.
    """
    objects = collectors(
        "metrics.authentication.enabled=true",
        "metrics.authentication.existingSecret=metrics-access",
        "telemetry.remoteWrite.credentials.secretName=monitoring-access",
        "telemetry.remoteWrite.credentials.username=metrics-user",
        "telemetry.logs.credentials.secretName=monitoring-access",
        "telemetry.traces.credentials.secretName=monitoring-access",
    )
    resources = indexed(objects)
    pod = resources["StatefulSet", "test-telemetry"]["spec"]["template"]["spec"]
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["operator-credentials"]["secret"]["secretName"] == "metrics-access"
    assert volumes["metrics-credentials"]["secret"]["secretName"] == "monitoring-access"
    assert "monitoring-access" not in json.dumps(resources["ConfigMap", "test-telemetry"]["data"])
    root = resources["Deployment", "test-polyad"]["spec"]["template"]["spec"]
    env = {item["name"]: item.get("value") for item in root["containers"][0]["env"]}
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "http://test-telemetry-otlp.test.svc:4318/v1/traces"
    rules = [
        rule for obj in objects if obj["kind"] == "Role" and obj["metadata"]["name"].startswith("test-telemetry") for rule in obj["rules"]
    ]
    assert all("secrets" not in rule["resources"] for rule in rules)
    assert all(set(rule["verbs"]) <= {"get", "list", "watch"} for rule in rules)


def test_collection_network_policies_preserve_database_and_dependency_ingress():
    """
    Scraping must not accidentally isolate otherwise unisolated database or webhook Pods.
    """
    objects = collectors(
        "networkPolicy.enabled=true",
        "networkPolicy.extraEgress[0].to[0].ipBlock.cidr=10.0.0.2/32",
        "networkPolicy.apiServerCIDRs[0]=10.0.0.1/32",
        "mesh.enabled=true",
        "mesh.operator.enabled=true",
        values_files=("root-values.yaml", "local-services-values.yaml"),
    )
    policies = [obj for obj in objects if obj["kind"] == "NetworkPolicy" and obj["metadata"]["name"].startswith("test-telemetry")]
    assert {obj["metadata"]["name"] for obj in policies} == {
        "test-telemetry",
        "test-telemetry-operator",
        "test-telemetry-cache",
        "test-telemetry-operator-proxy",
    }
    rules = indexed(objects)["AuthorizationPolicy", "test-polyad"]["spec"]["rules"]
    assert any("test-telemetry" in json.dumps(rule) and "/metrics" in json.dumps(rule) for rule in rules)


@pytest.mark.parametrize(
    "setting",
    [
        "telemetry.replicas=2",
        "telemetry.logs.enabled=true",
        "telemetry.traces.enabled=true",
        "telemetry.scrapeTimeoutSeconds=100",
        "telemetry.remoteWrite.url=",
    ],
)
def test_prometheus_rejects_duplicate_scraping_and_invalid_signal_configuration(setting):
    """
    Metrics-only mode rejects silent signal loss and duplicated unsharded writers.
    """
    with pytest.raises(subprocess.CalledProcessError):
        collectors(setting, mode="PrometheusAgent")
