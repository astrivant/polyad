"""
Keep failed-install diagnostics useful when custom APIs or individual reads fail.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = "scripts/testing/collect-operator-diagnostics.sh"
CUSTOM_APIS = (
    "dragonflies.dragonflydb.io",
    "dragonflypools.polyad.astrivant.com",
    "graphs.polyad.astrivant.com",
    "polygraphs.polyad.astrivant.com",
    "replicagroups.polyad.astrivant.com",
    "scaledobjects.keda.sh",
)
MOCK_KUBECTL = r"""
kubectl() {
    printf 'CALL %s\n' "$*" >&2
    if [[ -n "$DIAGNOSTIC_TEST_FAIL" && " $* " == *" $DIAGNOSTIC_TEST_FAIL "* ]]; then
        printf 'simulated Kubernetes API failure\n' >&2
        return 42
    fi
    if [[ " $* " == *' api-resources '* ]]; then
        printf '%s\n' "$DIAGNOSTIC_TEST_APIS"
    else
        printf 'collected %s\n' "$*"
    fi
}
export -f kubectl
bash "$1"
"""


def diagnostics(apis=CUSTOM_APIS, *, fail="", namespace="polyad"):
    """
    Execute the real helper with a shell mock, never contacting a Kubernetes cluster.
    """
    result = subprocess.run(
        ["bash", "-c", MOCK_KUBECTL, "operator-diagnostics-test", str(ROOT / HELPER)],
        env={
            **os.environ,
            "DIAGNOSTIC_TEST_APIS": "\n".join(apis),
            "DIAGNOSTIC_TEST_FAIL": fail,
            "POLYAD_TEST_NAMESPACE": namespace,
        },
        capture_output=True,
        text=True,
    )
    calls = [shlex.split(line.removeprefix("CALL ")) for line in result.stderr.splitlines() if line.startswith("CALL ")]
    return result, calls


@pytest.mark.parametrize("apis", [(), CUSTOM_APIS[1:], CUSTOM_APIS])
def test_only_installed_custom_apis_are_read_and_logs_are_always_collected(apis):
    """
    An absent Dragonfly CRD or a completely failed chart install cannot stop diagnostics.
    """
    result, calls = diagnostics(apis)
    assert result.returncode == 0, result.stderr
    for resource in CUSTOM_APIS:
        reads = [call for call in calls if "get" in call and resource in call]
        assert len(reads) == int(resource in apis)
        if resource not in apis:
            assert f"Skipping diagnostics for {resource}: API not installed." in result.stdout

    # Independent requests preserve Pods, leases and events before custom discovery.
    native = ["pods", "deployments.apps", "statefulsets.apps", "jobs.batch", "leases.coordination.k8s.io", "events"]
    assert [call[4] for call in calls[: len(native)]] == native
    assert calls[len(native)] == ["--request-timeout=20s", "api-resources", "--namespaced=true", "--verbs=list", "-o", "name"]
    assert calls[-1] == ["--request-timeout=20s", "-n", "polyad", "logs", "deployment/polyad-polyad", "--all-containers=true", "--tail=200"]


@pytest.mark.parametrize("fail", ["get pods", "get dragonflies.dragonflydb.io", "api-resources", "logs"])
def test_failed_diagnostic_requests_remain_visible_without_stopping_later_requests(fail):
    """
    Real API and log errors return failure only after the remaining evidence is collected.
    """
    result, calls = diagnostics(fail=fail)
    assert result.returncode == 1
    assert "simulated Kubernetes API failure" in result.stderr
    assert "::warning::" in result.stderr
    assert "logs" in calls[-1]
    assert all(any(resource in call for call in calls) for resource in CUSTOM_APIS)


def test_discovery_requires_an_exact_api_name_and_respects_the_test_namespace():
    """
    Similarly named resources cannot cause an absent API to be queried.
    """
    result, calls = diagnostics(("otherdragonflies.dragonflydb.io",), namespace="integration-tests")
    assert result.returncode == 0, result.stderr
    assert not any(resource in call for call in calls for resource in CUSTOM_APIS)
    assert all(call[2] == "integration-tests" for call in calls if "-n" in call)


def test_operator_workflow_collects_best_effort_diagnostics_only_after_failure():
    """
    Diagnostics cannot suppress the original job failure or run on successful installations.
    """
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["operator"]["steps"]
    step = next(step for step in steps if HELPER in step.get("run", ""))
    assert step["if"] == "failure()"
    assert step["continue-on-error"] == "true"
    assert step["run"] == f"bash {HELPER}"
    assert not any("get pods,leases,dragonflies" in step.get("run", "") for step in steps)
