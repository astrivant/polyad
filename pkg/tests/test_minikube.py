"""
Check local cluster targeting, standalone rendering, and failure-safe smoke tests.
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
SCRIPT = ROOT / "integrations/minikube/minikube.sh"
type Runner = Callable[..., tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]]


@pytest.fixture
def commands(tmp_path: Path) -> Runner:
    """
    Run lifecycle commands against recorders, not installed infrastructure tools.

    Args:
        tmp_path (Path): Isolated pytest temporary directory.

    Returns:
        Runner: Callable returning process results and recorded external commands.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorder = bin_dir / "record"
    shutil.copyfile(Path(__file__).parent / "data/local_cluster_command.py", recorder)
    recorder.chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)
    for executable in ("docker", "helm", "minikube", "kubectl"):
        (bin_dir / executable).symlink_to(recorder)
    log = tmp_path / "commands.jsonl"

    # Ignore developer overrides and launch outside the checkout to test path handling.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("POLYAD_MINIKUBE_")}
    environment.pop("FAIL_COMMAND", None)
    environment.pop("EXISTING_BOUNDARIES", None)
    environment.update(
        PATH=f"{bin_dir}:/usr/bin:/bin",
        COMMAND_LOG=str(log),
        POLYAD_MINIKUBE_PROFILE="isolated-test",
        POLYAD_MINIKUBE_NAMESPACE="test-namespace",
    )

    def run(*args: str, overrides: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
        log.write_text("")
        result = subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=tmp_path,
            env={**environment, **(overrides or {})},
            capture_output=True,
            text=True,
            check=False,
        )
        return result, [json.loads(line) for line in log.read_text().splitlines()]

    return run


def test_start_scopes_cluster_commands_and_loads_production_image(commands: Runner) -> None:
    """
    Start three non-HA nodes and load the content-tagged production image before Helm.
    """
    result, records = commands("start")
    assert result.returncode == 0, result.stderr
    calls = [record["command"] for record in records]
    start = calls[0]
    assert start[:4] == ["minikube", "--profile", "isolated-test", "start"]
    assert start[start.index("--nodes") + 1] == "3"
    assert "--keep-context" in start and "--ha=false" in start
    assert start[start.index("--container-runtime") + 1] == "containerd"
    assert (
        start[start.index("--kubernetes-version") + 1]
        == "v"
        + dict(line.split() for line in (ROOT / ".tool-versions").read_text().splitlines() if line.strip() and not line.startswith("#"))[
            "kubectl"
        ]
    )
    assert [call[4:] for call in calls if call[0] == "minikube" and "addons" in call] == [
        ["disable", "storage-provisioner"],
        ["disable", "default-storageclass"],
        ["enable", "storage-provisioner-rancher"],
        ["enable", "metrics-server"],
    ]
    for record in records:
        call = record["command"]
        if call[0] == "minikube":
            assert call[1:3] == ["--profile", "isolated-test"]
        elif call[0] == "kubectl":
            assert call[1:5] == ["--context", "isolated-test", "--namespace", "test-namespace"]
        elif call[0] == "helm":
            assert record["repository_config"] == str(ROOT / ".cache/minikube/helm/repositories.yaml")
            assert record["repository_cache"] == str(ROOT / ".cache/minikube/helm/repository-cache")
    build = next(call for call in calls if call[:3] == ["docker", "buildx", "build"])
    assert build[build.index("--target") + 1] == "production"
    assert "--load" in build and "--provenance=false" in build
    image_load = next(call for call in calls if call[0] == "minikube" and "load" in call)
    assert image_load[-2:] == ["--daemon", "polyad:local-" + "a" * 64]
    upgrade = next(call for call in calls if call[:2] == ["helm", "upgrade"])
    assert upgrade[upgrade.index("--kube-context") + 1] == "isolated-test"
    assert upgrade[upgrade.index("--namespace") + 1] == "test-namespace"
    assert "operator.image.tag=local-" + "a" * 64 in upgrade
    assert calls.index(image_load) < calls.index(upgrade)
    crds = next(call for call in calls if "apply" in call)
    assert "--server-side" in crds and "--force-conflicts" not in crds
    assert calls.index(image_load) < calls.index(crds) < calls.index(upgrade)
    assert any("exec" in call and "polyad.operator.lifecycle.probes" in call for call in calls)


def test_smoke_test_creates_and_cleans_only_its_unique_resources(commands: Runner) -> None:
    """
    Use server-generated identities and verify fresh execution metrics on every run.
    """
    result, records = commands("test")
    assert result.returncode == 0, result.stderr
    graph = yaml.safe_load(next(record["stdin"] for record in records if "stdin" in record))
    name = "polyad-minikube-smoke-abc12"
    assert graph["metadata"]["name"] == name
    assert graph["spec"]["nodes"] == [{"name": "worker", "kind": "Workload", "ref": name}]
    calls = [record["command"] for record in records]
    assert any("--for=jsonpath={.status.metrics.execution.completedNodes}=1" in call for call in calls)
    assert [call[6] for call in calls if "delete" in call] == [f"graph/{name}", f"workload/{name}"]
    assert not any("--all" in call for call in calls if "delete" in call)


@pytest.mark.parametrize("failure", ["docker buildx build", "helm dependency build", "apply --server-side"])
def test_install_failure_stops_without_upgrade_or_cleanup(commands: Runner, failure: str) -> None:
    """
    Preserve developer resources when dependency resolution, builds, or CRD updates fail.
    """
    result, records = commands("enable", overrides={"FAIL_COMMAND": failure})
    assert result.returncode == 43
    assert not any("upgrade" in record["command"] or "delete" in record["command"] for record in records)


def test_failed_smoke_keeps_resources_for_inspection(commands: Runner) -> None:
    """
    Keep diagnostic objects if a Graph fails to complete.
    """
    result, records = commands("test", overrides={"FAIL_COMMAND": "wait graph/polyad-minikube-smoke-abc12"})
    assert result.returncode == 43
    assert not any("delete" in record["command"] for record in records)


def test_render_accepts_overlay_paths_with_spaces_and_enforces_standalone(commands: Runner, tmp_path: Path) -> None:
    """
    Render without cluster tools and apply fixed standalone settings after custom values.
    """
    overlay = tmp_path / "custom values.yaml"
    overlay.write_text("ha: true\n")
    result, records = commands("render", overrides={"POLYAD_MINIKUBE_VALUES": str(overlay)})
    assert result.returncode == 0, result.stderr
    assert all(record["command"][0] == "helm" for record in records)
    render = records[-1]["command"]
    assert render[1] == "template"
    assert render.index(str(overlay)) < render.index("ha=false")
    for setting in (
        "operator.replicaCount=1",
        "operator.image.pullPolicy=Never",
        "dragonfly.ha.enabled=false",
        "dragonflyOperator.replicaCount=1",
        "architecture.mode=Dense",
        "worker.enabled=false",
        "rootControlPlane.enabled=false",
        "keda.install=false",
    ):
        assert setting in render


@pytest.mark.parametrize("command", ["stop", "delete"])
def test_profile_cleanup_only_targets_selected_profile(commands: Runner, command: str) -> None:
    """
    Never perform global Docker cleanup or delete unrelated Minikube profiles.
    """
    result, records = commands(command)
    assert result.returncode == 0
    assert [record["command"] for record in records] == [["minikube", "--profile", "isolated-test", command]]


def test_disable_requires_drained_boundaries(commands: Runner) -> None:
    """
    Leave the controller running until user-managed graph boundaries have been removed.
    """
    result, records = commands("disable", overrides={"EXISTING_BOUNDARIES": "graph.polyad.astrivant.com/application"})
    assert result.returncode != 0 and "Drain/delete" in result.stderr
    assert not any(record["command"][0] == "helm" for record in records)
    result, records = commands("disable")
    assert result.returncode == 0, result.stderr
    uninstall = records[-1]["command"]
    assert uninstall[:3] == ["helm", "uninstall", "polyad"]
    assert uninstall[uninstall.index("--kube-context") + 1] == "isolated-test"
    assert not any("delete" in record["command"] for record in records)


@pytest.mark.parametrize(
    ("args", "overrides"),
    [
        ([], {}),
        (["bogus"], {}),
        (["start", "unexpected"], {}),
        (["start"], {"POLYAD_MINIKUBE_PROFILE": "invalid/profile"}),
        (["start"], {"POLYAD_MINIKUBE_NAMESPACE": "Invalid"}),
        (["start"], {"POLYAD_MINIKUBE_NODES": "0"}),
        (["start"], {"POLYAD_MINIKUBE_VALUES": "/does/not/exist.yaml"}),
    ],
)
def test_invalid_arguments_fail_before_cluster_access(commands: Runner, args: list[str], overrides: dict[str, str]) -> None:
    """
    Reject malformed targeting or missing overlays before starting infrastructure.
    """
    result, records = commands(*args, overrides=overrides)
    assert result.returncode != 0
    assert records == []


def test_node_and_resource_overrides_are_forwarded(commands: Runner) -> None:
    """
    Permit smaller local installations without changing the shared chart defaults.
    """
    result, records = commands(
        "start", overrides={"POLYAD_MINIKUBE_NODES": "2", "POLYAD_MINIKUBE_CPUS": "3", "POLYAD_MINIKUBE_MEMORY": "3072"}
    )
    assert result.returncode == 0, result.stderr
    start = records[0]["command"]
    for option, value in (("--nodes", "2"), ("--cpus", "3"), ("--memory", "3072")):
        assert start[start.index(option) + 1] == value


@pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm and locked chart dependencies")
def test_local_overlay_renders_one_operator_and_one_cache() -> None:
    """
    Exercise the real chart to detect drift in deployment names and non-HA settings.
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
    assert operator["image"] == "polyad:minikube" and operator["imagePullPolicy"] == "Never"
    env = {item["name"]: item.get("value") for item in operator["env"]}
    assert "--ha" not in operator["args"]
    assert env["POLYAD_COMPONENT"] == "dense"
    dragonfly = next(obj for obj in objects if obj["kind"] == "Dragonfly")
    assert dragonfly["metadata"]["name"] == "polyad-queue" and dragonfly["spec"]["replicas"] == 1
    assert dragonfly["spec"]["snapshot"]["persistentVolumeClaimSpec"]["storageClassName"] == "local-path"
    assert not any(
        obj["kind"] in {"HorizontalPodAutoscaler", "ScaledObject", "VirtualService", "Graph", "PodDisruptionBudget"} for obj in objects
    )
