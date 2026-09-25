"""
Verify Kind lifecycle commands, isolated credentials, and non-HA Helm rendering.
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
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "integrations/kind/kind.sh"
type Runner = Callable[..., tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]]


@pytest.fixture
def commands(tmp_path: Path) -> Runner:
    """
    Run the real helper in a temporary checkout with recorded infrastructure commands.

    Args:
        tmp_path (Path): Isolated files and fake kubeconfig location.

    Returns:
        Runner: Process runner returning exit status, output, and recorded commands.
    """
    checkout = tmp_path / "checkout"
    for relative in (
        "integrations/kind/kind.sh",
        "integrations/kind/cluster.yaml",
        "integrations/kind/values.yaml",
        "scripts/tooling/tool-version.sh",
        "scripts/tooling/build-chart-dependencies.sh",
        ".tool-versions",
        "charts/polyad/Chart.lock",
    ):
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorder = bin_dir / "record"
    shutil.copyfile(Path(__file__).parent / "data/local_cluster_command.py", recorder)
    recorder.chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)
    for name in ("kind", "docker", "helm", "kubectl"):
        (bin_dir / name).symlink_to(recorder)
    log = tmp_path / "commands.jsonl"

    # The caller's real configuration must never be opened or modified by this fixture.
    unrelated_config = tmp_path / "unrelated-kubeconfig"
    unrelated_config.write_text("current-context: production\n")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("POLYAD_KIND_")}
    for key in ("FAIL_COMMAND", "EXISTING_CLUSTERS", "EXISTING_BOUNDARIES", "KIND_NODES"):
        environment.pop(key, None)
    environment.update(
        PATH=f"{bin_dir}:/usr/bin:/bin",
        COMMAND_LOG=str(log),
        KUBECONFIG=str(unrelated_config),
        POLYAD_KIND_CLUSTER="isolated-test",
        POLYAD_KIND_NAMESPACE="test-namespace",
        KIND_EXPERIMENTAL_PROVIDER="podman",
    )

    def run(*args: str, overrides: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
        log.write_text("")
        result = subprocess.run(
            ["bash", str(checkout / "integrations/kind/kind.sh"), *args],
            cwd=tmp_path,
            env={**environment, **(overrides or {})},
            text=True,
            capture_output=True,
            check=False,
        )
        assert unrelated_config.read_text() == "current-context: production\n"
        return result, [json.loads(line) for line in log.read_text().splitlines()]

    return run


def test_start_loads_image_and_isolates_every_cluster_command(commands: Runner) -> None:
    """
    Use Docker-backed nodes and private credentials for cluster creation and all clients.
    """
    result, records = commands("start")
    assert result.returncode == 0, result.stderr
    calls = [record["command"] for record in records]
    create = next(call for call in calls if call[:3] == ["kind", "create", "cluster"])
    config = Path(create[create.index("--config") + 1])
    assert [node["role"] for node in yaml.safe_load(config.read_text())["nodes"]] == ["control-plane", "worker", "worker"]
    version = dict(line.split() for line in (ROOT / ".tool-versions").read_text().splitlines() if line and not line.startswith("#"))[
        "kubectl"
    ]
    assert create[create.index("--image") + 1] == f"kindest/node:v{version}"
    kubeconfig = create[create.index("--kubeconfig") + 1]
    assert kubeconfig.endswith("/.cache/kind/isolated-test/kubeconfig")
    assert Path(kubeconfig).stat().st_mode & 0o777 == 0o600
    for record in records:
        call = record["command"]
        assert record["kind_provider"] == "docker"
        if call[0] == "kind" and call[1:3] != ["get", "clusters"]:
            assert call[call.index("--name") + 1] == "isolated-test"
        if call[0] == "kind" and call[1] in {"create", "export"}:
            assert call[call.index("--kubeconfig") + 1] == kubeconfig
        if call[0] == "kubectl":
            assert call[1:7] == ["--kubeconfig", kubeconfig, "--context", "kind-isolated-test", "--namespace", "test-namespace"]
    build = next(call for call in calls if call[:3] == ["docker", "buildx", "build"])
    assert build[build.index("--target") + 1] == "production"
    image_load = next(call for call in calls if call[:3] == ["kind", "load", "docker-image"])
    assert image_load[3] == "polyad:local-" + "a" * 64
    upgrade = next(call for call in calls if call[:2] == ["helm", "upgrade"])
    assert calls.index(build) < calls.index(image_load) < calls.index(upgrade)
    assert upgrade[upgrade.index("--kubeconfig") + 1] == kubeconfig
    assert upgrade[upgrade.index("--kube-context") + 1] == "kind-isolated-test"
    assert "operator.image.tag=local-" + "a" * 64 in upgrade
    assert "operator.replicaCount=1" in upgrade and "dragonflyOperator.replicaCount=1" in upgrade
    assert any("--server-side" in call and "--force-conflicts" not in call for call in calls)
    assert any("--for=jsonpath={.status.metrics.execution.completedNodes}=1" in call for call in calls)
    graph = yaml.safe_load(next(record["stdin"] for record in records if "stdin" in record))
    assert graph["spec"]["nodes"][0]["ref"] == graph["metadata"]["name"] == "polyad-kind-smoke-abc12"
    assert [call[8] for call in calls if call[0] == "kubectl" and "delete" in call] == [
        "graph/polyad-kind-smoke-abc12",
        "workload/polyad-kind-smoke-abc12",
    ]


def test_start_reuses_only_exact_cluster_match(commands: Runner) -> None:
    """
    Re-export credentials without recreating an existing cluster or confusing similar names.
    """
    result, records = commands("start", overrides={"EXISTING_CLUSTERS": "unrelated\nisolated-test\n"})
    assert result.returncode == 0, result.stderr
    assert not any(record["command"][:3] == ["kind", "create", "cluster"] for record in records)
    result, records = commands("start", overrides={"EXISTING_CLUSTERS": "isolated-test-other\n"})
    assert result.returncode == 0, result.stderr
    assert any(record["command"][:3] == ["kind", "create", "cluster"] for record in records)


@pytest.mark.parametrize("failure", ["docker buildx build", "helm dependency build", "kind load docker-image", "apply --server-side"])
def test_failed_install_never_upgrades_or_cleans_up(commands: Runner, failure: str) -> None:
    """
    Propagate prerequisite failures and leave diagnostic resources untouched.
    """
    result, records = commands("enable", overrides={"FAIL_COMMAND": failure})
    assert result.returncode == 43
    assert not any("upgrade" in record["command"] or "delete" in record["command"] for record in records)


def test_missing_cluster_fails_before_build_or_upgrade(commands: Runner) -> None:
    """
    Require start rather than silently installing into another available kubeconfig context.
    """
    result, records = commands("enable", overrides={"KIND_NODES": ""})
    assert result.returncode != 0 and "run start first" in result.stderr
    assert [record["command"] for record in records] == [["kind", "get", "nodes", "--name", "isolated-test"]]


def test_failed_smoke_retains_its_unique_objects(commands: Runner) -> None:
    """
    Do not delete the failed Graph or hide its completion timeout.
    """
    result, records = commands("test", overrides={"FAIL_COMMAND": "wait graph/polyad-kind-smoke-abc12"})
    assert result.returncode == 43
    assert not any("delete" in record["command"] for record in records)


def test_disable_checks_boundaries_before_scoped_uninstall(commands: Runner) -> None:
    """
    Keep the controller available until its application drain finalizers have completed.
    """
    result, records = commands("disable", overrides={"EXISTING_BOUNDARIES": "graph.polyad.astrivant.com/application"})
    assert result.returncode != 0 and "Drain/delete" in result.stderr
    assert not any(record["command"][0] == "helm" for record in records)
    result, records = commands("disable")
    assert result.returncode == 0, result.stderr
    uninstall = records[-1]["command"]
    assert uninstall[:3] == ["helm", "uninstall", "polyad"]
    assert uninstall[uninstall.index("--kube-context") + 1] == "kind-isolated-test"
    assert "--kubeconfig" in uninstall and not any("delete" in record["command"] for record in records)


def test_delete_targets_only_named_cluster_and_private_credentials(commands: Runner) -> None:
    """
    Never call a global Docker prune or mutate the caller's kubeconfig on deletion.
    """
    result, records = commands("delete")
    assert result.returncode == 0, result.stderr
    assert len(records) == 1
    call = records[0]["command"]
    assert call[:6] == ["kind", "delete", "cluster", "--name", "isolated-test", "--kubeconfig"]
    assert call[6].endswith("/.cache/kind/isolated-test/kubeconfig")


def test_render_is_cluster_independent_and_keeps_standalone_overrides(commands: Runner, tmp_path: Path) -> None:
    """
    Accept paths with spaces without letting overlay HA settings alter the local profile.
    """
    overlay = tmp_path / "custom values.yaml"
    overlay.write_text("ha: true\n")
    result, records = commands("render", overrides={"POLYAD_KIND_VALUES": str(overlay)})
    assert result.returncode == 0, result.stderr
    assert all(record["command"][0] == "helm" for record in records)
    command = records[-1]["command"]
    assert command[:2] == ["helm", "template"]
    assert command.index(str(overlay)) < command.index("ha=false")
    assert "dragonfly.ha.enabled=false" in command and "operator.image.pullPolicy=Never" in command


@pytest.mark.parametrize(
    ("args", "overrides"),
    [
        ([], {}),
        (["stop"], {}),
        (["bogus"], {}),
        (["start", "extra"], {}),
        (["start"], {"POLYAD_KIND_CLUSTER": "../unsafe"}),
        (["start"], {"POLYAD_KIND_NAMESPACE": "Invalid"}),
        (["start"], {"POLYAD_KIND_CONFIG": "/missing.yaml"}),
        (["enable"], {"POLYAD_KIND_VALUES": "/missing.yaml"}),
    ],
)
def test_invalid_arguments_never_access_infrastructure(commands: Runner, args: list[str], overrides: dict[str, str]) -> None:
    """
    Validate targeting and input paths before creating or modifying cluster resources.
    """
    result, records = commands(*args, overrides=overrides)
    assert result.returncode != 0 and records == []


@pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm and locked chart dependencies")
def test_kind_values_render_standalone_with_builtin_storage() -> None:
    """
    Use the real Helm chart to verify the deployment names, cache count, and storage class.
    """
    output = subprocess.check_output(
        ["helm", "template", "polyad", str(ROOT / "charts/polyad"), "--namespace", "polyad", "-f", str(SCRIPT.with_name("values.yaml"))],
        text=True,
    )
    objects = [obj for obj in yaml.safe_load_all(output) if obj]
    deployments = {obj["metadata"]["name"]: obj for obj in objects if obj["kind"] == "Deployment"}
    assert set(deployments) == {"polyad-polyad", "polyad-dragonfly-operator"}
    assert all(obj["spec"]["replicas"] == 1 for obj in deployments.values())
    operator = deployments["polyad-polyad"]["spec"]["template"]["spec"]["containers"][0]
    assert operator["image"] == "polyad:kind" and operator["imagePullPolicy"] == "Never"
    assert "--ha" not in operator["args"]
    dragonfly = next(obj for obj in objects if obj["kind"] == "Dragonfly")
    assert dragonfly["spec"]["replicas"] == 1
    assert dragonfly["spec"]["snapshot"]["persistentVolumeClaimSpec"]["storageClassName"] == "standard"
    assert not any(
        obj["kind"] in {"HorizontalPodAutoscaler", "ScaledObject", "VirtualService", "Graph", "PodDisruptionBudget"} for obj in objects
    )
