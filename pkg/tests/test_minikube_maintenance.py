"""
Exercise published-addon pause/resume using fake infrastructure and real file locks.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "integrations/minikube/autoscaler/maintenance.py"
CONTAINER = "a" * 64


@pytest.fixture
def maintenance() -> ModuleType:
    """
    Load the standalone integration helper without requiring Polyad dependencies.
    """
    spec = importlib.util.spec_from_file_location("autoscaler_maintenance", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lab(tmp_path: Path) -> argparse.Namespace:
    """
    Create private addon state and a harmless executable, never real VM resources.
    """
    state = tmp_path / "state"
    (state / "host").mkdir(parents=True)
    (state / "provider").mkdir()
    (state / "config.json").write_text('{"profile":"polyad"}')
    (state / "provider/state.json").write_text('{"ClusterUID":"cluster-one","Workers":null,"Error":"existing-error"}')
    binary = tmp_path / "bridge"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return argparse.Namespace(profile="polyad", state_dir=str(state), binary=str(binary), action="start", command=["unused"])


def container(state: Path, running: bool = True) -> dict:
    """
    Model the published container's identity and narrowly scoped state mount.
    """
    return {
        "Id": CONTAINER,
        "Name": "/minikube-cluster-autoscaler-addon-polyad",
        "State": {"Running": running, "Paused": False},
        "Mounts": [{"Type": "bind", "Source": str(state / "provider"), "Destination": "/state"}],
    }


@pytest.mark.parametrize("action", ["start", "recover", "stop"])
@pytest.mark.parametrize("active", [True, False])
def test_pause_and_restore_previous_state(
    maintenance: ModuleType, lab: argparse.Namespace, monkeypatch: pytest.MonkeyPatch, action: str, active: bool
) -> None:
    """
    Stop only active components, lock all VM work, and leave disabled components disabled.
    """
    lab.action = action
    state = Path(lab.state_dir)
    info = container(state, running=active)
    events = []
    monkeypatch.setattr(maintenance, "container_info", lambda *_: info)
    monkeypatch.setattr(maintenance, "bridge_pid", lambda *_: 12345 if active else None)
    monkeypatch.setattr(maintenance.os, "kill", lambda pid, sig: events.append(("signal", pid, sig)))

    def output(command, **kwargs):
        events.append(command)
        if command[:2] == ["docker", "stop"]:
            info["State"]["Running"] = False
        return ""

    def child(command, lock):
        assert lock.fileno() >= 3

        # Another descriptor must not acquire the bridge lock during Minikube work.
        with (state / "host/bridge.lock").open("a+b") as contender:
            assert not maintenance.acquire(contender)
        events.append("VM command")
        return 0

    def restore(receipt, binary, directory):
        assert receipt["providerRunning"] is active
        assert receipt["bridgeRunning"] is active
        assert receipt["container"] == CONTAINER
        with (state / "host/bridge.lock").open("a+b") as contender:
            assert maintenance.acquire(contender)
        events.append("restore")

    # A stopped bridge means the lookup acquired its lock, just like the real helper.
    if not active:
        monkeypatch.setattr(maintenance, "bridge_pid", lambda lock, *_: None if maintenance.acquire(lock) else 12345)
    monkeypatch.setattr(maintenance, "output", output)
    monkeypatch.setattr(maintenance, "run_child", child)
    monkeypatch.setattr(maintenance, "restore", restore)
    assert maintenance.maintain(lab) == 0
    if active:
        assert events[:2] == [["docker", "stop", "--timeout", "30", CONTAINER], ("signal", 12345, signal.SIGTERM)]
    else:
        assert not any(isinstance(event, (tuple, list)) for event in events)
    receipt = state / "host/polyad-maintenance.json"
    assert receipt.exists() == (action == "stop")
    assert events[-1] == ("VM command" if action == "stop" else "restore")
    assert json.loads((state / "provider/state.json").read_text())["Error"] == "existing-error"


def test_failed_start_keeps_receipt_and_retry_restores_original_intent(
    maintenance: ModuleType, lab: argparse.Namespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Remember a previously running provider across failed startup and a successful retry.
    """
    state = Path(lab.state_dir)
    info = container(state)
    restored = []
    monkeypatch.setattr(maintenance, "container_info", lambda *_: info)

    def output(command, **kwargs):
        info["State"]["Running"] = False
        return ""

    monkeypatch.setattr(maintenance, "output", output)
    monkeypatch.setattr(maintenance, "run_child", lambda *_: 43)
    monkeypatch.setattr(maintenance, "restore", lambda receipt, *_: restored.append(receipt))
    assert maintenance.maintain(lab) == 43
    assert not restored
    receipt = state / "host/polyad-maintenance.json"
    assert json.loads(receipt.read_text())["providerRunning"] is True
    assert receipt.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(maintenance, "run_child", lambda *_: 0)
    assert maintenance.maintain(lab) == 0
    assert restored[0]["providerRunning"] is True
    assert not receipt.exists()


@pytest.mark.parametrize("phase", ["creating", "deleting", "marking"])
def test_pending_operations_abort_before_any_stop(
    maintenance: ModuleType, lab: argparse.Namespace, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """
    Do not abort an in-flight VM operation merely to run a new lifecycle command.
    """
    state = Path(lab.state_dir)
    (state / "provider/state.json").write_text(json.dumps({"ClusterUID": "cluster-one", "Workers": [{"Phase": phase}]}))
    monkeypatch.setattr(maintenance, "container_info", lambda *_: pytest.fail("infrastructure queried despite pending work"))
    with pytest.raises(RuntimeError, match="pending"):
        maintenance.maintain(lab)
    assert not (state / "host/polyad-maintenance.json").exists()


def test_operation_racing_with_pause_prevents_bridge_shutdown(
    maintenance: ModuleType, lab: argparse.Namespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Recheck the journal after provider shutdown before touching the bridge or VMs.
    """
    state = Path(lab.state_dir)
    info = container(state)
    monkeypatch.setattr(maintenance, "container_info", lambda *_: info)
    monkeypatch.setattr(maintenance, "bridge_pid", lambda *_: 12345)
    monkeypatch.setattr(maintenance.os, "kill", lambda *_: pytest.fail("bridge interrupted during a worker operation"))
    monkeypatch.setattr(maintenance, "run_child", lambda *_: pytest.fail("VM work started despite a pending operation"))

    def output(command, **kwargs):
        info["State"]["Running"] = False
        (state / "provider/state.json").write_text('{"ClusterUID":"cluster-one","Workers":[{"Phase":"creating"}]}')
        return ""

    monkeypatch.setattr(maintenance, "output", output)
    with pytest.raises(RuntimeError, match="pending"):
        maintenance.maintain(lab)


@pytest.mark.parametrize("bad", ["mount", "name", "id", "paused"])
def test_container_verification_fails_closed(
    maintenance: ModuleType, lab: argparse.Namespace, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """
    A matching Docker name alone does not authorize stopping a container.
    """
    info = container(Path(lab.state_dir))
    if bad == "mount":
        info["Mounts"][0]["Source"] = "/unrelated/state"
    elif bad == "name":
        info["Name"] = "/unrelated"
    elif bad == "id":
        info["Id"] = "b" * 64
    else:
        info["State"]["Paused"] = True
    monkeypatch.setattr(maintenance, "output", lambda command, **_: CONTAINER if "ls" in command else json.dumps([info]))
    with pytest.raises(RuntimeError, match="unexpected"):
        maintenance.container_info("polyad", Path(lab.state_dir))


@pytest.mark.parametrize("identity", ["correct", "wrong-user", "wrong-mode", "wrong-state", "wrong-binary", "multiple"])
def test_bridge_identity_is_exact_and_profile_scoped(
    maintenance: ModuleType, lab: argparse.Namespace, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    """
    Verify the lock owner before allowing a targeted SIGTERM.
    """
    state, binary = Path(lab.state_dir), Path(lab.binary)
    command = f"{binary} --mode=bridge --config=/private/edited.json --state-dir={state}"
    uid = os.getuid()
    if identity == "wrong-user":
        uid += 1
    elif identity == "wrong-mode":
        command = command.replace("mode=bridge", "mode=serve")
    elif identity == "wrong-state":
        command += "-different"
    elif identity == "wrong-binary":
        command = "/other" + command
    monkeypatch.setattr(maintenance, "acquire", lambda *_: False)
    monkeypatch.setattr(
        maintenance,
        "output",
        lambda args, **_: ("12345\n54321" if identity == "multiple" else "12345") if args[0] == "lsof" else f"{uid} {command}",
    )
    with (state / "host/bridge.lock").open("a+b") as lock:
        if identity == "correct":
            assert maintenance.bridge_pid(lock, binary, state) == 12345
        else:
            with pytest.raises(RuntimeError):
                maintenance.bridge_pid(lock, binary, state)


def test_real_child_inherits_and_validates_held_lock(maintenance: ModuleType, lab: argparse.Namespace) -> None:
    """
    Verify the descriptor survives the Python-to-Bash-to-Python reentry path.
    """
    state = Path(lab.state_dir)
    with (state / "host/bridge.lock").open("a+b") as lock:
        assert maintenance.acquire(lock)
        command = ["bash", "-c", 'exec "$@"', "lock-test", sys.executable, str(HELPER), "check", "--state-dir", str(state)]
        assert maintenance.run_child(command, lock) == 0
        with (state / "host/bridge.lock").open("a+b") as contender:
            assert not maintenance.acquire(contender)


def test_environment_flag_without_lock_cannot_bypass_guard(lab: argparse.Namespace) -> None:
    """
    Reject forged or closed maintenance descriptors before any VM commands run.
    """
    result = subprocess.run(
        [sys.executable, str(HELPER), "check", "--state-dir", lab.state_dir],
        env={**os.environ, "POLYAD_MINIKUBE_MAINTENANCE_FD": "9999"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1


def test_concurrent_maintenance_is_rejected(maintenance: ModuleType, lab: argparse.Namespace) -> None:
    """
    A second startup must not pause or restore the first startup's components.
    """
    with (Path(lab.state_dir) / "host/polyad-maintenance.lock").open("a+b") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            maintenance.maintain(lab)


@pytest.mark.parametrize("bridge_running", [True, False])
@pytest.mark.parametrize("provider_running", [True, False])
def test_restore_starts_only_previously_active_components(
    maintenance: ModuleType,
    lab: argparse.Namespace,
    monkeypatch: pytest.MonkeyPatch,
    bridge_running: bool,
    provider_running: bool,
) -> None:
    """
    Detach only a previously running bridge, then start the same provider by immutable ID.
    """
    state, binary = Path(lab.state_dir), Path(lab.binary)
    events = []
    monkeypatch.setattr(maintenance, "container_info", lambda *_: container(state, running=False))
    monkeypatch.setattr(maintenance.subprocess, "Popen", lambda command, **kwargs: events.append((command, kwargs)))
    monkeypatch.setattr(maintenance, "output", lambda command, **_: events.append((command, {})))
    maintenance.restore(
        {"profile": "polyad", "container": CONTAINER, "bridgeRunning": bridge_running, "providerRunning": provider_running},
        binary,
        state,
    )
    if bridge_running:
        command, options = events[0]
        assert command[:2] == [str(binary), "--mode=bridge"]
        assert options["start_new_session"] is True
        assert options["stdin"] == subprocess.DEVNULL
        assert events[1][0][1] == "--mode=bridge-check"
    if provider_running:
        assert events[-2][0] == ["docker", "start", CONTAINER]
        assert events[-1][0][1] == "--mode=check"
    assert len(events) == 2 * (bridge_running + provider_running)


def test_changed_receipt_identity_never_stops_anything(
    maintenance: ModuleType, lab: argparse.Namespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Reject a resume receipt belonging to a different cluster or bridge build.
    """
    state = Path(lab.state_dir)
    receipt = state / "host/polyad-maintenance.json"
    receipt.write_text('{"clusterUID":"a-different-cluster"}')
    monkeypatch.setattr(maintenance, "container_info", lambda *_: None)
    monkeypatch.setattr(maintenance, "run_child", lambda *_: pytest.fail("ran lifecycle with mismatched receipt"))
    with pytest.raises(RuntimeError, match="receipt identity changed"):
        maintenance.maintain(lab)


def test_failed_restore_stops_provider_again(maintenance: ModuleType, lab: argparse.Namespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep scaling paused if its authenticated health check fails after restart.
    """
    state = Path(lab.state_dir)
    commands = []
    monkeypatch.setattr(maintenance, "container_info", lambda *_: container(state, running=False))

    def output(command, **kwargs):
        commands.append(command)
        if "--mode=check" in command:
            raise RuntimeError("provider still unhealthy")
        return ""

    monkeypatch.setattr(maintenance, "output", output)
    with pytest.raises(RuntimeError, match="unhealthy"):
        maintenance.restore(
            {"profile": "polyad", "container": CONTAINER, "bridgeRunning": False, "providerRunning": True}, Path(lab.binary), state
        )
    assert commands[0] == ["docker", "start", CONTAINER]
    assert commands[-1] == ["docker", "stop", "--timeout", "30", CONTAINER]


def test_interruption_signals_only_our_child_group(
    maintenance: ModuleType, lab: argparse.Namespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Forward parent interruption while retaining the exclusive bridge lock through cleanup.
    """
    signals = []
    waits = []

    class Child:
        """
        Simulate a lifecycle child interrupted during its first wait.
        """

        pid = 34567

        def wait(self, timeout=None):
            """
            Interrupt once and confirm cleanup waits for the spawned child.
            """
            waits.append(timeout)
            if timeout is None:
                raise KeyboardInterrupt
            return 0

    monkeypatch.setattr(maintenance.subprocess, "Popen", lambda *_, **__: Child())
    monkeypatch.setattr(maintenance.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    with (Path(lab.state_dir) / "host/bridge.lock").open("a+b") as lock:
        assert maintenance.acquire(lock)
        with pytest.raises(KeyboardInterrupt):
            maintenance.run_child(["unused"], lock)
        with (Path(lab.state_dir) / "host/bridge.lock").open("a+b") as other:
            assert not maintenance.acquire(other)
    assert signals == [(34567, signal.SIGTERM)]
    assert waits == [None, 30]
