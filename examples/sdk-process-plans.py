"""
Run approved SDK worker plans through idle -> busy -> idle -> shutdown.

The parent feeds illustrative graph resource observations to ThresholdStrategy.
The strategy proposes a profile; ProcessSupervisor starts ready children and
retires removed ones. Each child acknowledges readiness and draining through a
temporary filesystem mailbox. Production services supply their own IPC protocol.

    idle              busy                 idle
    parent -> A       parent -> A, B       parent -> A

Run with `poetry run python examples/sdk-process-plans.py`. Add `--otlp` to export
traces and metrics to an OTLP HTTP collector configured through OTEL variables.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from polyad_sdk import Change, Environment, ProcessPlan, ProcessSpec, ProcessSupervisor, Telemetry, ThresholdStrategy

if TYPE_CHECKING:
    from polyad_sdk import ManagedProcess


def worker(directory: Path, *, export: bool) -> None:
    """
    Advertise readiness and acknowledge settled work before exiting.

    Args:
        directory (Path): Parent-owned readiness and drain mailbox.
        export (bool): Enable this process's independently owned OTLP exporters.

    Returns:
        None: The parent requested draining and the worker acknowledged completion.
    """
    telemetry = Telemetry.otlp(service_name="sdk-plan-worker") if export else Telemetry()
    identity = str(os.getpid())
    try:
        with telemetry.tracer.start_as_current_span("worker.lifecycle", context=telemetry.parent_context()):
            (directory / f"{identity}.ready").touch()
            while not (directory / f"{identity}.drain").exists():
                time.sleep(0.01)
    finally:
        telemetry.close()
    (directory / f"{identity}.drained").touch()


def demonstration(directory: Path, *, export: bool) -> None:
    """
    Keep policy, process lifecycle and telemetry as separately configured components.

    Args:
        directory (Path): Temporary mailbox shared with child processes.
        export (bool): Enable explicit OTLP telemetry in parent and children.

    Returns:
        None: All profiles have committed and every child has stopped.
    """
    telemetry = Telemetry.otlp(service_name="sdk-plan-demo") if export else Telemetry()
    graph_uid = "local-example"
    current = Environment({"graph": {"uid": graph_uid}, "outgoing": []}, {}, {}, True, None)

    def ready(process: ManagedProcess) -> bool:
        return (directory / f"{process.pid}.ready").exists()

    def drain(process: ManagedProcess) -> bool:
        (directory / f"{process.pid}.drain").touch()
        return (directory / f"{process.pid}.drained").exists()

    def activate(processes: tuple[ManagedProcess, ...]) -> None:
        print("Active roles:", [process.spec.name for process in processes], flush=True)

    argv = (sys.executable, str(Path(__file__).resolve()), "--worker", str(directory), *(("--otlp",) if export else ()))
    first, second = (ProcessSpec(name, argv, ready, drain) for name in ("A", "B"))
    supervisor = ProcessSupervisor(
        [ProcessPlan("idle", (first,)), ProcessPlan("busy", (first, second))],
        view=lambda: current,
        activate=activate,
        max_processes=2,
        telemetry=telemetry,
    )

    def propose(profile: str, change: Change, environment: Environment) -> None:
        supervisor.propose(profile)

    strategy = ThresholdStrategy(
        "pods",
        low=1,
        high=4,
        idle="idle",
        busy="busy",
        active=lambda: supervisor.profile or "idle",
        propose=propose,
    )
    observations = telemetry.meter.create_counter("example.observations", unit="{observation}")
    try:
        supervisor.propose("idle")
        for pods in (1, 4, 1):
            before = current
            current = Environment(current.topology, {graph_uid: {"resources": {"pods": pods}, "status": {}}}, {}, True, None)
            with telemetry.operation("example.adapt"):
                observations.add(1)
                strategy.adapt(Change(before, current, (), True, None), current)
            result = supervisor.reconcile()
            print(f"Observed pods={pods}: {result.profile} {result.state}", flush=True)
            if result.state != "Applied":
                raise RuntimeError(result.reason)
    finally:
        try:
            supervisor.close()
        finally:
            telemetry.close()


def main() -> None:
    """
    Select the parent demo or its explicitly constructed worker entry point.

    Returns:
        None: The finite experiment and all subprocess cleanup have completed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--otlp", action="store_true")
    options = parser.parse_args()
    if options.worker is not None:
        worker(options.worker, export=options.otlp)
    else:
        with TemporaryDirectory(prefix="polyad-sdk-plans-") as directory:
            demonstration(Path(directory), export=options.otlp)


if __name__ == "__main__":
    main()
