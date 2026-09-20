"""
Compare the modeled effects of service relationships and measured runtime guard costs.
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
    Plot signed capacity effects, analytic admission margins and measured guard overhead.

    Args:
        result (dict[str, Any]): Recorded interaction comparisons, with optional numerical diagnostics.
        output (Path): Figure destination.

    Returns:
        list[str]: Interaction and guard-cost PNG/SVG artifacts.
    """
    records = result["records"]
    labels = [record["relationship"] for record in records]
    figure, axes = plt.subplots(1, 2, figsize=(14, 5), layout="constrained")
    for index, name in enumerate(records[0]["model"]["names"]):
        axes[0].barh(
            [y + (index - 0.5) * 0.35 for y in range(len(records))],
            [record["effects"][index] for record in records],
            height=0.35,
            label=name,
        )
    axes[0].set(
        yticks=range(len(records)),
        yticklabels=labels,
        xlabel=f"Declared capacity effect ({records[0]['model']['unit']}/s)",
    )
    describe_axis(
        axes[0],
        "Each participant's modeled benefit or cost",
        "Signed effects show capacity gained or consumed by each relationship.",
    )
    axes[0].legend(fontsize=8)
    bars = axes[1].barh(
        labels, [record["margin"] for record in records], color=["#38876e" if record["allowed"] else "#b96657" for record in records]
    )
    axes[1].bar_label(bars, labels=[f"{record['margin']:g} / {'allow' if record['allowed'] else 'block'}" for record in records], padding=4)
    axes[1].set(xlabel=f"Analytic margin ({records[0]['model']['unit']})")
    describe_axis(
        axes[1],
        "Does the whole queue contract hold?",
        "Positive analytic margin admits the relationship; negative margin blocks it.",
    )
    axes[1].margins(x=0.35)

    # A shared zero reference makes signed capacity effects and admission margins directly readable.
    for axis in axes:
        axis.axvline(0, color="#8995a5", linewidth=1)
    paths = save(figure, output, "interactions", study="symbiosis")

    figure, axes = plt.subplots(1, 2, figsize=(13, 5), layout="constrained")
    axes[0].barh(labels, [record["guard"]["meanSeconds"] * 1e6 for record in records], color="#335c81")
    axes[0].set(xlabel="Mean measured microseconds per assessment")
    describe_axis(
        axes[0],
        "Runtime guard evaluation",
        "Measured callback cost shows the service-side price of each assessment.",
    )
    axes[1].barh(labels, [record["guard"]["artifactBytes"] for record in records], color="#72578b")
    axes[1].set(xlabel="Serialized bytes")
    describe_axis(
        axes[1],
        "Portable envelope size",
        "Serialized bytes show the policy artifact carried between environments.",
    )
    return paths + save(figure, output, "guard-cost", study="symbiosis", note=f"Run {result['runId']}")
