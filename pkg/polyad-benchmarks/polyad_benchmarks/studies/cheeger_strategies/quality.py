"""
Separate observable certificate uncertainty from independently audited estimation error.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

import matplotlib.pyplot as plt
import numpy as np

from polyad_benchmarks.studies.cheeger_strategies.plotting import COLORS, lines
from polyad_benchmarks.studies.plotting import describe_axis, save

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

__all__ = ("pid_accuracy",)


def pid_accuracy(result: dict[str, Any], output: Path) -> list[str]:
    """
    Plot a paired accuracy-goal replay without confusing uncertainty with measured error.

    Args:
        result (dict[str, Any]): Measured trajectories, including CPU work and independent exact references.
        output (Path): Destination for PNG and SVG artifacts.

    Returns:
        list[str]: The accuracy-feedback figure artifacts.
    """
    rows = [row for row in result["records"] if row["sweep"] == "pid-accuracy"]
    figure, axes = plt.subplots(3, 2, figsize=(18, 17), layout="constrained")
    panels = (
        (
            "What the accuracy controller can observe",
            r"Certified gap $g=(U-L)/U$; the goal is $g^*=r^*/(1+r^*)$.",
            "Normalized certificate gap",
        ),
        (
            "Actual estimation error, including the tail",
            r"95th percentile of $(U-h)/h$ across paired samples; no PID sees exact $h$.",
            "Relative cut error (95th percentile)",
        ),
        (
            "Requested versus achieved cache reuse",
            r"Solid: requested fraction $q$. Dashed: actual attempts in trailing windows.",
            "Cache-attempt fraction",
        ),
        (
            "How uncertainty moves the outer PID",
            r"Error $1-\overline{g}/g^*$: negative requests less reuse, positive permits more.",
            "Normalized accuracy feedback",
        ),
        (
            "Time paid for the accuracy objective",
            r"Whole-method wall time $W$, including controller work; accuracy is not free.",
            "Seconds / calculation",
        ),
        (
            "CPU work paid for the accuracy objective",
            r"Integrated process CPU $1000C$, excluding the independent oracle.",
            "Millicore-seconds / calculation",
        ),
    )
    for axis, (title, description, ylabel) in zip(axes.flat, panels, strict=True):
        describe_axis(axis, title, description)
        axis.set(xlabel=r"Graph observation $t$", ylabel=ylabel)
        if not rows:
            axis.text(0.5, 0.5, "Accuracy feedback not collected", ha="center", transform=axis.transAxes)
    if not rows:
        return save(figure, output, "pid-accuracy", study="cheeger-strategies")

    # The controller receives only a reduced certificate. The exact oracle
    # remains outside its clock and feedback path, including at phase changes.
    lines(axes[0, 0], rows, "step", "certificateGap")
    goals = {row["step"]: row["relativeErrorTarget"] for row in rows}
    steps = sorted(goals)
    axes[0, 0].step(
        steps, [goals[t] / (1 + goals[t]) for t in steps], where="post", color="#111827", linestyle="--", label="Certificate-gap goal"
    )
    axes[0, 0].set_ylim(-0.05, 1.05)
    axes[0, 0].legend(fontsize=7)

    # A median alone can conceal transient poor cuts when a cached partition
    # crosses a topology transition. Keep the tail visible without calling it a
    # statistical confidence bound or feeding it into the controller.
    groups: dict[tuple[str, int], list[float]] = defaultdict(list)
    for row in rows:
        if row["relativeError"] is not None:
            groups[(row["strategy"], row["step"])].append(row["relativeError"])
    tails = [{"strategy": method, "step": step, "tail": float(np.quantile(errors, 0.95))} for (method, step), errors in groups.items()]
    lines(axes[0, 1], tails, "step", "tail")
    axes[0, 1].step(steps, [goals[t] for t in steps], where="post", color="#111827", linestyle="--", label="Relative-error objective")
    axes[0, 1].set_yscale("symlog", linthresh=0.1)
    axes[0, 1].legend(fontsize=7)

    controlled = [{**row, "target": row["pid"]["targetCacheRate"]} for row in rows if "pid" in row]
    lines(axes[1, 0], controlled, "step", "target")
    window = result["recipe"].get("pidFeedback", {}).get("controller", {}).get("updateEvery", 4)
    histories: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in controlled:
        histories[(row["strategy"], row["seed"], row["repeat"])].append(row)
    rates: dict[tuple[str, int], list[float]] = defaultdict(list)
    for (method, _, _), history in histories.items():
        history.sort(key=lambda item: item["step"])
        for index, row in enumerate(history):
            recent = history[max(0, index + 1 - window) : index + 1]
            rates[(method, row["step"])].append(float(np.mean([item["pid"]["cacheAttempted"] for item in recent])))
    for method in sorted({key[0] for key in rates}):
        times = sorted(step for name, step in rates if name == method)
        axes[1, 0].plot(
            times, [np.mean(rates[(method, t)]) for t in times], color=COLORS[method], linestyle="--", label=method + ": observed"
        )
    axes[1, 0].set_ylim(-0.05, 1.05)
    axes[1, 0].legend(fontsize=7)

    updates = [{**row, "error": row["accuracyPid"]["normalizedError"]} for row in rows if "accuracyPid" in row]
    lines(axes[1, 1], updates, "step", "error")
    axes[1, 1].axhline(0, color="#64748b", linestyle=":")
    lines(axes[2, 0], rows, "step", "durationSeconds")
    lines(axes[2, 1], rows, "step", "millicoreSeconds")
    axes[2, 0].set_yscale("log")
    axes[2, 1].set_yscale("log")
    phases = {row["step"]: row["phase"] for row in rows if row["phaseStep"] == 0}
    for axis in axes.flat:
        for step in sorted(phases)[1:]:
            axis.axvline(step, color="#94a3b8", alpha=0.4, linestyle=":")
        axis.grid(alpha=0.18)
    note = "; ".join(f"{step}: {name}" for step, name in sorted(phases.items()))
    return save(
        figure,
        output,
        "pid-accuracy",
        study="cheeger-strategies",
        note=f"Phase starts: {note}. Goals are soft; fresh spectral bounds can remain too wide.",
    )
