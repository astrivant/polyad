"""
Compare reduced-state disagreements, grid growth and measured analysis costs.
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
    Relate state reduction to analytic disagreement and recorded resource consumption.

    Args:
        result (dict[str, Any]): Recorded representation and resolution comparisons.
        output (Path): Destination for figures.

    Returns:
        list[str]: State tradeoff and analysis cost PNG/SVG filenames.
    """
    records = result["records"]
    labels = [f"{record['representation']}\n{record['resolution']} points/axis" for record in records]
    positions = list(range(len(records)))
    figure, axes = plt.subplots(1, 2, figsize=(15, 6), layout="constrained")
    optimistic = [record["optimisticCount"] for record in records]
    axes[0].barh(labels, optimistic, color="#b96657", label="Accepts reference-rejected state")
    axes[0].barh(
        labels,
        [record["conservativeCount"] for record in records],
        left=optimistic,
        color="#d9ad51",
        label="Rejects reference-accepted state",
    )
    axes[0].set(xlabel="Probe disagreements with full analytic model")
    describe_axis(
        axes[0],
        "Information lost by state reduction",
        "Bars count optimistic and conservative disagreements with the full state model.",
    )
    axes[0].legend(fontsize=8, loc="lower right")
    bars = axes[1].barh(labels, [record["resources"]["gridPoints"] for record in records], color="#335c81")
    axes[1].bar_label(bars, labels=[str(record["resources"]["gridPoints"]) for record in records], padding=3)
    axes[1].set(xscale="log", xlabel="Cartesian grid points (log scale)")
    describe_axis(
        axes[1],
        "State dimensions multiply the search space",
        "Grid points grow multiplicatively as state dimensions and resolution increase.",
    )
    axes[1].margins(x=0.2)
    paths = save(figure, output, "state-tradeoffs", study="reachability-state")

    figure, axes = plt.subplots(1, 3, figsize=(19, 6), layout="constrained")
    axes[0].barh(labels, [record["guard"]["meanSeconds"] * 1e6 for record in records], color="#38876e")
    axes[0].set(xlabel="Mean measured microseconds per call")
    describe_axis(
        axes[0],
        "Service-side guard cost",
        "This is the measured cost of one application-side admission decision.",
    )
    axes[1].barh(
        [x - 0.18 for x in positions],
        [record["resources"]["estimatedWorkspaceBytes"] for record in records],
        height=0.35,
        label="Estimated solver workspace",
        color="#72578b",
    )

    # Keep omitted numerical runs visibly absent, rather than treating unmeasured time or memory as zero.
    numerical = [(index, record["numerical"]) for index, record in enumerate(records) if "numerical" in record]
    if numerical:
        axes[1].barh(
            [index + 0.18 for index, _ in numerical],
            [data["peakRssBytes"] for _, data in numerical],
            height=0.35,
            label="Measured peak process RSS",
            color="#b96657",
        )
        axes[2].barh(
            [index - 0.18 for index, _ in numerical],
            [data["wallSeconds"] for _, data in numerical],
            height=0.35,
            label="Whole analysis",
            color="#335c81",
        )
        axes[2].barh(
            [index + 0.18 for index, _ in numerical],
            [data["solveSeconds"] for _, data in numerical],
            height=0.35,
            label="Solve stage",
            color="#38876e",
        )
        axes[2].legend(fontsize=8)
    else:
        axes[2].text(0.5, 0.5, "Numerical analysis disabled\nNo solve measurements", transform=axes[2].transAxes, ha="center")
    axes[1].set(xscale="log", xlabel="Bytes (log scale)")
    describe_axis(
        axes[1],
        "Estimated versus measured memory",
        "Compare predicted solver workspace with measured peak process RSS when available.",
    )
    axes[1].legend(fontsize=8)
    axes[2].set(xlabel="Measured seconds")
    describe_axis(
        axes[2],
        "Optional numerical analysis",
        "Wall time includes setup; solve time isolates the numerical stage.",
    )
    for axis in axes[1:]:
        axis.set(yticks=positions, yticklabels=labels)
    return paths + save(figure, output, "analysis-cost", study="reachability-state", note=f"Run {result['runId']}")
