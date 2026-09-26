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
    for executable in (
        "docker",
        "helm",
        "minikube",
        "kubectl",
        "virsh",
        "uname",
        "curl",
        "crane",
        "qemu-img",
        "qemu-system-aarch64",
        "qemu-system-x86_64",
    ):
        (bin_dir / executable).symlink_to(recorder)

    # Parse real JSON while mocking only infrastructure and network commands.
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("requires jq to parse Minikube profiles")
    (bin_dir / "jq").symlink_to(jq)
    log = tmp_path / "commands.jsonl"

    # Ignore developer overrides and launch outside the checkout to test path handling.
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("POLYAD_MINIKUBE_", "MINIKUBE_TEST_"))}
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


def test_start_scopes_kvm_nodes_and_pushes_production_image(commands: Runner) -> None:
    """
    Start three KVM VMs and publish the content-tagged production image before Helm.
    """
    result, records = commands("start")
    assert result.returncode == 0, result.stderr
    calls = [record["command"] for record in records]
    start = next(call for call in calls if call[:4] == ["minikube", "--profile", "isolated-test", "start"])
    assert start[:4] == ["minikube", "--profile", "isolated-test", "start"]
    assert start[start.index("--driver") + 1] == "kvm2"
    assert start[start.index("--kvm-qemu-uri") + 1] == "qemu:///system"
    assert start[start.index("--kvm-network") + 1] == "default"
    assert start[start.index("--insecure-registry") + 1] == "localhost:5000"
    assert calls[:4] == [
        ["uname", "-s"],
        ["uname", "-m"],
        ["virsh", "--connect", "qemu:///system", "domcapabilities", "--virttype", "kvm"],
        ["virsh", "--connect", "qemu:///system", "net-info", "default"],
    ]
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
        ["enable", "registry"],
    ]
    for record in records:
        call = record["command"]
        if call[0] == "minikube":
            assert call[1:3] == ["--profile", "isolated-test"]
        elif call[0] == "kubectl":
            assert call[1:4] == ["--context", "isolated-test", "--namespace"]
            assert call[4] == ("kube-system" if "registry" in " ".join(call) else "test-namespace")
        elif call[0] == "helm":
            assert record["repository_config"] == str(ROOT / ".cache/minikube/helm/repositories.yaml")
            assert record["repository_cache"] == str(ROOT / ".cache/minikube/helm/repository-cache")
    build = next(call for call in calls if call[:3] == ["docker", "buildx", "build"])
    assert build[build.index("--target") + 1] == "production"
    assert build[build.index("--platform") + 1] == "linux/amd64"
    assert "--load" in build and "--provenance=false" in build
    image_push = next(call for call in calls if call[:2] == ["docker", "push"])
    assert image_push[-1] == "127.0.0.1:5000/polyad:local-" + "a" * 64
    forward = next(call for call in calls if "port-forward" in call)
    assert forward[-4:] == ["--address", "127.0.0.1", "service/registry", "5000:80"]
    assert sum(record.get("forward_stopped", False) for record in records) == 1
    registry_ready = next(call for call in calls if "daemonset/registry-proxy" in call)
    assert calls.index(registry_ready) < calls.index(forward) < calls.index(image_push)
    upgrade = next(call for call in calls if call[:2] == ["helm", "upgrade"])
    assert upgrade[upgrade.index("--kube-context") + 1] == "isolated-test"
    assert upgrade[upgrade.index("--namespace") + 1] == "test-namespace"
    assert "operator.image.tag=local-" + "a" * 64 in upgrade
    assert "operator.image.repository=localhost:5000/polyad" in upgrade
    assert "operator.image.pullPolicy=IfNotPresent" in upgrade
    assert calls.index(image_push) < calls.index(upgrade)
    crds = next(call for call in calls if "apply" in call)
    assert "--server-side" in crds and "--force-conflicts" not in crds
    assert calls.index(image_push) < calls.index(crds) < calls.index(upgrade)
    assert any("exec" in call and "polyad.operator.lifecycle.probes" in call for call in calls)
    assert not any("--delete-on-failure" in call or "load" in call or call[:2] == ["docker", "run"] for call in calls)


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


@pytest.mark.parametrize(
    "failure",
    ["docker buildx build", "helm dependency build", "addons enable registry", "rollout status deployment/registry", "apply --server-side"],
)
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
        "operator.image.pullPolicy=IfNotPresent",
        "operator.image.repository=localhost:5000/polyad",
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
        (["start"], {"POLYAD_MINIKUBE_DRIVER": "docker"}),
        (["enable"], {"POLYAD_MINIKUBE_DRIVER": "podman"}),
        (["start"], {"POLYAD_MINIKUBE_REGISTRY_PORT": "0"}),
        (["start"], {"POLYAD_MINIKUBE_REGISTRY_PORT": "1023"}),
        (["start"], {"POLYAD_MINIKUBE_REGISTRY_PORT": "65536"}),
        (["start"], {"POLYAD_MINIKUBE_REGISTRY_PORT": "not-a-port"}),
        (["start"], {"POLYAD_MINIKUBE_REGISTRY_PORT": "05000"}),
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
    start = next(record["command"] for record in records if record["command"][:4] == ["minikube", "--profile", "isolated-test", "start"])
    for option, value in (("--nodes", "2"), ("--cpus", "3"), ("--memory", "3072")):
        assert start[start.index(option) + 1] == value


def test_kvm_connection_network_and_registry_overrides_are_consistent(commands: Runner) -> None:
    """
    Forward custom libvirt settings and keep the host push port separate from VM pulls.
    """
    result, records = commands(
        "start",
        overrides={
            "POLYAD_MINIKUBE_KVM_QEMU_URI": "qemu:///session",
            "POLYAD_MINIKUBE_KVM_NETWORK": "polyad-dev",
            "POLYAD_MINIKUBE_REGISTRY_PORT": "5500",
        },
    )
    assert result.returncode == 0, result.stderr
    calls = [record["command"] for record in records]
    assert ["virsh", "--connect", "qemu:///session", "net-info", "polyad-dev"] in calls
    start = next(call for call in calls if call[:4] == ["minikube", "--profile", "isolated-test", "start"])
    assert start[start.index("--kvm-qemu-uri") + 1] == "qemu:///session"
    assert start[start.index("--kvm-network") + 1] == "polyad-dev"
    assert ["docker", "push", "127.0.0.1:5500/polyad:local-" + "a" * 64] in calls
    upgrade = next(call for call in calls if call[:2] == ["helm", "upgrade"])
    assert "operator.image.repository=localhost:5000/polyad" in upgrade


@pytest.mark.parametrize("failure", ["domcapabilities", "net-info"])
def test_kvm_preflight_failure_stops_before_start(commands: Runner, failure: str) -> None:
    """
    Preserve all profiles when virtualization or libvirt access is unavailable.

    Args:
        commands (Runner): Infrastructure command recorder.
        failure (str): Libvirt operation to fail.
    """
    result, records = commands("start", overrides={"FAIL_COMMAND": failure})
    assert result.returncode != 0 and "unavailable" in result.stderr
    assert all(record["command"][0] in {"uname", "virsh"} for record in records)


def test_unsupported_host_fails_without_fallback(commands: Runner) -> None:
    """
    Do not silently switch an unsupported host to Docker or another VM driver.
    """
    result, records = commands("start", overrides={"MINIKUBE_TEST_HOST_OS": "FreeBSD"})
    assert result.returncode != 0 and "Unsupported host" in result.stderr
    assert [record["command"] for record in records] == [["uname", "-s"]]


@pytest.mark.parametrize("command", ["enable", "test"])
def test_existing_docker_profile_is_not_reused_or_deleted(commands: Runner, command: str) -> None:
    """
    Require a fresh KVM2 profile instead of modifying an existing Docker-backed cluster.

    Args:
        commands (Runner): Infrastructure command recorder.
        command (str): Lifecycle operation that would write Kubernetes resources.
    """
    result, records = commands(command, overrides={"MINIKUBE_TEST_PROFILE_DRIVER": "docker"})
    assert result.returncode != 0 and "fresh POLYAD_MINIKUBE_PROFILE" in result.stderr
    infrastructure = [record for record in records if record["command"][0] != "uname"]
    assert len(infrastructure) == 1 and infrastructure[0]["command"][3:5] == ["profile", "list"]


@pytest.mark.parametrize(("arch", "qemu", "platform"), [("arm64", "aarch64", "arm64"), ("x86_64", "x86_64", "amd64")])
def test_macos_starts_native_qemu_vms_and_pushes_from_host(commands: Runner, arch: str, qemu: str, platform: str) -> None:
    """
    Use HVF and shared VM networking, then push from macOS rather than Docker Desktop.
    """
    result, records = commands("start", overrides={"MINIKUBE_TEST_HOST_OS": "Darwin", "MINIKUBE_TEST_HOST_ARCH": arch})
    assert result.returncode == 0, result.stderr
    calls = [record["command"] for record in records]
    assert [f"qemu-system-{qemu}", "-accel", "help"] in calls
    start = next(call for call in calls if call[:4] == ["minikube", "--profile", "isolated-test", "start"])
    assert start[start.index("--driver") + 1] == "qemu2"
    assert start[start.index("--network") + 1] == "socket_vmnet"
    assert start[start.index("--nodes") + 1] == "3"
    assert "--kvm-qemu-uri" not in start and "--kvm-network" not in start
    build = next(call for call in calls if call[:3] == ["docker", "buildx", "build"])
    assert build[build.index("--platform") + 1] == f"linux/{platform}"
    save = next(call for call in calls if call[:3] == ["docker", "image", "save"])
    archive = save[save.index("--output") + 1]
    push = next(call for call in calls if call[:2] == ["crane", "push"])
    assert push == ["crane", "push", "--insecure", archive, "127.0.0.1:5000/polyad:local-" + "a" * 64]
    forward = next(call for call in calls if "port-forward" in call)
    upgrade = next(call for call in calls if call[:2] == ["helm", "upgrade"])
    assert calls.index(save) < calls.index(forward) < calls.index(push) < calls.index(upgrade)
    assert sum(record.get("forward_stopped", False) for record in records) == 1
    assert not Path(archive).exists()
    assert not any(call[0] == "virsh" or call[:2] == ["docker", "push"] for call in calls)


@pytest.mark.parametrize("failure", ["docker image save", "port-forward", "curl", "crane push"])
def test_failed_macos_publish_cleans_temporary_archive(commands: Runner, failure: str) -> None:
    """
    Remove the exported image and owned forward on each macOS publishing failure.
    """
    result, records = commands("enable", overrides={"MINIKUBE_TEST_HOST_OS": "Darwin", "FAIL_COMMAND": failure})
    assert result.returncode != 0
    calls = [record["command"] for record in records]
    save = next(call for call in calls if call[:3] == ["docker", "image", "save"])
    assert not Path(save[save.index("--output") + 1]).exists()
    if failure in {"curl", "crane push"}:
        assert sum(record.get("forward_stopped", False) for record in records) == 1
    assert not any("upgrade" in call or "apply" in call for call in calls)


def test_macos_requires_hvf_before_start(commands: Runner) -> None:
    """
    Refuse slow software emulation rather than silently replacing hardware acceleration.
    """
    result, records = commands("start", overrides={"MINIKUBE_TEST_HOST_OS": "Darwin", "MINIKUBE_TEST_ACCELERATORS": "tcg"})
    assert result.returncode != 0 and "must support HVF" in result.stderr
    assert all(record["command"][0] in {"uname", "qemu-system-x86_64"} for record in records)


@pytest.mark.parametrize("executable", ["crane", "qemu-system-aarch64", "qemu-img"])
def test_macos_missing_prerequisite_never_starts_vms(commands: Runner, tmp_path: Path, executable: str) -> None:
    """
    Diagnose missing local tools before creating a profile or touching the cluster.
    """
    (tmp_path / "bin" / executable).unlink()
    result, records = commands("start", overrides={"MINIKUBE_TEST_HOST_OS": "Darwin", "MINIKUBE_TEST_HOST_ARCH": "arm64"})
    assert result.returncode != 0 and f"Executable '{executable}' is required" in result.stderr
    assert all(record["command"][0] == "uname" for record in records)


@pytest.mark.parametrize("driver", ["qemu", "qemu2"])
def test_macos_accepts_qemu_driver_alias(commands: Runner, driver: str) -> None:
    """
    Normalize Minikube's public QEMU alias to the stored QEMU2 profile driver.
    """
    result, _ = commands("test", overrides={"MINIKUBE_TEST_HOST_OS": "Darwin", "POLYAD_MINIKUBE_DRIVER": driver})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("host", "driver"), [("Darwin", "kvm2"), ("Linux", "qemu2")])
def test_incompatible_driver_override_fails_before_cluster_access(commands: Runner, host: str, driver: str) -> None:
    """
    Reject explicitly incompatible drivers without touching existing profiles.
    """
    result, records = commands("start", overrides={"MINIKUBE_TEST_HOST_OS": host, "POLYAD_MINIKUBE_DRIVER": driver})
    assert result.returncode != 0 and "requires driver" in result.stderr
    assert [record["command"] for record in records] == [["uname", "-s"]]


@pytest.mark.parametrize("command", ["enable", "test"])
def test_macos_rejects_builtin_network_profiles(commands: Runner, command: str) -> None:
    """
    Reject an isolated QEMU user network that cannot provide the required VM connectivity.
    """
    result, records = commands(command, overrides={"MINIKUBE_TEST_HOST_OS": "Darwin", "MINIKUBE_TEST_PROFILE_NETWORK": "builtin"})
    assert result.returncode != 0 and "required network" in result.stderr
    assert not any("addons" in record["command"] or "apply" in record["command"] for record in records)


def test_brewfile_covers_macos_cluster_dependencies() -> None:
    """
    Keep native VM, build, registry, and cluster tools available through brew bundle.
    """
    brewfile = (ROOT / "Brewfile").read_text()
    for dependency in ("asdf", "bash", "curl", "jq", "docker", "docker-buildx", "crane", "minikube", "qemu", "socket_vmnet", "kind"):
        assert f'brew "{dependency}"' in brewfile


@pytest.mark.parametrize("failure", ["curl", "docker push"])
def test_failed_registry_push_closes_forward_and_never_installs(commands: Runner, failure: str) -> None:
    """
    Clean up the owned forward on registry failures without touching unrelated processes.

    Args:
        commands (Runner): Infrastructure command recorder.
        failure (str): Registry operation to fail after forwarding starts.
    """
    result, records = commands("enable", overrides={"FAIL_COMMAND": failure})
    assert result.returncode == 43
    assert sum(record.get("forward_stopped", False) for record in records) == 1
    assert not any("upgrade" in record["command"] or "apply" in record["command"] for record in records)


def test_failed_port_forward_never_probes_or_pushes_an_unrelated_registry(commands: Runner) -> None:
    """
    Stop before HTTP checks or push when our forward cannot bind the selected port.
    """
    result, records = commands("enable", overrides={"FAIL_COMMAND": "port-forward"})
    assert result.returncode != 0 and "port-forward failed" in result.stderr
    calls = [record["command"] for record in records]
    assert not any(call[0] == "curl" or call[:2] in (["docker", "push"], ["helm", "upgrade"]) for call in calls)


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
    assert operator["image"] == "localhost:5000/polyad:minikube" and operator["imagePullPolicy"] == "IfNotPresent"
    env = {item["name"]: item.get("value") for item in operator["env"]}
    assert "--ha" not in operator["args"]
    assert env["POLYAD_COMPONENT"] == "dense"
    dragonfly = next(obj for obj in objects if obj["kind"] == "Dragonfly")
    assert dragonfly["metadata"]["name"] == "polyad-queue" and dragonfly["spec"]["replicas"] == 1
    assert dragonfly["spec"]["snapshot"]["persistentVolumeClaimSpec"]["storageClassName"] == "local-path"
    assert not any(
        obj["kind"] in {"HorizontalPodAutoscaler", "ScaledObject", "VirtualService", "Graph", "PodDisruptionBudget"} for obj in objects
    )
