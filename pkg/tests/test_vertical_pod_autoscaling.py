"""
Validate optional VPA admission, application projection and live resource helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from polyad.compiler.passes.vertical import compile_vertical_pod_autoscaler, inject_vertical_environment
from polyad_sdk import VPAConstraints, container_metrics

if TYPE_CHECKING:
    from pathlib import Path


def policy(**overrides):
    """
    Return one representative native VPA spec.
    """
    value = {
        "targetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "compiled-worker"},
        "updatePolicy": {"updateMode": "InPlaceOrRecreate"},
        "resourcePolicy": {
            "containerPolicies": [
                {
                    "containerName": "worker",
                    "minAllowed": {"cpu": "100m", "memory": "128Mi"},
                    "maxAllowed": {"cpu": "2", "memory": "4Gi"},
                }
            ]
        },
    }
    value.update(overrides)
    return value


def test_vpa_bounds_are_validated_and_projected_in_sdk_units():
    """
    Native quantities become immutable SDK bounds in the projected integer units.
    """
    spec = compile_vertical_pod_autoscaler(policy(), {"compiled-worker": "Deployment"})
    pod = {"spec": {"containers": [{"name": "worker", "env": [{"name": "KEEP", "value": "yes"}]}]}}
    inject_vertical_environment(pod, spec)
    environment = {item["name"]: item["value"] for item in pod["spec"]["containers"][0]["env"]}
    bounds = VPAConstraints.from_environment(environment)
    assert bounds.min_cpu_millicores == 100
    assert bounds.max_cpu_millicores == 2000
    assert bounds.min_memory_bytes == 128 * 1024 * 1024
    assert bounds.max_memory_bytes == 4 * 1024**3
    assert bounds.update_mode == "InPlaceOrRecreate"
    assert bounds.clamp("cpu", 50) == 100
    assert bounds.clamp("cpu", 3000) == 2000
    assert environment["KEEP"] == "yes"


@pytest.mark.parametrize(
    "change,match",
    [
        ({"targetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "outside"}}, "same graph"),
        ({"updatePolicy": {"updateMode": "Auto"}}, "ambiguous"),
        (
            {
                "resourcePolicy": {
                    "containerPolicies": [{"containerName": "worker", "minAllowed": {"memory": "2Gi"}, "maxAllowed": {"memory": "1Gi"}}]
                }
            },
            "must not exceed",
        ),
    ],
)
def test_vpa_rejects_ambiguous_targets_modes_and_bounds(change, match):
    """
    Unsafe target ownership and policy intervals fail before resource creation.
    """
    with pytest.raises(ValueError, match=match):
        compile_vertical_pod_autoscaler(policy(**change), {"compiled-worker": "Deployment"})


def test_live_cgroup_v2_metrics_expose_usage_limits_and_headroom(tmp_path: Path):
    """
    Applications can sample live assignments after an in-place resize.
    """
    (tmp_path / "cpu.stat").write_text("usage_usec 12345\nuser_usec 10000\n", encoding="ascii")
    (tmp_path / "cpu.max").write_text("200000 100000\n", encoding="ascii")
    (tmp_path / "memory.current").write_text("1024\n", encoding="ascii")
    (tmp_path / "memory.max").write_text("4096\n", encoding="ascii")
    sample = container_metrics(tmp_path)
    assert sample.cpu_usage_usec == 12345
    assert sample.cpu_limit_millicores == 2000
    assert sample.memory_usage_bytes == 1024
    assert sample.memory_available_bytes == 3072
