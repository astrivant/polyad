"""
Render topology, process changes and measured outcomes from recorded study data.
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

from polyad_benchmarks.studies.plotting import describe_axis, save

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from matplotlib.axes import Axes

COLORS = {"before": "#dceee5", "surge": "#f9dfb8", "constraints": "#f3c6ce", "after": "#dce6f7"}


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
    for axis, phase, title, description in zip(
        axes,
        ("before", "constraints", "after"),
        ("Before: baseline", "During: load + constraints", "After: recovery"),
        (
            "Committed routes and worker PIDs before sustained pressure.",
            "Peak overlap and blocked admission while constraints are active.",
            "Recovered routes and replacement workers after all work drains.",
        ),
        strict=True,
    ):
        frames = [frame for frame in record["frames"] if frame["phase"] == phase]

        # Show the most pressured recorded frame during constraints, and settled frames otherwise.
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
        axis.set(xlim=(-0.8, 9.1), ylim=(-0.7, 12))
        describe_axis(axis, title, description)
        axis.axis("off")
        axis.text(
            0.5,
            -0.025,
            f"{frame['time'] - record['started']:.1f}s | queued at router: {frame['pending']}",
            transform=axis.transAxes,
            ha="center",
            fontsize=10,
        )
    return save(
        figure,
        output,
        "topology",
        study=study,
        note="Arrows carry parent-relayed IPC jobs; green admits, pink waits. Child workers: purple active/starting, gray retiring.",
    )


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
    for axis, title, description in zip(
        axes[0],
        ("Backlog", "Growth, rolling overlap and recovery", "Admission under guards"),
        (
            "Accepted jobs waiting or executing inside each service.",
            "Live and ready children expose rolling overlap and recovery.",
            "Guards pause only new assignments while accepted work drains.",
        ),
        strict=True,
    ):
        describe_axis(axis, title, description)
    axes[0, 1].legend(fontsize=8)
    for axis in axes[-1]:
        axis.set_xlabel("Measured seconds since trial start")
    return save(
        figure,
        output,
        "adaptations",
        study=study,
        note="Green = before, orange = surge, pink = constraints, blue = after.",
    )


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
    for axis, key, title, description in zip(
        axes,
        ("completed", "meanLatencySeconds", "elapsedSeconds"),
        ("Verified jobs", "Mean completion latency (s)", "Whole trial duration (s)"),
        (
            "Only exact, end-to-end checked results count as completed.",
            "Mean spans accepted work from offer through verified result.",
            "Elapsed time includes production, processing, adaptation and drain.",
        ),
        strict=True,
    ):
        values = [record[key] or 0 for record in records]
        bars = axis.bar(labels, values, color=["#9ba7b8", "#38876e"])
        axis.bar_label(bars, fmt="%.2f", padding=4)
        describe_axis(axis, title, description)
        axis.margins(y=0.2)
    paths = save(
        figure,
        output,
        "outcomes",
        study=study,
        note=f"Rejected: fixed {records[0]['rejected']}, adaptive {records[1]['rejected']}; every accepted result verified.",
    )
    coverage = records[1]["coverage"]
    names = sorted({name.split(":")[0] for name in coverage})
    figure, axis = plt.subplots(figsize=(11, 6), layout="constrained")
    left = [0] * len(names)
    for state, color in (("satisfied", "#38876e"), ("blocked", "#b96657"), ("unknown", "#d9ad51"), ("callback", "#6b7fab")):
        values = [coverage.get(name if state == "callback" else f"{name}:{state}", 0) for name in names]
        axis.barh(names, values, left=left, label=state, color=color)
        left = [a + b for a, b in zip(left, values, strict=True)]
    axis.set(xlabel="Actual delivered callbacks / guard evaluations")
    describe_axis(
        axis,
        "SDK delivery and guard coverage",
        "Stacked counts distinguish callbacks from satisfied, blocked and unknown assessments.",
    )
    axis.legend()
    paths += save(figure, output, "strategies", study=study)
    return paths


def service_levels(study: str, records: list[dict[str, Any]], output: Path) -> list[str]:
    """
    Plot customer-outcome objectives beside per-service adaptation state.

    Args:
        study (str): Study title.
        records (list[dict[str, Any]]): Fixed and adaptive trial measurements.
        output (Path): Figure destination.

    Returns:
        list[str]: Service-level figure artifacts.
    """
    figure, axes = plt.subplots(3, 1, figsize=(15, 10), sharex=False, layout="constrained")
    colors = {"fixed": "#7b8798", "adaptive": "#248266"}
    for record in records:
        frames = record["frames"]
        times = [frame["time"] - record["started"] for frame in frames]
        levels = [frame["serviceLevel"] for frame in frames]
        axes[0].plot(times, [level["availability"] for level in levels], label=record["mode"], color=colors[record["mode"]])
        latency_x = [time for time, level in zip(times, levels, strict=True) if level["latencyP99Seconds"] is not None]
        latency_y = [level["latencyP99Seconds"] for level in levels if level["latencyP99Seconds"] is not None]
        axes[1].plot(latency_x, latency_y, label=record["mode"], color=colors[record["mode"]])
    policy = records[1]["serviceLevelPolicy"]
    axes[0].axhline(policy["availability"], color="#a44960", linestyle="--", label="objective")
    axes[0].set(ylabel="Successful / terminal", ylim=(0, 1.04))
    describe_axis(
        axes[0],
        "Population availability",
        "Verified completions are divided by terminal completed or rejected jobs.",
    )
    axes[1].axhline(policy["latencyP99Seconds"], color="#a44960", linestyle="--", label="objective")
    axes[1].set(ylabel="Seconds")
    describe_axis(
        axes[1],
        "Population p99 completion latency",
        "The dashed objective separates compliant latency from degradation.",
    )
    adaptive = records[1]
    names = sorted({sample["service"] for sample in adaptive["samples"]})
    state_colors = {"Compliant": "#38876e", "Degraded": "#d9ad51", "Unavailable": "#b74d62"}
    for row, name in enumerate(names):
        samples = [sample for sample in adaptive["samples"] if sample["service"] == name]
        axes[2].scatter(
            [sample["time"] - adaptive["started"] for sample in samples],
            [row] * len(samples),
            c=[state_colors[sample["serviceLevel"]["state"]] for sample in samples],
            marker="s",
            s=18,
        )
        changing = [sample for sample in samples if sample["adaptationInProgress"]]
        axes[2].scatter(
            [sample["time"] - adaptive["started"] for sample in changing],
            [row] * len(changing),
            facecolors="none",
            edgecolors="#172033",
            s=42,
            label="worker adaptation" if row == 0 and changing else None,
        )
    axes[2].set(
        yticks=range(len(names)),
        yticklabels=names,
        xlabel="Measured seconds since adaptive trial start",
    )
    describe_axis(
        axes[2],
        "Per-service state",
        "Color reports contract state; outlines identify active worker replacement.",
    )
    for axis in axes[:2]:
        axis.grid(alpha=0.2)
        axis.legend(fontsize=8)
    axes[2].grid(axis="x", alpha=0.2)
    if axes[2].get_legend_handles_labels()[0]:
        axes[2].legend(fontsize=8)
    return save(
        figure,
        output,
        "service-level",
        study=study,
        note="Green service marks are compliant, amber degraded and red unavailable; outlined marks indicate an active worker replacement.",
    )


def resources(study: str, record: dict[str, Any], output: Path) -> list[str]:
    """
    Compare modeled vertical allocation with real SDK cgroup observations.

    Args:
        study (str): Study title.
        record (dict[str, Any]): Adaptive trial with resource samples.
        output (Path): Figure destination.

    Returns:
        list[str]: Resource-loop figure artifacts.
    """
    frames = [frame for frame in record["frames"] if frame["services"]]
    times = [frame["time"] - record["started"] for frame in frames]

    def aggregate(key: str) -> list[float]:
        return [
            statistics.median(service[key] for service in frame["services"].values() if service.get(key) is not None) for frame in frames
        ]

    mib = 1024 * 1024
    figure, axes = plt.subplots(2, 2, figsize=(15, 9), sharex=True, layout="constrained")
    assigned = [value / mib for value in aggregate("resourceAssignedBytes")]
    modeled = [value / mib for value in aggregate("resourceModeledUsageBytes")]
    available = [value / mib for value in aggregate("resourceAvailableBytes")]
    axes[0, 0].step(times, assigned, where="post", label="VPA-like admitted allocation", color="#335c81")
    axes[0, 0].plot(times, modeled, label="modeled application use", color="#a44960")
    axes[0, 0].set(ylabel="MiB")
    describe_axis(
        axes[0, 0],
        "Modeled allocation loop",
        "The deterministic controller moves admitted memory within recipe bounds.",
    )
    axes[0, 0].legend(fontsize=8)
    axes[0, 1].plot(times, available, color="#248266")
    axes[0, 1].axhline(0, color="#a44960", linestyle="--")
    axes[0, 1].set(ylabel="MiB")
    describe_axis(
        axes[0, 1],
        "Modeled available memory",
        "Assigned minus modeled use shows headroom available to application policy.",
    )
    cgroup_samples = [sample for sample in record["samples"] if sample["cgroup"]["memoryUsageBytes"] is not None]
    if cgroup_samples:
        cgroup_times = [sample["time"] - record["started"] for sample in cgroup_samples]
        axes[1, 0].plot(
            cgroup_times,
            [sample["cgroup"]["memoryUsageBytes"] / mib for sample in cgroup_samples],
            alpha=0.35,
            color="#72578b",
        )
        limits = [(sample["time"] - record["started"], sample["cgroup"]["memoryLimitBytes"]) for sample in cgroup_samples]
        bounded = [(when, value) for when, value in limits if value is not None]
        if bounded:
            axes[1, 0].step(
                [item[0] for item in bounded],
                [item[1] / mib for item in bounded],
                where="post",
                color="#172033",
                label="live cgroup limit",
            )
            axes[1, 0].legend(fontsize=8)
    else:
        axes[1, 0].text(0.5, 0.5, "cgroup v2 values unavailable on this host", transform=axes[1, 0].transAxes, ha="center")
    axes[1, 0].set(ylabel="MiB")
    describe_axis(
        axes[1, 0],
        "SDK-observed real cgroup",
        "Only SDK values read from cgroup v2 appear here; missing data stays missing.",
    )
    axes[1, 1].step(
        times,
        [sum(service.get("workers", 0) for service in frame["services"].values()) for frame in frames],
        where="post",
        color="#72578b",
        label="live workers",
    )
    axes[1, 1].step(
        times,
        [sum(bool(service.get("adaptationInProgress")) for service in frame["services"].values()) for frame in frames],
        where="post",
        color="#d47a49",
        label="services adapting",
    )
    axes[1, 1].set(ylabel="Processes / services")
    describe_axis(
        axes[1, 1],
        "Application follow-along",
        "Worker and adaptation counts show the application reacting to changing headroom.",
    )
    axes[1, 1].legend(fontsize=8)
    for axis in axes.flat:
        axis.grid(alpha=0.2)
    for axis in axes[-1]:
        axis.set_xlabel("Measured seconds since adaptive trial start")
    return save(
        figure,
        output,
        "resources",
        study=study,
        note="The admitted allocation is a local VPA analogue; the cgroup panel alone is sampled from the kernel through the SDK.",
    )


def render(study: str, records: list[dict[str, Any]], output: Path) -> list[str]:
    """
    Produce all figures exclusively from completed trial evidence.

    Args:
        study (str): Registered study name.
        records (list[dict[str, Any]]): Verified fixed and adaptive measurements.
        output (Path): Figure destination.

    Returns:
        list[str]: Twelve image/vector artifacts for inspection and publication.
    """
    return (
        topology(study, records[1], output)
        + timeline(study, records[1], output)
        + comparison(study, records, output)
        + service_levels(study, records, output)
        + resources(study, records[1], output)
    )
