"""
Check the published addon boundary without downloading code or changing real VMs.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[2]
ADDON = Path("integrations/minikube/autoscaler")
REVISION = "a" * 40
type Runner = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, dict[str, str], Runner]:
    """
    Copy only integration scripts and replace network and infrastructure commands.
    """
    root = tmp_path / "polyad"
    for relative in (
        ADDON / "autoscaler.sh",
        ADDON / "paths.sh",
        ADDON / "maintenance.py",
        Path("integrations/minikube/minikube.sh"),
        Path("integrations/minikube/full/full.sh"),
        Path("scripts/tooling/tool-version.sh"),
        Path(".tool-versions"),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)

    # This fake upstream records the boundary, including the original exit code.
    entrypoint = b"""#!/usr/bin/env bash
printf '%s\\n' "$1" "$MINIKUBE_AUTOSCALER_PROFILE" "$MINIKUBE_AUTOSCALER_STATE_DIR" \
    "${MINIKUBE_AUTOSCALER_CONFIG:-}" "$MINIKUBE_AUTOSCALER_BINARY" \
    "${MINIKUBE_AUTOSCALER_IMAGE:-}" > "$ADDON_RECORD"
exit "${ADDON_EXIT:-0}"
"""
    archive = tmp_path / "published.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        info = tarfile.TarInfo(f"minikube-cluster-autoscaler-addon-{REVISION}/scripts/addon.sh")
        info.size = len(entrypoint)
        stream.addfile(info, io.BytesIO(entrypoint))
    (root / ADDON / "source.lock.json").write_text(
        json.dumps(
            {
                "repository": "astrivant/minikube-cluster-autoscaler-addon",
                "revision": REVISION,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
        )
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    curl = bin_dir / "curl"
    curl.write_text("""#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$CURL_LOG"
while [[ $# -gt 0 ]]; do
    if [[ "$1" == --output ]]; then cp "$PUBLISHED_ARCHIVE" "$2"; exit 0; fi
    shift
done
exit 1
""")
    curl.chmod(0o755)
    for tool in ("jq",):
        executable = shutil.which(tool)
        if executable is None:
            pytest.skip(f"requires {tool}")
        (bin_dir / tool).symlink_to(executable)

    # State is isolated outside the copied checkout, just like the upstream default.
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("POLYAD_MINIKUBE_", "MINIKUBE_AUTOSCALER_", "BASH_FUNC_"))
    }
    environment.update(
        PATH=f"{bin_dir}:/usr/bin:/bin",
        XDG_STATE_HOME=str(tmp_path / "state"),
        PUBLISHED_ARCHIVE=str(archive),
        CURL_LOG=str(tmp_path / "curl.log"),
        ADDON_RECORD=str(tmp_path / "addon.log"),
        OPS_LOG=str(tmp_path / "operations.log"),
    )

    def run(command: str, *, script: str = "autoscaler", **overrides: str) -> subprocess.CompletedProcess[str]:
        relative = {
            "autoscaler": ADDON / "autoscaler.sh",
            "minikube": Path("integrations/minikube/minikube.sh"),
            "full": Path("integrations/minikube/full/full.sh"),
        }[script]
        return subprocess.run(
            ["bash", str(root / relative), command],
            cwd=tmp_path,
            env={**environment, **overrides},
            capture_output=True,
            text=True,
            check=False,
        )

    return root, environment, run


@pytest.mark.parametrize("command", ["build", "init", "bridge", "enable", "test", "status", "render", "disable", "resume"])
def test_delegate_commands_and_polyad_settings(checkout: tuple[Path, dict[str, str], Runner], command: str) -> None:
    """
    Forward upstream lifecycle commands without reimplementing provider behavior.
    """
    root, environment, run = checkout
    result = run(
        command,
        POLYAD_MINIKUBE_PROFILE="lab-test",
        POLYAD_MINIKUBE_AUTOSCALER_CONFIG="/private/lab.json",
        MINIKUBE_AUTOSCALER_STATE_DIR="/private/lab-state",
        MINIKUBE_AUTOSCALER_IMAGE="example/provider:reviewed",
        ADDON_EXIT="17",
    )
    assert result.returncode == 17, result.stderr
    assert Path(environment["ADDON_RECORD"]).read_text().splitlines() == [
        command,
        "lab-test",
        "/private/lab-state",
        "/private/lab.json",
        str(root / f".cache/minikube/addons/minikube-cluster-autoscaler-addon/{REVISION}/bin/minikube-cluster-autoscaler-addon"),
        "example/provider:reviewed",
    ]


def test_pinned_fetch_is_cached_and_path_is_offline(checkout: tuple[Path, dict[str, str], Runner]) -> None:
    """
    Fetch once by immutable revision and keep cluster state independent of the cache.
    """
    _, environment, run = checkout
    path = run("path")
    assert path.returncode == 0, path.stderr
    assert not Path(environment["CURL_LOG"]).exists()
    fetched = run("fetch")
    assert fetched.returncode == 0, fetched.stderr
    assert fetched.stdout == path.stdout
    assert (Path(fetched.stdout.strip()) / "scripts/addon.sh").is_file()
    assert not Path(environment["ADDON_RECORD"]).exists()
    calls = Path(environment["CURL_LOG"]).read_text()
    assert f"https://codeload.github.com/astrivant/minikube-cluster-autoscaler-addon/tar.gz/{REVISION}" in calls
    assert "--proto-redir\n=https\n" in calls
    assert run("build").returncode == 0
    assert Path(environment["CURL_LOG"]).read_text() == calls
    record = Path(environment["ADDON_RECORD"]).read_text().splitlines()
    assert record[1:3] == ["polyad", f"{environment['XDG_STATE_HOME']}/minikube-cluster-autoscaler-addon/polyad"]


def test_checksum_failure_never_executes_or_extracts(checkout: tuple[Path, dict[str, str], Runner]) -> None:
    """
    Reject a damaged or substituted archive before exposing an executable source tree.
    """
    _, environment, run = checkout
    path = Path(run("path").stdout.strip())
    Path(environment["PUBLISHED_ARCHIVE"]).write_bytes(b"untrusted replacement")
    result = run("build")
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not path.exists()
    assert not Path(environment["ADDON_RECORD"]).exists()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"POLYAD_MINIKUBE_PROFILE": "../escape"}, "Invalid"),
        ({"MINIKUBE_AUTOSCALER_PROFILE": "another-cluster"}, "profiles differ"),
        ({"MINIKUBE_AUTOSCALER_STATE_DIR": "relative"}, "absolute"),
        ({"MINIKUBE_AUTOSCALER_BINARY": "relative"}, "absolute"),
        ({"POLYAD_MINIKUBE_AUTOSCALER_CONFIG": "/one.json", "MINIKUBE_AUTOSCALER_CONFIG": "/two.json"}, "configuration paths differ"),
    ],
)
def test_reject_ambiguous_settings(checkout: tuple[Path, dict[str, str], Runner], overrides: dict[str, str], message: str) -> None:
    """
    Avoid silently targeting another cluster, state directory or configuration.
    """
    _, environment, run = checkout
    result = run("init", **overrides)
    assert result.returncode != 0
    assert message in result.stderr
    assert not Path(environment["ADDON_RECORD"]).exists()
    assert not Path(environment["CURL_LOG"]).exists()


def lifecycle_owner(checkout: tuple[Path, dict[str, str], Runner]) -> Path:
    """
    Install a published-addon journal without starting infrastructure.
    """
    _, environment, _ = checkout
    state = Path(environment["XDG_STATE_HOME"]) / "minikube-cluster-autoscaler-addon/polyad"
    journal = state / "provider/state.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"ClusterUID":"test-cluster","Workers":[]}')
    return journal


def test_deletion_still_requires_explicit_shutdown(checkout: tuple[Path, dict[str, str], Runner]) -> None:
    """
    Automatic maintenance never authorizes deleting an initialized cluster.
    """
    root, environment, run = checkout
    journal = lifecycle_owner(checkout)
    mock = root.parent / "bin/minikube"
    mock.write_text('#!/usr/bin/env bash\nprintf "minikube called\\n" >> "$OPS_LOG"\n')
    mock.chmod(0o755)
    result = run("delete", script="minikube")
    assert result.returncode != 0
    assert "ownership is initialized" in result.stderr
    assert not Path(environment["OPS_LOG"]).exists()
    assert journal.exists()


def test_start_reenters_under_maintenance_and_preserves_dynamic_size(checkout: tuple[Path, dict[str, str], Runner]) -> None:
    """
    Execute the complete shell startup using the real lock helper and fake infrastructure.
    """
    root, environment, run = checkout
    journal = lifecycle_owner(checkout)
    state = journal.parent.parent
    (state / "host").mkdir()
    (state / "config.json").write_text('{"profile":"polyad"}')
    binary = root / f".cache/minikube/addons/minikube-cluster-autoscaler-addon/{REVISION}/bin/minikube-cluster-autoscaler-addon"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/usr/bin/env bash\nexit 0\n")
    binary.chmod(0o755)
    bin_dir = root.parent / "bin"
    recorder = bin_dir / "record"
    shutil.copyfile(ROOT / "pkg/tests/data/local_cluster_command.py", recorder)
    recorder.chmod(0o755)
    for tool in ("docker", "helm", "minikube", "kubectl", "virsh", "uname", "curl"):
        target = bin_dir / tool
        target.unlink(missing_ok=True)
        target.symlink_to(recorder)

    # Chart and registry commands are recorded, not run; supply the checked-in
    # inputs that the shell itself resolves before calling those commands.
    for relative in (
        "scripts/tooling/build-chart-dependencies.sh",
        "charts/polyad/Chart.lock",
        "integrations/minikube/values.yaml",
        "integrations/minikube/workload.yaml",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    result = run("start", script="minikube", COMMAND_LOG=environment["OPS_LOG"], MINIKUBE_TEST_NODES="5")
    assert result.returncode == 0, result.stderr
    assert "Pausing autoscaler" in result.stdout
    assert "Autoscaler restored" in result.stdout
    calls = [json.loads(line)["command"] for line in Path(environment["OPS_LOG"]).read_text().splitlines()]
    assert sum(call[:4] == ["minikube", "--profile", "polyad", "start"] for call in calls) == 1
    assert not any("node" in call and "add" in call for call in calls)
    assert not (state / "host/polyad-maintenance.json").exists()


def test_pin_is_immutable_and_implementation_is_external() -> None:
    """
    Do not reintroduce the provider, its chart, or its CI build into Polyad.
    """
    lock = json.loads((ROOT / ADDON / "source.lock.json").read_text())
    assert lock["repository"] == "astrivant/minikube-cluster-autoscaler-addon"
    assert len(lock["revision"]) == 40 and int(lock["revision"], 16)
    assert len(lock["sha256"]) == 64 and int(lock["sha256"], 16)
    for path in ("pkg/minikube-cluster-autoscaler", "services/minikube-cluster-autoscaler", str(ADDON / "chart")):
        assert not any(item.is_file() and ".tgz" != item.suffix for item in (ROOT / path).rglob("*"))
    workflow = (ROOT / ".github/workflows/stage-test.yml").read_text()
    assert "pkg/minikube-cluster-autoscaler" not in workflow
    assert "pkg/tests/flux/go.mod" in workflow


@pytest.mark.parametrize("ownership", ["upstream", "missing"])
def test_full_lab_never_promotes_elastic_workers_to_base(checkout: tuple[Path, dict[str, str], Runner], ownership: str) -> None:
    """
    Use the published addon journal for placement and refuse missing ownership.
    """
    root, environment, run = checkout
    if ownership == "upstream":
        journal = lifecycle_owner(checkout)
        journal.write_text('{"Base":{"polyad":{},"polyad-m02":{}},"Workers":[{"Name":"polyad-m04"}]}')

    # Stop at chart preparation after recording placement. No Helm rollout or
    # credential creation is needed to exercise this early ownership boundary.
    bin_dir = root.parent / "bin"
    for tool in ("kubectl", "minikube"):
        mock = bin_dir / tool
        mock.write_text("""#!/usr/bin/env bash
printf '%s %s\\n' "${0##*/}" "$*" >> "$OPS_LOG"
case "$*" in
    *'get nodes -o json')
        printf '%s\\n' '{"items":[{"metadata":{"name":"polyad-m04","labels":{"minikube-autoscaler.astrivant.com/pool":"elastic"}}}]}' ;;
    *'get node polyad -o json') printf '%s\\n' '{"metadata":{"labels":{"node-role.kubernetes.io/control-plane":""}}}' ;;
    *'get node polyad-m02 -o json') printf '%s\\n' '{"metadata":{"labels":{}}}' ;;
esac
""")
        mock.chmod(0o755)
    sentinel = root / "scripts/tooling/build-chart-dependencies.sh"
    sentinel.write_text('#!/usr/bin/env bash\nprintf "chart preparation reached\\n" >&2\nexit 19\n')
    result = run("enable", script="full")
    commands = Path(environment["OPS_LOG"]).read_text()
    assert "label node polyad-m04" not in commands
    if ownership == "upstream":
        assert result.returncode == 19, result.stderr
        assert "label node polyad minikube-autoscaler.astrivant.com/pool=base" in commands
        assert "label node polyad-m02 minikube-autoscaler.astrivant.com/pool=base" in commands
        assert "label node polyad-m02 polyad.astrivant.com/minikube-worker=true" in commands
    else:
        assert result.returncode != 0
        assert "label node" not in commands
        assert "chart preparation reached" not in result.stderr
