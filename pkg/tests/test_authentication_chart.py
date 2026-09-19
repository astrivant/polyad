"""
Verify named credentials survive Helm rendering without embedding token values.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest
import yaml

from tests.test_chart import CHART
from tests.test_chart import pytestmark as pytestmark


def render(*settings, policy=None):
    """
    Render the full credential reference with optional policy or topology overrides.
    """
    command = [
        "helm",
        "template",
        "test",
        str(CHART),
        "-n",
        "test",
        "-f",
        str(CHART / "references" / "values-authentication.reference.yaml"),
    ]
    for setting in settings:
        command.extend(["--set", setting])
    if policy is not None:
        command.extend(["--set-json", "authentication=" + json.dumps(policy)])
    return list(filter(None, yaml.safe_load_all(subprocess.check_output(command, text=True))))


@pytest.mark.parametrize("mode", ["Dense", "Distributed"])
def test_named_registry_replaces_endpoint_token_mounts(mode):
    """
    Project group-specific token files into every local component and retain a matching KEDA Secret.
    """
    objects = render(f"architecture.mode={mode}", f"ha={str(mode == 'Distributed').lower()}")
    config = next(obj for obj in objects if obj["kind"] == "ConfigMap" and obj["metadata"]["name"] == "test-polyad-authentication")
    policy = json.loads(config["data"]["config.json"])
    assert set(policy) == {"services", "operators"}
    assert [key["direction"] for key in policy["services"]] == ["Inbound", "Outbound"]
    pod = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")["spec"]["template"]
    assert "checksum/authentication" in pod["metadata"]["annotations"]
    volumes = {volume["name"]: volume for volume in pod["spec"]["volumes"]}
    assert not {"api-credentials", "events-credentials", "metrics-credentials"} & volumes.keys()
    sources = volumes["authentication"]["projected"]["sources"]
    assert {source["secret"]["items"][0]["path"] for source in sources if "secret" in source} == {
        "services/pipeline",
        "services/archive",
        "operators/west",
        "operators/keda",
    }
    assert {"name": "POLYAD_AUTH_CONFIG_FILE", "value": "/var/run/polyad/authentication/config.json"} in pod["spec"]["containers"][0]["env"]
    for daemon in (obj for obj in objects if obj["kind"] == "Daemon"):
        assert daemon["spec"]["template"]["spec"]["volumes"] == pod["spec"]["volumes"]
    trigger = next(obj for obj in objects if obj["kind"] == "TriggerAuthentication")
    assert trigger["spec"]["secretTargetRef"][0] == {"parameter": "token", "name": "polyad-metrics", "key": "token"}


def test_observer_named_keys_can_reach_shared_admission_cache():
    """
    Observation listeners replace the old Secret and receive narrow cache network access.
    """
    objects = render(
        "observer.enabled=true",
        "global.multiCluster.clusterName=east",
        "networkPolicy.enabled=true",
        "networkPolicy.apiServerCIDRs[0]=10.0.0.1/32",
    )
    pod = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad-observer")["spec"][
        "template"
    ]["spec"]
    assert [volume["name"] for volume in pod["volumes"]] == ["authentication"]
    assert {"name": "POLYAD_CACHE_URL", "value": "redis://test-queue:6379/0"} in pod["containers"][0]["env"]
    observer = next(obj for obj in objects if obj["kind"] == "NetworkPolicy" and obj["metadata"]["name"] == "test-polyad-observer")
    assert observer["spec"]["egress"][0]["to"] == [{"podSelector": {"matchLabels": {"app": "test-queue"}}}]
    cache = next(
        obj for obj in objects if obj["kind"] == "NetworkPolicy" and obj["spec"]["podSelector"]["matchLabels"].get("app") == "test-queue"
    )
    assert {"podSelector": {"matchLabels": {"app.kubernetes.io/instance": "test", "app.kubernetes.io/name": "polyad-observer"}}} in cache[
        "spec"
    ]["ingress"][0]["from"]


@pytest.mark.parametrize(
    "setting",
    [
        "authentication.services[0].direction=Unknown",
        "authentication.services[0].requestsPerMinute=0",
        "authentication.services[0].maxConcurrentRequests=0",
        "authentication.services[0].baseUrl=https://unused.example",
        "authentication.services[1].endpoints[0]=composition",
        "authentication.services[1].baseUrl=ftp://peer.example",
        "authentication.services[1].name=pipeline",
        "authentication.operators[0].endpoints[0]=admin",
        "metrics.authentication.existingSecret=not-a-configured-key",
    ],
)
def test_invalid_named_key_configuration_fails_before_install(setting):
    """
    Reject direction, quota, duplicate identity and KEDA credential mismatches.
    """
    with pytest.raises(subprocess.CalledProcessError):
        if setting.startswith("authentication."):
            policy = yaml.safe_load((CHART / "references" / "values-authentication.reference.yaml").read_text())["authentication"]
            path, value = setting.split("=", 1)
            group, index, field, element = re.fullmatch(r"authentication\.(\w+)\[(\d+)\]\.(\w+)(?:\[(\d+)\])?", path).groups()
            key = policy[group][int(index)]
            if element is not None:
                key.setdefault(field, []).insert(int(element), yaml.safe_load(value))
            else:
                key[field] = yaml.safe_load(value)
            render(policy=policy)
        else:
            render(setting)
