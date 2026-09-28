"""
Pause the published addon around Polyad's local VM lifecycle, retaining ownership.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import FrameType
    from typing import Any, BinaryIO

__all__ = ["main"]


def output(command: list[str], timeout: int = 40) -> str:
    """
    Capture a bounded infrastructure query without exposing successful output.

    Args:
        command (list[str]): Executable and literal arguments.
        timeout (int): Maximum command duration in seconds.

    Returns:
        str: Stripped standard output.
    """
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def read_json(path: Path) -> dict[str, Any]:
    """
    Read one JSON object, rejecting malformed or unexpectedly typed state.

    Args:
        path (Path): Private state document.

    Returns:
        dict[str, Any]: Decoded object.
    """
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return value


def save_receipt(path: Path, value: dict[str, Any]) -> None:
    """
    Persist restart intent before pausing either component, without changing addon journals.

    Args:
        path (Path): Polyad's private maintenance receipt.
        value (dict[str, Any]): Original component state and ownership identity.

    Returns:
        None: The complete receipt replaces any previous receipt atomically.
    """
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)


def quiescent(state: Path) -> dict[str, Any]:
    """
    Refuse VM maintenance while the addon has unfinished worker operations.

    Args:
        state (Path): Published addon's state directory.

    Returns:
        dict[str, Any]: Validated provider journal; existing errors remain untouched.
    """
    journal = read_json(state / "provider/state.json")
    workers = journal["Workers"]

    # Go serializes an untouched nil slice as null, and an emptied pool as [].
    if workers is None:
        workers = []
    if not isinstance(workers, list) or any(not isinstance(worker, dict) or worker.get("Phase") != "ready" for worker in workers):
        raise RuntimeError("Resolve pending autoscaler worker operations before VM maintenance")
    if not journal.get("ClusterUID"):
        raise RuntimeError("Autoscaler journal has no cluster identity")
    return journal


def container_info(profile: str, state: Path) -> dict[str, Any] | None:
    """
    Resolve the exact addon container and verify its state mount before stopping it.

    Args:
        profile (str): Selected Minikube profile.
        state (Path): Published addon's state directory.

    Returns:
        dict[str, Any] | None: Container metadata, or None when it is absent.
    """
    name = f"minikube-cluster-autoscaler-addon-{profile}"
    identifier = output(["docker", "container", "ls", "--all", "--no-trunc", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"])
    if not identifier:
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise RuntimeError("Cannot resolve exactly one autoscaler container")
    info: dict[str, Any] = json.loads(output(["docker", "container", "inspect", identifier]))[0]
    mounts = [mount for mount in info["Mounts"] if mount["Destination"] == "/state"]
    if (
        info["Id"] != identifier
        or info["Name"] != f"/{name}"
        or len(mounts) != 1
        or mounts[0]["Type"] != "bind"
        or Path(mounts[0]["Source"]).resolve() != state / "provider"
        or info["State"].get("Paused")
    ):
        raise RuntimeError("Autoscaler container identity, state mount or pause state is unexpected")
    return info


def acquire(stream: BinaryIO) -> bool:
    """
    Try to hold the exact exclusive lock used by the upstream native bridge.

    Args:
        stream (BinaryIO): Open lock file, retained by the maintenance parent.

    Returns:
        bool: True when maintenance owns the lock; False while a bridge owns it.
    """
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def bridge_pid(stream: BinaryIO, binary: Path, state: Path) -> int | None:
    """
    Identify only a same-user bridge holding this profile's lock, without name-based kills.

    Args:
        stream (BinaryIO): Bridge lock file.
        binary (Path): Configured native bridge executable.
        state (Path): Published addon's state directory.

    Returns:
        int | None: Verified bridge PID, or None if maintenance acquired the lock.
    """
    if acquire(stream):
        return None
    pids = output(["lsof", "-t", str(state / "host/bridge.lock")]).splitlines()

    # lsof includes our own open descriptor; that descriptor does not yet own the lock.
    pids = sorted(set(pid for pid in pids if pid != str(os.getpid())))
    if len(pids) != 1 or not pids[0].isdigit():
        raise RuntimeError("Cannot identify one native bridge lock owner; stop its supervisor before retrying")
    pid = int(pids[0])
    identity = output(["ps", "-ww", "-p", str(pid), "-o", "uid=", "-o", "command="]).split(maxsplit=1)
    if (
        len(identity) != 2
        or identity[0] != str(os.getuid())
        or not identity[1].startswith(f"{binary} --mode=bridge --config=")
        or not identity[1].endswith(f" --state-dir={state}")
    ):
        raise RuntimeError("Unrecognized bridge process; set MINIKUBE_AUTOSCALER_BINARY correctly or stop it manually")
    return pid


def run_child(command: list[str], stream: BinaryIO) -> int:
    """
    Run Minikube under the held bridge lock and forward interruptions to our child group.

    Args:
        command (list[str]): Original Polyad lifecycle invocation.
        stream (BinaryIO): Exclusively locked bridge file, inherited by the child.

    Returns:
        int: Lifecycle command's exit status.
    """
    environment = {**os.environ, "POLYAD_MINIKUBE_MAINTENANCE_FD": str(stream.fileno())}
    child = subprocess.Popen(command, env=environment, pass_fds=(stream.fileno(),), start_new_session=True)
    try:
        return child.wait()
    except BaseException:
        # Only this command's new process group is signaled. Keep the lock until
        # its processes have stopped, even when the parent received SIGTERM alone.
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        except ProcessLookupError:
            child.wait()
        raise


def restore(receipt: dict[str, Any], binary: Path, state: Path) -> None:
    """
    Restore only components that were running before maintenance, without recreating them.

    Args:
        receipt (dict[str, Any]): Persisted original component state.
        binary (Path): Native bridge executable.
        state (Path): Published addon's state directory.

    Returns:
        None: Previously active components have been restarted and checked.
    """
    arguments = [f"--config={state / 'config.json'}", f"--state-dir={state}"]
    if receipt["bridgeRunning"]:
        with (state / "host/polyad-bridge.log").open("ab") as log:
            subprocess.Popen(
                [str(binary), "--mode=bridge", *arguments],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        # Authentication checks the intended bridge, not just an open TCP port.
        for attempt in range(3):
            try:
                output([str(binary), "--mode=bridge-check", *arguments], timeout=20)
                break
            except RuntimeError:
                if attempt == 2:
                    raise
                time.sleep(1)
    if receipt["providerRunning"]:
        info = container_info(receipt["profile"], state)
        if info is None or info["Id"] != receipt["container"]:
            raise RuntimeError("Autoscaler container changed during maintenance; refusing to start a replacement")
        try:
            output(["docker", "start", info["Id"]])
            output([str(binary), "--mode=check", *arguments], timeout=20)
        except BaseException:
            # A failed or interrupted health check must not leave scaling active
            # while the maintenance command reports a failed restoration.
            output(["docker", "stop", "--timeout", "30", info["Id"]])
            raise


def maintain(args: argparse.Namespace) -> int:
    """
    Serialize pause, VM lifecycle and restoration while preserving failed restart intent.

    Args:
        args (argparse.Namespace): Validated CLI options and lifecycle command.

    Returns:
        int: Zero on success, otherwise the lifecycle command's failure code.
    """
    state, binary = Path(args.state_dir).resolve(), Path(args.binary).absolute()
    if read_json(state / "config.json").get("profile") != args.profile:
        raise RuntimeError("Autoscaler configuration profile differs from the selected Minikube profile")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError(f"Restore the configured bridge executable before VM maintenance: {binary}")
    receipt_path = state / "host/polyad-maintenance.json"
    with (state / "host/polyad-maintenance.lock").open("a+b") as guard, (state / "host/bridge.lock").open("a+b") as lock:
        if not acquire(guard):
            raise RuntimeError("Another Polyad VM maintenance command is already running")
        journal = quiescent(state)
        info = container_info(args.profile, state)
        pid = bridge_pid(lock, binary, state)
        identity = {"profile": args.profile, "state": str(state), "binary": str(binary), "clusterUID": journal["ClusterUID"]}
        receipt = {
            **identity,
            "container": info["Id"] if info else None,
            "providerRunning": bool(info and info["State"]["Running"]),
            "bridgeRunning": pid is not None,
        }
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            if (
                any(receipt.get(key) != value for key, value in identity.items())
                or receipt.get("container") != (info["Id"] if info else None)
                or any(type(receipt.get(key)) is not bool for key in ("providerRunning", "bridgeRunning"))
            ):
                raise RuntimeError("Maintenance receipt identity changed; inspect it before retrying")
        save_receipt(receipt_path, receipt)
        print(f"Pausing autoscaler for Minikube {args.action}; preserving workers and addon state", flush=True)
        if info and info["State"]["Running"]:
            output(["docker", "stop", "--timeout", "30", info["Id"]])
            stopped = container_info(args.profile, state)
            if stopped is None or stopped["Id"] != info["Id"] or stopped["State"]["Running"]:
                raise RuntimeError("Autoscaler container did not stop; refusing VM maintenance")

        # An operation might have begun between the first journal check and
        # container shutdown. Never stop its bridge or touch VMs in that case.
        quiescent(state)
        if pid is not None:
            current = bridge_pid(lock, binary, state)
            if current is not None:
                if current != pid:
                    raise RuntimeError("Bridge lock owner changed during shutdown; retry after stopping its supervisor")
                os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 20
            while not acquire(lock):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Bridge did not release its lock; VMs were not changed")
                time.sleep(0.1)
        quiescent(state)
        status = run_child(args.command, lock)
        if status:
            return status if status > 0 else 128 - status
        if args.action == "stop":
            print("Autoscaler remains paused; the next successful start or recover restores it", flush=True)
            return 0

        # Release ownership only after VM work has completed. Restoring uses the
        # same container ID and persisted config, never Helm install or new keys.
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        restore(receipt, binary, state)
        receipt_path.unlink()
        print("Autoscaler restored to its pre-maintenance state", flush=True)
        return 0


def check_lock(state: Path) -> None:
    """
    Validate inherited lock ownership before the reentered shell changes any VMs.

    Args:
        state (Path): Published addon's state directory.

    Returns:
        None: The inherited descriptor is the locked bridge file.
    """
    descriptor = int(os.environ["POLYAD_MINIKUBE_MAINTENANCE_FD"])
    if descriptor < 3 or not os.path.samestat(os.fstat(descriptor), (state / "host/bridge.lock").stat()):
        raise RuntimeError("Invalid inherited autoscaler maintenance lock")
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    quiescent(state)


def interrupt(signum: int, frame: FrameType | None) -> None:
    """
    Route SIGTERM through the same fail-closed cleanup path as Ctrl-C.

    Args:
        signum (int): Delivered signal number.
        frame (FrameType | None): Interrupted Python frame.

    Returns:
        None: Always raises KeyboardInterrupt.
    """
    raise KeyboardInterrupt


def main() -> int:
    """
    Parse the internal lifecycle interface and report failures without clearing addon errors.

    Returns:
        int: Lifecycle exit code, 130 on interruption, or one on maintenance failure.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    check = modes.add_parser("check")
    check.add_argument("--state-dir", required=True)
    run = modes.add_parser("run")
    run.add_argument("--profile", required=True)
    run.add_argument("--state-dir", required=True)
    run.add_argument("--binary", required=True)
    run.add_argument("--action", choices=("start", "recover", "stop"), required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        if args.mode == "check":
            check_lock(Path(args.state_dir))
            return 0
        if args.command[:1] == ["--"]:
            args.command.pop(0)
        if not args.command:
            parser.error("a lifecycle command is required")
        status = maintain(args)
        if status:
            print("VM lifecycle failed; autoscaler restoration is deferred until a successful retry", file=sys.stderr)
        return status
    except KeyboardInterrupt:
        print("Maintenance interrupted; autoscaler restoration is deferred until a successful retry", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}; no automatic error reset. Inspect addon state before retrying", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
