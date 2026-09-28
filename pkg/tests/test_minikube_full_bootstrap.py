"""
Test the remembered HA profile against fresh clusters and existing installations.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[2]
type Runner = Callable[[str], tuple[subprocess.CompletedProcess[str], list[list[str]]]]


@pytest.fixture
def bootstrap(tmp_path: Path) -> Runner:
    """
    Run the real HA script with fake infrastructure commands and private temporary state.
    """
    root = tmp_path / "checkout"
    for relative in (
        "integrations/minikube/full/full.sh",
        "integrations/minikube/autoscaler/paths.sh",
        "integrations/minikube/autoscaler/source.lock.json",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    chart_helper = root / "scripts/tooling/build-chart-dependencies.sh"
    chart_helper.parent.mkdir(parents=True)
    chart_helper.write_text("#!/usr/bin/env bash\nexit 0\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorder = bin_dir / "record"
    shutil.copyfile(ROOT / "pkg/tests/data/minikube_full_command.py", recorder)
    recorder.chmod(0o755)
    for tool in ("kubectl", "minikube", "helm", "openssl"):
        (bin_dir / tool).symlink_to(recorder)
    (bin_dir / "python3").symlink_to(sys.executable)
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("requires jq")
    (bin_dir / "jq").symlink_to(jq)
    state = tmp_path / "mock-state"
    state.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("POLYAD_MINIKUBE_", "MINIKUBE_AUTOSCALER_", "BASH_FUNC_"))
    }
    environment.update(
        PATH=f"{bin_dir}:/usr/bin:/bin",
        XDG_STATE_HOME=str(tmp_path / "addon-state"),
        FULL_TEST_STATE=str(state),
        POLYAD_MINIKUBE_PROFILE="ha-test",
        POLYAD_MINIKUBE_IMAGE="localhost:5000/polyad:test-build",
    )

    def run(mode: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        if mode == "existing":
            (state / "namespace").touch()
            (state / "release-polyad").touch()
            for name in ("api", "events", "metrics", "observer", "record-encryption", "ingress-tls"):
                (state / f"secret-polyad-{name}").touch()
        log = state / "commands.jsonl"
        log.write_text("")
        result = subprocess.run(
            ["bash", str(root / "integrations/minikube/full/full.sh"), "enable"],
            cwd=tmp_path,
            env={**environment, "FULL_TEST_NAMESPACE": mode},
            capture_output=True,
            text=True,
            check=False,
        )
        return result, [json.loads(line) for line in log.read_text().splitlines()]

    return run


@pytest.mark.parametrize("mode", ["fresh", "existing"])
def test_namespace_crds_and_release_bootstrap_are_ordered(bootstrap: Runner, mode: str) -> None:
    """
    Create prerequisites on a fresh cluster without rotating existing installation secrets.
    """
    result, calls = bootstrap(mode)
    assert result.returncode == 0, result.stderr
    assert "Polyad HA smoke test passed" in result.stdout
    kube = [call for call in calls if call[0] == "kubectl"]
    assert all(call[1:5] == ["--context", "ha-test", "--namespace", "polyad"] for call in kube)
    created = [call for call in kube if call[5:7] == ["create", "namespace"]]
    assert len(created) == (mode == "fresh")
    active = next(call for call in kube if call[5:7] == ["wait", "namespace/polyad"])
    first_secret = next(call for call in kube if call[5:7] == ["get", "secret"])
    assert calls.index(active) < calls.index(first_secret)
    if created:
        assert calls.index(created[0]) < calls.index(active)
    secret_creates = [call for call in kube if call[5:7] == ["create", "secret"]]
    assert len(secret_creates) == (6 if mode == "fresh" else 0)
    if mode == "existing":
        assert not any(call[0] == "openssl" for call in calls)

    applied = next(call for call in kube if call[5] == "apply" and call[-1].endswith("charts/polyad-crds/crds"))
    assert "--field-manager=polyad-minikube" in applied
    assert "--force-conflicts" not in applied
    established = next(call for call in kube if call[5] == "wait" and call[-1].endswith("charts/polyad-crds/crds"))
    installs = [call for call in calls if call[:2] == ["helm", "upgrade"]]
    assert calls.index(applied) < calls.index(established) < calls.index(installs[0])
    assert installs[0][2:4] == ["--install", "polyad-lab"]
    assert installs[1][2:4] == ["--install", "polyad"]
    assert "operator.autoscaling.enabled=false" in installs[1]
    assert installs[2][2] == "polyad"
    assert all("--skip-crds" in call for call in installs)


def test_fresh_bootstrap_can_be_repeated_without_recreating_secrets(bootstrap: Runner) -> None:
    """
    Retrying bootstrap must retain the namespace, generated credentials and existing releases.
    """
    result, _ = bootstrap("fresh")
    assert result.returncode == 0, result.stderr
    result, calls = bootstrap("fresh")
    assert result.returncode == 0, result.stderr
    assert not any(call[0] == "openssl" or call[5:7] in (["create", "namespace"], ["create", "secret"]) for call in calls)


@pytest.mark.parametrize("mode", ["forbidden", "terminating", "create-failed", "wait-failed"])
def test_namespace_failures_stop_before_credentials_and_helm(bootstrap: Runner, mode: str) -> None:
    """
    Do not interpret API errors as NotFound or create credentials in a terminating namespace.
    """
    result, calls = bootstrap(mode)
    assert result.returncode != 0
    assert not any(call[0] == "openssl" or call[5:7] in (["get", "secret"], ["create", "secret"]) for call in calls)
    assert not any(call[:2] == ["helm", "upgrade"] for call in calls)
    if mode in ("forbidden", "terminating"):
        assert not any(call[5:7] == ["create", "namespace"] for call in calls)
