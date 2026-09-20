"""
Run and publish bounded process studies through the existing refresh protocol.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from polyad_benchmarks.refresh import write_json

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from polyad_benchmarks.studies.soul.runtime.engine import PopulationMonitor


def validate(config: dict[str, Any]) -> None:
    """
    Reject unbounded recipes before creating processes or output files.

    Args:
        config (dict[str, Any]): Prepared local process-study recipe.

    Returns:
        None: All work and timing budgets are finite and within local study ceilings.
    """
    for name, low, high in (("workSeconds", 0.001, 0.2), ("timeoutSeconds", 5, 180)):
        value = config[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} must be finite and in [{low}, {high}]")
    service_level = config["serviceLevel"]
    for name, low, high in (
        ("availability", 0, 1),
        ("latencyP99Seconds", 0.001, 60),
        ("maximumAdaptationSeconds", 0.01, 60),
    ):
        value = service_level[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"serviceLevel.{name} must be finite and in [{low}, {high}]")
    resources = config["resourceLoop"]
    integer_fields = (
        "minMemoryBytes",
        "maxMemoryBytes",
        "initialMemoryBytes",
        "stepMemoryBytes",
        "baseMemoryBytes",
        "workerMemoryBytes",
        "queuedJobMemoryBytes",
    )
    if any(type(resources[name]) is not int or resources[name] <= 0 for name in integer_fields):
        raise ValueError("resource-loop byte values must be positive integers")
    if not resources["minMemoryBytes"] <= resources["initialMemoryBytes"] <= resources["maxMemoryBytes"]:
        raise ValueError("resource-loop initial memory must be within min and max bounds")
    for name, low, high in (("targetUtilization", 0.1, 0.9), ("intervalSeconds", 0.05, 10)):
        value = resources[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"resourceLoop.{name} must be finite and in [{low}, {high}]")
    if [phase["name"] for phase in config["phases"]] != ["before", "surge", "constraints", "after"]:
        raise ValueError("use the four ordered before/surge/constraints/after phases")
    for phase in config["phases"]:
        for name, low, high in (("seconds", 0.2, 10), ("rate", 1, 500)):
            value = phase[name]
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"phase {name} must be finite and in [{low}, {high}]")
        if phase["output"] not in {"squared", "enriched"}:
            raise ValueError("unsupported output contract")


def execute(study: str, config: dict[str, Any], output: Path) -> dict[str, Any]:
    """
    Measure fixed and adaptive trials with fresh processes and the same load recipe.

    Args:
        study (str): Soul or Nature.
        config (dict[str, Any]): Validated finite scenario.
        output (Path): Destination for raw evidence and rendered figures.

    Returns:
        dict[str, Any]: Completed run with real measurements and artifact names.
    """
    from polyad_benchmarks.studies.artifacts import fingerprint, verify
    from polyad_benchmarks.studies.soul.plotting import render

    monitor: type[PopulationMonitor]
    if study == "soul":
        from polyad_benchmarks.studies.soul.monitor import Monitor as SoulMonitor

        monitor = SoulMonitor
    elif study == "nature":
        from polyad_benchmarks.studies.nature.monitor import Monitor as NatureMonitor

        monitor = NatureMonitor
    else:
        raise ValueError("select soul or nature")

    validate(config)
    records = []

    # Reuse the recipe, not the runtime: each mode owns fresh processes and independent counters.
    for adaptive in (False, True):
        experiment = monitor(config, adaptive)
        try:
            record = experiment.run()
            records.append(record)
            write_json(output / f"{record['mode']}.json", record)
        finally:
            experiment.close()
    result = {
        "study": study,
        "runId": config["runId"],
        "complete": True,
        "records": records,
        "environment": {"python": sys.version, "platform": platform.platform()},
        "recipe": config,
    }
    write_json(output / "results.json", result)
    result["figures"] = render(study, records, output)
    result["artifacts"] = fingerprint(output)
    verify(result, config, output)
    write_json(output / "results.json", result)
    return result


def run(project: Path, root: Path, study: str) -> None:
    """
    Isolate a whole process tree and retain stdout/stderr even if the study fails.

    Args:
        project (Path): Checkout containing the original Soul and Nature modules.
        root (Path): Prepared refresh directory.
        study (str): Registered process study name.

    Returns:
        None: Measurements and figures are available for verified publication.
    """
    config = root / "inputs" / f"{study}.json"
    output = root / "outputs" / study
    output.mkdir(parents=True, exist_ok=True)
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(project), *sys.path)), "MPLCONFIGDIR": str(output / "matplotlib")}
    command = [sys.executable, "-m", __name__, "--execute", study, "--recipe", str(config.resolve()), "--output", str(output.resolve())]
    with (logs / f"{study}.log").open("w") as stream:
        process = subprocess.Popen(command, cwd=project, env=environment, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            returncode = process.wait(timeout=1200)
            if returncode:
                raise RuntimeError(f"{study} process study failed; see {logs / (study + '.log')}")
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise


def main(study: str | None = None, argv: Sequence[str] | None = None) -> None:
    """
    Expose direct demo study runs and the isolated refresh execution entry point.

    Args:
        study (str | None): Name supplied by the study package entry point.
        argv (Sequence[str] | None): Explicit CLI arguments; None reads process arguments.

    Returns:
        None: A prepared, verified study is written without changing published results.
    """
    from polyad_benchmarks.refresh import finish, prepare, study_phase

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", choices=("soul", "nature"))
    parser.add_argument("--recipe", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if args.execute:
        if args.recipe is None:
            parser.error("--execute requires --recipe")
        execute(args.execute, json.loads(args.recipe.read_text()), args.output)
    else:
        if study not in {"soul", "nature"}:
            parser.error("run the soul or nature package under polyad_benchmarks.studies")
        prepare(args.project, args.output, (study,))
        study_phase(args.project, args.output, study, "")
        finish(args.project, args.output)
        print(args.output / "outputs" / study / "topology.png")


if __name__ == "__main__":
    main()
