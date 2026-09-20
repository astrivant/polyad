"""
Plot each rerouting repetition separately, keeping rejections and queue ceilings visible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import matplotlib

__all__ = ("render",)


matplotlib.use("Agg")
import matplotlib.pyplot as plt

from polyad_benchmarks.studies.plotting import describe_axis, save

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


def render(result: dict[str, Any], output: Path) -> list[str]:
    """
    Compare actual consumer assignments, queue peaks and outcomes for paired trials.

    Args:
        result (dict[str, Any]): Recorded real-process trials in measured execution order.
        output (Path): Figure directory.

    Returns:
        list[str]: Routing and outcome PNG/SVG artifacts.
    """
    records = result["records"]
    modes = {"first-consumer": "First consumer", "guarded-rerouting": "Guarded split"}

    # Retain each repetition instead of averaging away individual queue-limit breaches.
    labels = [f"{modes[record['mode']]}\nrepeat {record['repetition'] + 1}" for record in records]
    figure, axes = plt.subplots(1, 2, figsize=(14, 5), layout="constrained")
    for index, name in enumerate(records[0]["artifact"]["model"]["names"]):
        positions = [x + (index - 0.5) * 0.35 for x in range(len(records))]
        for axis, key in zip(axes, ("routed", "peakOutstanding"), strict=True):
            bars = axis.bar(positions, [record[key][index] for record in records], width=0.35, label=name)
            axis.bar_label(bars, padding=3)
    for limit in sorted({limit for record in records for limit in record["artifact"]["model"]["limits"]}):
        axes[1].axhline(limit, linestyle="--", color="#b96657", label=f"Queue limit: {limit:g}")
    for axis in axes:
        axis.set(xticks=range(len(records)), xticklabels=labels)
        axis.legend(fontsize=8)
        axis.margins(y=0.25)
    axes[0].set(ylabel="Jobs assigned")
    describe_axis(
        axes[0],
        "Where accepted work actually ran",
        "Bars identify the consumer that executed each trial's accepted jobs.",
    )
    axes[1].set(ylabel="Outstanding jobs")
    describe_axis(
        axes[1],
        "Measured peaks and modeled queue limits",
        "Measured outstanding work is compared with every configured queue ceiling.",
    )
    paths = save(figure, output, "routing", study="reachability-routing")

    figure, axes = plt.subplots(1, 3, figsize=(18, 5), layout="constrained")
    completed = [record["completed"] for record in records]
    axes[0].bar(labels, completed, label="Completed", color="#38876e")
    axes[0].bar(labels, [record["rejected"] for record in records], bottom=completed, label="Rejected", color="#b96657")
    axes[0].set(ylabel="Offered jobs")
    describe_axis(
        axes[0],
        "Completion and rejection remain visible",
        "Stacked bars keep rejected jobs visible beside verified completions.",
    )
    axes[0].legend(fontsize=8)
    for axis, key, title in zip(
        axes[1:],
        ("meanLatencySeconds", "elapsedSeconds"),
        ("Mean latency of completed jobs", "Elapsed producer and drain time"),
        strict=True,
    ):
        samples = [(index, record[key]) for index, record in enumerate(records) if record[key] is not None]
        bars = axis.bar([index for index, _ in samples], [value for _, value in samples], color="#335c81")
        axis.bar_label(bars, fmt="%.3f", padding=3)
        axis.set(xticks=range(len(records)), xticklabels=labels, ylabel="Measured seconds")
        description = (
            "Latency includes only jobs that completed successfully."
            if key == "meanLatencySeconds"
            else "Elapsed time covers production, processing and final drain."
        )
        describe_axis(axis, title, description)
        axis.margins(y=0.2)
    return paths + save(figure, output, "outcomes", study="reachability-routing", note=f"Run {result['runId']}")
