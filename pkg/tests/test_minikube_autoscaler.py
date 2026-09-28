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


@pytest.mark.parametrize("command", ["init", "bridge", "enable", "disable", "resume"])
def test_legacy_journals_require_explicit_handoff(checkout: tuple[Path, dict[str, str], Runner], command: str) -> None:
    """
    Never silently adopt workers or pretend that upstream disable stops the old owner.
    """
    root, environment, run = checkout
    journal = root / ".cache/minikube/autoscaler/polyad/provider/state.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"Workers": {}}')
    result = run(command)
    assert result.returncode != 0
    assert "Legacy autoscaler ownership" in result.stderr
    assert not Path(environment["CURL_LOG"]).exists()
    assert journal.read_text() == '{"Workers": {}}'


def lifecycle_owner(checkout: tuple[Path, dict[str, str], Runner], *, legacy: bool) -> Path:
    """
    Install a journal and harmless provider/container recorders for maintenance tests.
    """
    root, environment, _ = checkout
    state = (
        root / ".cache/minikube/autoscaler/polyad"
        if legacy
        else Path(environment["XDG_STATE_HOME"]) / "minikube-cluster-autoscaler-addon/polyad"
    )
    journal = state / "provider/state.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"Workers": {"worker": {"Phase": "ready"}}}')
    binary = (
        root / ".cache/minikube/autoscaler/bin/provider"
        if legacy
        else root / f".cache/minikube/addons/minikube-cluster-autoscaler-addon/{REVISION}/bin/minikube-cluster-autoscaler-addon"
    )
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/usr/bin/env bash\nprintf "bridge %s\\n" "$*" >> "$OPS_LOG"\nexit "${BRIDGE_EXIT:-0}"\n')
    binary.chmod(0o755)
    bin_dir = root.parent / "bin"
    for tool in ("docker", "minikube"):
        mock = bin_dir / tool
        mock.write_text("""#!/usr/bin/env bash
printf '%s %s\\n' "${0##*/}" "$*" >> "$OPS_LOG"
case "$*" in
    'inspect --format '*) printf '%s\\n' "${CONTAINER_RUNNING:-false}" ;;
    *'profile list'*) printf '%s\\n' '{"valid":[{"Name":"polyad","Config":{"Nodes":[{"ControlPlane":true},{},{},{},{}]}}]}' ;;
esac
""")
        mock.chmod(0o755)
    return journal


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("failure", ["container", "lock", "pending", "delete", "none"])
def test_parent_lifecycle_checks_each_ownership_generation(
    checkout: tuple[Path, dict[str, str], Runner], legacy: bool, failure: str
) -> None:
    """
    Preserve maintenance safety for retained old journals and independently stored new ones.
    """
    _, environment, run = checkout
    journal = lifecycle_owner(checkout, legacy=legacy)
    if failure == "pending":
        journal.write_text('{"Workers": {"worker": {"Phase": "creating"}}}')
    result = run(
        "delete" if failure == "delete" else "stop",
        script="minikube",
        CONTAINER_RUNNING="true" if failure == "container" else "false",
        BRIDGE_EXIT="1" if failure == "lock" else "0",
    )
    log = Path(environment["OPS_LOG"])
    commands = log.read_text() if log.exists() else ""
    assert journal.exists()
    if failure != "none":
        assert result.returncode != 0, result.stdout
        assert "minikube --profile polyad stop" not in commands
        assert "minikube --profile polyad delete" not in commands
    else:
        assert result.returncode == 0, result.stderr
        container = "polyad-minikube-autoscaler-polyad" if legacy else "minikube-cluster-autoscaler-addon-polyad"
        assert f"docker container inspect {container}" in commands
        assert "bridge --mode=maintenance-check" in commands
        assert "minikube --profile polyad profile list" in commands
        assert "minikube --profile polyad stop" in commands


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


@pytest.mark.parametrize("ownership", ["legacy", "upstream", "both", "missing"])
def test_full_lab_never_promotes_elastic_workers_to_base(checkout: tuple[Path, dict[str, str], Runner], ownership: str) -> None:
    """
    Use the correct journal for placement and refuse missing or ambiguous ownership.
    """
    root, environment, run = checkout
    for legacy in (True, False):
        if ownership == "both" or ownership == ("legacy" if legacy else "upstream"):
            journal = lifecycle_owner(checkout, legacy=legacy)
            journal.write_text('{"Base":{"polyad":{},"polyad-m02":{}},"Workers":{"polyad-m04":{}}}')

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
    if ownership in ("legacy", "upstream"):
        assert result.returncode == 19, result.stderr
        assert "label node polyad minikube-autoscaler.astrivant.com/pool=base" in commands
        assert "label node polyad-m02 minikube-autoscaler.astrivant.com/pool=base" in commands
        assert "label node polyad-m02 polyad.astrivant.com/minikube-worker=true" in commands
    else:
        assert result.returncode != 0
        assert "label node" not in commands
        assert "chart preparation reached" not in result.stderr
