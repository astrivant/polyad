"""
Plot cloud request outcomes and observed API acceptance and completion latencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from polyad_benchmarks.studies.plotting import describe_axis, save

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


def render(result: dict[str, Any], output: Path) -> list[str]:
    """
    Show skipped arrivals and failures alongside successful work and measured latency.

    Args:
        result (dict[str, Any]): Runner JSON from an actual cluster load experiment.
        output (Path): Destination for exported figures.

    Returns:
        list[str]: Outcome and latency figures in PNG and SVG form.
    """
    phases = dict(result["phases"])
    phases["Skipped"] = result["skipped"]
    figure, axis = plt.subplots(figsize=(10, 5), layout="constrained")
    bars = axis.bar(list(phases), list(phases.values()), color=["#38876e" if key == "Completed" else "#b96657" for key in phases])
    axis.bar_label(bars, padding=3)
    axis.set(ylabel="Requests")
    describe_axis(
        axis,
        "Terminal request accounting",
        "Each bar is one terminal phase; skipped arrivals remain explicit.",
    )
    axis.margins(y=0.2)
    paths = save(figure, output, "outcomes", study="load", note=f"Run {result['runId']} | interrupted: {result['interrupted']}")

    requests = result.get("requests", [])
    figure, axes = plt.subplots(1, 2, figsize=(13, 5), layout="constrained")
    for key, label, color in (("acceptanceSeconds", "API acceptance", "#335c81"), ("elapsedSeconds", "Successful completion", "#38876e")):
        samples = [
            (index + 1, request[key])
            for index, request in enumerate(requests)
            if key in request and (key != "elapsedSeconds" or request["phase"] == "Completed")
        ]
        if not samples:
            continue
        axes[0].scatter([item[0] for item in samples], [item[1] for item in samples], label=label, color=color, s=18)

        # The empirical CDF counts measured samples; it does not invent latency for skipped arrivals.
        ordered = sorted(item[1] for item in samples)
        axes[1].step(
            [ordered[0], *ordered],
            [0, *[(i + 1) / len(ordered) for i in range(len(ordered))]],
            where="post",
            label=label,
            color=color,
        )
    for axis in axes:
        axis.grid(alpha=0.2)
        handles, _ = axis.get_legend_handles_labels()
        if handles:
            axis.legend()
        else:
            axis.text(0.5, 0.5, "No measured latency samples", transform=axis.transAxes, ha="center")
    axes[0].set(xlabel="Request order (not wall time)", ylabel="Seconds")
    describe_axis(
        axes[0],
        "Observed request latencies",
        "Each point is an observed API or successful end-to-end duration.",
    )
    axes[1].set(xlabel="Seconds", ylabel="Fraction of observed samples", ylim=(0, 1.05))
    describe_axis(
        axes[1],
        "Empirical latency distributions",
        "Step curves show the fraction of measured samples completed by each duration.",
    )
    return paths + save(figure, output, "latencies", study="load", note=f"Run {result['runId']}")
