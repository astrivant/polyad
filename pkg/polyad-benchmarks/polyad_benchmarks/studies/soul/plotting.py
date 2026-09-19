"""
Render topology, process changes and measured outcomes from recorded study data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

COLORS = {"before": "#dceee5", "surge": "#f9dfb8", "constraints": "#f3c6ce", "after": "#dce6f7"}


def save(figure: Figure, output: Path, name: str) -> list[str]:
    """
    Export the same measured figure as a README image and a vector artifact.

    Args:
        figure (Figure): Completed Matplotlib figure.
        output (Path): Artifact directory.
        name (str): Stable figure basename.

    Returns:
        list[str]: Relative PNG and SVG filenames.
    """
    paths = []
    for suffix in ("png", "svg"):
        path = output / f"{name}.{suffix}"
        figure.savefig(path, dpi=160, bbox_inches="tight")
        paths.append(path.name)
    plt.close(figure)
    return paths


def shade(axis: Axes, record: dict[str, Any]) -> None:
    """
    Mark observed phase boundaries on a real elapsed-time axis.

    Args:
        axis (Axes): Plot receiving background phase bands.
        record (dict[str, Any]): One measured trial with phase timestamps.

    Returns:
        None: Background bands include the time spent draining each load.
    """
    for phase in record["phases"]:
        start, end = phase["start"] - record["started"], phase["finish"] - record["started"]
        axis.axvspan(start, end, color=COLORS[phase["name"]], alpha=0.45, zorder=0)
    axis.grid(alpha=0.2)


def topology(study: str, record: dict[str, Any], output: Path) -> list[str]:
    """
    Show actual committed routes and child counts before, during and after change.

    Args:
        study (str): Study title.
        record (dict[str, Any]): Adaptive trial containing timestamped graph frames.
        output (Path): Figure destination.

    Returns:
        list[str]: Rendered topology artifact names.
    """
    figure, axes = plt.subplots(1, 3, figsize=(24, 10), layout="constrained")
    for axis, phase, title in zip(
        axes, ("before", "constraints", "after"), ("Before: baseline", "During: load + constraints", "After: recovery"), strict=True
    ):
        frames = [frame for frame in record["frames"] if frame["phase"] == phase]
        frame = (
            max(
                frames,
                key=lambda item: sum(service.get("workers", 0) + 4 * (not service.get("admit")) for service in item["services"].values()),
            )
            if phase == "constraints"
            else frames[-1]
        )
        positions: dict[str, tuple[float, float]] = {"input": (0, 5.5), "output": (8.3, 5.5)}
        for index, route in enumerate(frame["routes"]):
            for stage, name in enumerate(route):
                positions[name] = (3.7 if len(route) == 1 else 2.3 + stage * 3.2, 11 - index * 2)
        edges = {(left, right) for route in frame["routes"] for left, right in zip(("input", *route), (*route, "output"), strict=True)}
        for left, right in edges:
            axis.add_patch(
                FancyArrowPatch(
                    positions[left],
                    positions[right],
                    arrowstyle="-|>",
                    mutation_scale=11,
                    connectionstyle="arc3,rad=0.05",
                    color="#8995a5",
                    alpha=0.65,
                    shrinkA=22,
                    shrinkB=44,
                )
            )
        for name, point in positions.items():
            if name in {"input", "output"}:
                label = "Router" if name == "input" else "Verified\nresults"
                axis.text(
                    *point, label, ha="center", va="center", fontsize=9, bbox={"boxstyle": "round,pad=.4", "fc": "#edf0f5", "ec": "#78869b"}
                )
                continue
            data = frame["services"][name]
            label = f"{name}: {data['capability']}\n{data.get('profile', 'starting')} | PID {data['pid']}"
            color = "#d3ebdf" if data.get("admit") else "#f3d0d5"
            axis.text(*point, label, ha="center", va="center", fontsize=8, bbox={"boxstyle": "round,pad=.35", "fc": color, "ec": "#607086"})
            children = data.get("children", [])
            for index, child in enumerate(children):
                x, y = point[0] + (index - (len(children) - 1) / 2) * 0.7, point[1] - 0.85
                axis.plot([point[0], x], [point[1] - 0.25, y], color="#b3a4c1", linewidth=1, zorder=0)
                axis.scatter([x], [y], color="#c5c9cf" if child["retiring"] else "#72578b", s=45)
                axis.text(x, y - 0.2, str(child["pid"]), fontsize=6, ha="center", va="top")
        axis.set(xlim=(-0.8, 9.1), ylim=(-0.7, 12), title=title)
        axis.axis("off")
        axis.text(
            0.5,
            -0.025,
            f"{frame['time'] - record['started']:.1f}s | queued at router: {frame['pending']}",
            transform=axis.transAxes,
            ha="center",
            fontsize=10,
        )
    figure.suptitle(
        f"{study.title()}: observed process graph\n"
        "Arrows carry parent-relayed IPC jobs; green admits, pink waits. Child workers: purple active/starting, gray retiring.",
        fontsize=16,
    )
    return save(figure, output, "topology")


def timeline(study: str, record: dict[str, Any], output: Path) -> list[str]:
    """
    Relate real queue measurements to profile changes and admission interventions.

    Args:
        study (str): Study title.
        record (dict[str, Any]): Adaptive trial with per-service samples.
        output (Path): Figure destination.

    Returns:
        list[str]: Timeline artifact names.
    """
    names = sorted({sample["service"] for sample in record["samples"]})
    figure, axes = plt.subplots(len(names), 3, figsize=(16, 2.0 * len(names)), sharex=True, layout="constrained")
    for row, name in enumerate(names):
        samples = [sample for sample in record["samples"] if sample["service"] == name]
        # A restored logical name is a different process. Keep its lifetime
        # separate so the plot does not invent activity while it was retired.
        for index, pid in enumerate(dict.fromkeys(sample["pid"] for sample in samples)):
            incarnation = [sample for sample in samples if sample["pid"] == pid]
            times = [sample["time"] - record["started"] for sample in incarnation]
            axes[row, 0].plot(times, [sample["backlog"] for sample in incarnation], color="#335c81")
            axes[row, 1].step(
                times,
                [sample["workers"] for sample in incarnation],
                where="post",
                color="#72578b",
                label="live children" if index == 0 else None,
            )
            axes[row, 1].step(
                times,
                [sample["readyWorkers"] for sample in incarnation],
                where="post",
                color="#248266",
                label="ready children" if index == 0 else None,
            )
            axes[row, 2].step(times, [int(sample["admit"]) for sample in incarnation], where="post", color="#a44960")
        axes[row, 0].set_ylabel(f"{name}\nqueued jobs")
        axes[row, 1].set_ylim(-0.2, 4.5)
        axes[row, 2].set(yticks=[0, 1], yticklabels=["pause", "admit"], ylim=(-0.1, 1.1))
        for axis in axes[row]:
            shade(axis, record)
    for axis, title in zip(axes[0], ("Backlog", "Growth, rolling overlap and recovery", "Admission under guards"), strict=True):
        axis.set_title(title)
    axes[0, 1].legend(fontsize=8)
    for axis in axes[-1]:
        axis.set_xlabel("Measured seconds since trial start")
    figure.suptitle(f"{study.title()}: how services cope\nGreen = before, orange = surge, pink = constraints, blue = after", fontsize=16)
    return save(figure, output, "adaptations")


def comparison(study: str, records: list[dict[str, Any]], output: Path) -> list[str]:
    """
    Compare fixed and adaptive performance without asserting a predetermined speedup.

    Args:
        study (str): Study title.
        records (list[dict[str, Any]]): Measured fixed and adaptive trials.
        output (Path): Figure destination.

    Returns:
        list[str]: Performance and strategy assessment figures.
    """
    figure, axes = plt.subplots(1, 3, figsize=(14, 4), layout="constrained")
    labels = [record["mode"] for record in records]
    for axis, key, title in zip(
        axes,
        ("completed", "meanLatencySeconds", "elapsedSeconds"),
        ("Verified jobs", "Mean completion latency (s)", "Whole trial duration (s)"),
        strict=True,
    ):
        values = [record[key] or 0 for record in records]
        bars = axis.bar(labels, values, color=["#9ba7b8", "#38876e"])
        axis.bar_label(bars, fmt="%.2f", padding=4)
        axis.set_title(title)
        axis.margins(y=0.2)
    figure.suptitle(
        f"{study.title()}: measured outcomes under the same offered load\n"
        f"Rejected: fixed {records[0]['rejected']}, adaptive {records[1]['rejected']}; every accepted result verified",
        fontsize=13,
    )
    paths = save(figure, output, "outcomes")
    coverage = records[1]["coverage"]
    names = sorted({name.split(":")[0] for name in coverage})
    figure, axis = plt.subplots(figsize=(11, 6), layout="constrained")
    left = [0] * len(names)
    for state, color in (("satisfied", "#38876e"), ("blocked", "#b96657"), ("unknown", "#d9ad51"), ("callback", "#6b7fab")):
        values = [coverage.get(name if state == "callback" else f"{name}:{state}", 0) for name in names]
        axis.barh(names, values, left=left, label=state, color=color)
        left = [a + b for a, b in zip(left, values, strict=True)]
    axis.set(xlabel="Actual delivered callbacks / guard evaluations", title=f"{study.title()}: exercised SDK strategies")
    axis.legend()
    paths += save(figure, output, "strategies")
    return paths


def render(study: str, records: list[dict[str, Any]], output: Path) -> list[str]:
    """
    Produce all figures exclusively from completed trial evidence.

    Args:
        study (str): Registered study name.
        records (list[dict[str, Any]]): Verified fixed and adaptive measurements.
        output (Path): Figure destination.

    Returns:
        list[str]: Eight image/vector artifacts for inspection and publication.
    """
    return topology(study, records[1], output) + timeline(study, records[1], output) + comparison(study, records, output)
