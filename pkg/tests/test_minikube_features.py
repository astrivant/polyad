"""
Keep the optional full lab and native node-autoscaler profiles internally consistent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_chart import render

ROOT = Path(__file__).resolve().parents[2]
FULL = ROOT / "integrations/minikube/full"
ADDON = ROOT / "integrations/minikube/autoscaler"


@pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm and chart dependencies")
def test_full_profile_renders_ha_local_root_and_authenticated_mesh():
    """
    Enable coherent single-cluster features without remote credentials or conflicting scaling owners.
    """
    objects = render(values_files=(FULL.parent / "values.yaml", FULL / "values.yaml"))
    deployment = next(item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == "test-polyad")
    pod = deployment["spec"]["template"]
    assert pod["metadata"]["labels"]["sidecar.istio.io/inject"] == "true"
    assert pod["spec"]["nodeSelector"] == {
        "polyad.astrivant.com/minikube-pool": "base",
        "polyad.astrivant.com/minikube-worker": "true",
    }
    env = {entry["name"]: entry.get("value") for entry in pod["spec"]["containers"][0]["env"]}
    for flag in ("POLYAD_API_ENABLED", "POLYAD_EVENTS_ENABLED", "POLYAD_METRICS_ENABLED", "POLYAD_CAPACITY_ENABLED", "POLYAD_VPA_ENABLED"):
        assert env[flag] == "true"
    cluster = next(item for item in objects if item["kind"] == "Cluster")
    assert cluster["spec"]["instances"] == 3
    cache = next(item for item in objects if item["kind"] == "Dragonfly")
    assert cache["spec"]["replicas"] == 2
    assert cache["spec"]["enableReplicationReadinessGate"] is True
    assert len([item for item in objects if item["kind"] == "ScaledObject"]) >= 3


def test_full_prerequisites_include_capacity_api_and_real_trace_receiver():
    """
    Required feature APIs exist before the operator attempts dependency observations.
    """
    crd = yaml.safe_load((FULL / "chart/crds/provisioningrequests.yaml").read_text())
    assert crd["metadata"]["name"] == "provisioningrequests.autoscaling.x-k8s.io"
    assert any(version["name"] == "v1" and version["served"] for version in crd["spec"]["versions"])
    values = yaml.safe_load((FULL / "chart/values.yaml").read_text())
    receiver = values["istiod"]["meshConfig"]["extensionProviders"][0]["opentelemetry"]
    assert receiver == {"service": "polyad-telemetry-otlp.polyad.svc.cluster.local", "port": 4317}


def test_autoscaler_ceiling_and_safe_startup_defaults():
    """
    Cap VM memory independently of Pod requests and avoid removed upstream flags.
    """
    config = json.loads((ADDON / "config.example.json").read_text())
    assert config["minWorkers"] == 0
    assert (3 + config["maxWorkers"]) * 4096 == config["maxTotalMemoryMiB"] == 20480
    values = yaml.safe_load((ADDON / "chart/values.yaml").read_text())["cluster-autoscaler"]
    assert values["image"]["tag"] == "v1.35.0"
    flags = values["extraArgs"]
    assert flags["max-scale-down-parallelism"] == flags["max-nodes-per-scaleup"] == 1
    assert flags["enable-provisioning-requests"] is True
    assert "max-empty-bulk-delete" not in flags
    assert "max-nodegroup-parallelism" not in flags
    assert flags["skip-nodes-with-local-storage"] is True


@pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm and chart dependencies")
def test_kernel_compatibility_is_scoped_and_can_be_disabled():
    """
    Constrain the opt-in host firewall helper and omit it completely when disabled.
    """
    command = ["helm", "template", "lab", str(FULL / "chart"), "--namespace", "polyad"]
    objects = list(filter(None, yaml.safe_load_all(subprocess.check_output(command, text=True))))
    daemon = next(item for item in objects if item["kind"] == "DaemonSet" and item["metadata"]["name"] == "lab-kindnet-compat")
    pod = daemon["spec"]["template"]["spec"]
    assert pod["hostNetwork"] is True
    assert pod["automountServiceAccountToken"] is False
    assert all("hostPath" not in volume for volume in pod["volumes"])
    security = pod["containers"][0]["securityContext"]
    assert security["capabilities"] == {"drop": ["ALL"], "add": ["NET_ADMIN"]}
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    config = next(item for item in objects if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "lab-kindnet-compat")
    assert "priority 96" in config["data"]["kindnet-compat.nft"]
    assert "ct state new ct label set ct label | 28" in config["data"]["kindnet-compat.nft"]
    assert "nft delete table inet polyad_kindnet_compat" in config["data"]["kindnet-compat.sh"]

    disabled = list(
        filter(None, yaml.safe_load_all(subprocess.check_output([*command, "--set", "kindnetCompat.enabled=false"], text=True)))
    )
    assert not any(item["metadata"]["name"] == "lab-kindnet-compat" for item in disabled)
