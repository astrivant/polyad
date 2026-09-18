"""
Verify Pod-only health binding and kubelet-resolved context across deployment roles.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from polyad.compiler.passes.identity import pod_environment
from polyad.operator import runtime
from polyad.operator.lifecycle import probes
from polyad.operator.observability.tracing import telemetry_resource
from tests.test_chart import render


@pytest.mark.parametrize("address,url", [("10.23.4.5", "http://10.23.4.5:8080/healthz"), ("fd00::5", "http://[fd00::5]:8080/healthz")])
def test_runtime_binds_kopf_health_only_to_downward_pod_ip(monkeypatch, address, url):
    """
    Pass a concrete IPv4 or IPv6 listener to Kopf without starting another server.
    """
    monkeypatch.setenv("POLYAD_POD_IP", address)
    monkeypatch.setattr("sys.argv", ["polyad"])
    monkeypatch.setattr(runtime.signal, "signal", Mock())
    instance = SimpleNamespace(start=Mock(), join=Mock(), stop=Mock(), thread=SimpleNamespace(is_alive=lambda: False))
    constructor = Mock(return_value=instance)
    monkeypatch.setattr(runtime, "OperatorThread", constructor)
    runtime.main()
    assert constructor.call_args.kwargs["liveness_endpoint"] == url
    instance.start.assert_called_once()
    assert probes.health_endpoint() == url


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://0.0.0.0:8080/healthz",
        "http://[::]:8080/healthz",
        "http://127.0.0.1:8080/healthz",
        "http://10.23.4.6:8080/healthz",
        "http://example:8080/healthz",
        "https://10.23.4.5:8080/healthz",
    ],
)
def test_health_override_cannot_broaden_or_redirect_bind(monkeypatch, endpoint):
    """
    Reject wildcard interfaces and alternate hosts even when explicitly requested by CLI.
    """
    monkeypatch.setenv("POLYAD_POD_IP", "10.23.4.5")
    with pytest.raises(ValueError):
        probes.health_endpoint(endpoint)
    assert probes.health_endpoint("http://10.23.4.5:8099/live") == "http://10.23.4.5:8099/live"


def test_pod_ip_is_required_in_cluster_and_loopback_is_local_only(monkeypatch):
    """
    Do not silently bind loopback after a missing Downward API projection in Kubernetes.
    """
    monkeypatch.delenv("POLYAD_POD_IP", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    with pytest.raises(ValueError, match="status.podIP"):
        probes.health_endpoint()
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST")
    assert probes.health_endpoint() == "http://127.0.0.1:8080/healthz"
    for address in ["0.0.0.0", "::", "224.0.0.1", "not-an-ip"]:
        monkeypatch.setenv("POLYAD_POD_IP", address)
        with pytest.raises(ValueError):
            probes.health_endpoint()


def test_probe_uses_pod_ip_without_environment_proxies(monkeypatch):
    """
    Docker, readiness and administrator probes use the listener's address and bypass proxies.
    """
    monkeypatch.setenv("POLYAD_POD_IP", "fd00::5")
    response = Mock()
    response.read.return_value = json.dumps({"scheduler": {"worker": True}})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    opener = Mock()
    opener.open.return_value = response
    factory = Mock(return_value=opener)
    monkeypatch.setattr(probes, "build_opener", factory)
    assert probes.read_health() == {"scheduler": {"worker": True}}
    assert factory.call_args.args[0].proxies == {}
    opener.open.assert_called_once_with("http://[fd00::5]:8080/healthz", timeout=2)


@pytest.mark.parametrize("missing", [None, "initialized", "worker", "apiFresh", "cacheFresh", "attached", "draining"])
def test_readiness_retains_dependency_and_drain_checks(monkeypatch, missing):
    """
    Binding changes must preserve scheduler, root-attachment and connection-drain readiness.
    """
    monkeypatch.setattr(probes.Path, "exists", lambda _: missing == "draining")
    scheduler = dict.fromkeys(("initialized", "worker", "apiFresh", "cacheFresh", "attached"), True)
    if missing and missing != "draining":
        scheduler[missing] = False
    if missing:
        with pytest.raises(RuntimeError):
            probes.check_readiness({"scheduler": scheduler})
    else:
        probes.check_readiness({"scheduler": scheduler})


@pytest.mark.parametrize(
    "settings,values",
    [
        ((), ()),
        (("ha=true",), ()),
        (("ha=true", "architecture.mode=Distributed", "api.enabled=true", "metrics.enabled=true"), ()),
        (("observer.enabled=true", "observer.existingSecret=observer-token", "global.multiCluster.clusterName=east"), ()),
        ((), ("worker-values.yaml",)),
    ],
)
def test_all_operator_profiles_get_context_without_telemetry(settings, values):
    """
    Cover dense, split, observer and attached-worker Pods with no duplicate context variables.
    """
    objects = render(*settings, values_files=values)
    pods = [obj["spec"]["template"] for obj in objects if obj["kind"] in {"Deployment", "Daemon"}]
    checked = 0
    for pod in pods:
        for container in pod["spec"]["containers"]:
            if container["name"] not in {"operator", "observer"}:
                continue
            checked += 1
            env = {item["name"]: item for item in container["env"]}
            assert len(env) == len(container["env"])
            assert env["POLYAD_TRACING_ENABLED"]["value"] == "false"
            assert env["POLYAD_LOGS_ENABLED"]["value"] == "false"
            assert all(env[item["name"]] == item for item in pod_environment())
            if container["name"] == "operator":
                assert not any("--liveness=" in arg for arg in container["args"])
                assert container["readinessProbe"]["exec"]["command"] == ["python", "-m", "polyad.operator.lifecycle.probes", "--ready"]
                for name in ("startupProbe", "livenessProbe"):
                    assert "host" not in container[name]["httpGet"]
    assert checked


def test_telemetry_uses_actual_kubernetes_node_identity(monkeypatch):
    """
    Add standard node context independently of graph vertex identity.
    """
    monkeypatch.setenv("POLYAD_KUBERNETES_NODE_NAME", "worker-pool-3")
    monkeypatch.setenv("POLYAD_NODE_NAME", "graph-vertex")
    assert telemetry_resource().attributes["k8s.node.name"] == "worker-pool-3"
