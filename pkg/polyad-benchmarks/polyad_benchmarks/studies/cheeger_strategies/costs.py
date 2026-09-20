"""
Compare CPU work per calculation with average occupied cores during the same work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import matplotlib.pyplot as plt

from polyad_benchmarks.studies.cheeger_strategies.plotting import lines
from polyad_benchmarks.studies.plotting import describe_axis, save

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

__all__ = ("cpu_cost",)


def cpu_cost(result: dict[str, Any], output: Path) -> list[str]:
    """
    Plot measured process CPU work and core occupancy, retaining missing old measurements.

    Args:
        result (dict[str, Any]): Individual wall-clock and process CPU measurements.
        output (Path): Destination for companion CPU figures.

    Returns:
        list[str]: Feedback CPU and controlled-sweep CPU comparison artifacts.
    """
    paths = []
    rows = [row for row in result["records"] if row.get("cpuSeconds") is not None]
    feedback = [row for row in rows if row["sweep"] == "pid-feedback"]
    figure, axes = plt.subplots(2, 2, figsize=(17, 12), layout="constrained")

    # Compare paired calculations, never a method's median against an unrelated
    # fresh-spectral sample. A missing or zero reference is not a zero-cost method.
    references = {(row["seed"], row["repeat"], row["step"]): row["cpuSeconds"] for row in feedback if row["strategy"] == "Fresh spectral"}
    paired = [
        {**row, "relativeCpu": row["cpuSeconds"] / references[(row["seed"], row["repeat"], row["step"])]}
        for row in feedback
        if references.get((row["seed"], row["repeat"], row["step"]), 0) > 0
    ]
    panels = (
        (feedback, "durationSeconds", "Time until the answer", r"Wall time $W$ includes computation and any descheduling.", "Seconds"),
        (
            feedback,
            "averageMillicores",
            "Average CPU occupied while computing",
            r"Measured $1000 C/W$; 1000m is one fully occupied core.",
            "Average millicores",
        ),
        (
            feedback,
            "millicoreSeconds",
            "Total CPU work per calculation",
            r"Measured $1000 C$; equal core occupancy can hide very different work.",
            "Millicore-seconds / calculation",
        ),
        (
            paired,
            "relativeCpu",
            "CPU expense relative to fresh spectral",
            r"Paired $C_{method}/C_{fresh}$ at the same seed, repeat and observation.",
            "CPU cost / fresh spectral",
        ),
    )
    for axis, (selected, field, title, description, ylabel) in zip(axes.flat, panels, strict=True):
        lines(axis, selected, "step", field)
        describe_axis(axis, title, description)
        axis.set(xlabel=r"Graph observation $t$", ylabel=ylabel)
        if selected and field in {"durationSeconds", "millicoreSeconds", "relativeCpu"}:
            axis.set_yscale("log")
        if not selected:
            axis.text(0.5, 0.5, "CPU measurements not collected", ha="center", transform=axis.transAxes)
        for step in sorted({row["step"] for row in feedback if row["phaseStep"] == 0})[1:]:
            axis.axvline(step, color="#94a3b8", alpha=0.4, linestyle=":")
    axes[1, 1].axhline(1, color="#64748b", linestyle="--")
    paths.extend(
        save(
            figure,
            output,
            "pid-cost",
            study="cheeger-strategies",
            note="C is process CPU time across its threads, not Kubernetes requests or limits. Oracle and priming are excluded.",
        )
    )

    figure, axes = plt.subplots(2, 3, figsize=(21, 12), layout="constrained")
    config = result["recipe"]
    slices = (
        ([row for row in rows if row["sweep"] == "churn"], "replacement", "Connection churn", r"Replacement fraction $r$"),
        ([row for row in rows if row["sweep"] == "size"], "vertices", "Graph size", r"Vertices $n$"),
        (
            [
                row
                for row in rows
                if row["sweep"] == "reduction"
                and row["components"] == config["fixedComponents"]
                and row["topology"] == config["fixedTopology"]
            ],
            "supernodes",
            "Quotient size",
            r"Supernodes $k$",
        ),
    )
    for column, (selected, x, title, xlabel) in enumerate(slices):
        for row_index, field in enumerate(("millicoreSeconds", "averageMillicores")):
            axis = axes[row_index, column]
            lines(axis, selected, x, field)
            axis.set(xlabel=xlabel, ylabel="Millicore-seconds / calculation" if row_index == 0 else "Average millicores")
            describe_axis(
                axis,
                title + (": CPU work" if row_index == 0 else ": core occupancy"),
                r"Integrated work $1000 C$; baseline priming and controller warmup excluded."
                if row_index == 0
                else r"Average occupied cores $1000 C/W$; not CPU cost per unit of work.",
            )
            if selected and row_index == 0:
                axis.set_yscale("log")
            if not selected:
                axis.text(0.5, 0.5, "CPU measurements not collected", ha="center", transform=axis.transAxes)
    paths.extend(
        save(
            figure,
            output,
            "cpu-cost",
            study="cheeger-strategies",
            note="Process CPU includes native threads. Submillisecond ratios are noisy; bands show the middle 50%, not confidence bounds.",
        )
    )
    return paths
