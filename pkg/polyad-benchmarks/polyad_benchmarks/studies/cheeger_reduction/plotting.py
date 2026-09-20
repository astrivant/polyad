"""
Plot PCA quotient accuracy, cost, certificates and reuse under graph churn.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import TYPE_CHECKING

import matplotlib

__all__ = (
    "accuracy",
    "cost",
    "render",
    "stability",
)


# File-based rendering also works on headless CI runners without a GUI backend.
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from polyad_benchmarks.studies.plotting import describe_axis, save

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any


def _series(
    records: list[dict[str, Any]],
    group: str,
    x: str,
    y: str,
    select: Callable[[dict[str, Any]], bool],
) -> dict[str, tuple[list[float], list[float]]]:
    """
    Aggregate repeated records into median lines.

    Args:
        records (list[dict[str, Any]]): Study measurements.
        group (str): Categorical field defining separate lines.
        x (str): Numeric horizontal field.
        y (str): Numeric vertical field.
        select (Callable[[dict[str, Any]], bool]): Record inclusion predicate.

    Returns:
        dict[str, tuple[list[float], list[float]]]: Sorted x values and median y values.
    """

    # Filter first, then group samples by series and x coordinate. This prevents
    # settings meant to be held constant from being averaged into the same line.
    values: dict[str, dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        if select(record):
            values[str(record[group])][float(record[x])].append(float(record[y]))

    result = {}
    for name, points in values.items():
        ordered = sorted(points)
        result[name] = (ordered, [statistics.median(points[value]) for value in ordered])
    return result


def _lines(axis: Any, series: dict[str, tuple[list[float], list[float]]], *, marker: str = "o") -> None:
    """
    Draw consistently labelled median series.

    Args:
        axis (Any): Matplotlib subplot.
        series (dict[str, tuple[list[float], list[float]]]): Grouped x and y values.
        marker (str): Marker passed to Matplotlib.

    Returns:
        None: Lines and a legend are added to the subplot.
    """
    for name, (x_values, y_values) in sorted(series.items()):
        axis.plot(x_values, y_values, marker=marker, label=name)
    axis.legend(fontsize=8)
    axis.grid(alpha=0.2)


def accuracy(result: dict[str, Any], output: Path) -> list[str]:
    """
    Isolate PCA dimensions, quotient size, topology and retained variance.

    Args:
        result (dict[str, Any]): Completed reduction study.
        output (Path): Figure destination.

    Returns:
        list[str]: Accuracy figure artifacts.
    """
    records = result["records"]
    config = result["recipe"]
    fixed_components = config["fixedComponents"]
    fixed_supernodes = config["fixedSupernodes"]
    figure, axes = plt.subplots(2, 2, figsize=(15, 10), layout="constrained")

    # Vary retained dimensions with quotient size fixed. This isolates embedding
    # quality from the number of cluster unions the reduced search can examine.
    _lines(
        axes[0, 0],
        _series(
            records,
            "topology",
            "components",
            "relativeError",
            lambda row: row["sweep"] == "reduction" and row["supernodes"] == fixed_supernodes,
        ),
    )
    axes[0, 0].set(xlabel=r"Retained PCA dimensions, $d$", ylabel=r"Median relative error, $(\hat{h}_Q-h)/h$")
    describe_axis(
        axes[0, 0],
        "PCA dimensions versus Cheeger error",
        rf"The quotient size $k={fixed_supernodes}$ is fixed; only retained PCA dimension $d$ changes.",
    )

    # Reverse the comparison: vary quotient size with the PCA dimension fixed.
    _lines(
        axes[0, 1],
        _series(
            records,
            "topology",
            "supernodes",
            "relativeError",
            lambda row: row["sweep"] == "reduction" and row["components"] == fixed_components,
        ),
    )
    axes[0, 1].set(xlabel=r"Quotient supernodes, $k$", ylabel=r"Median relative error, $(\hat{h}_Q-h)/h$")
    describe_axis(
        axes[0, 1],
        "Compression versus Cheeger error",
        rf"For fixed $d={fixed_components}$, compression maps $n\rightarrow k$; larger $k$ admits more lifted cuts.",
    )

    # Keep both settings fixed for topology comparisons. Boxplots retain the
    # spread across seeds that a single median line would conceal.
    selected = [
        row
        for row in records
        if row["sweep"] == "reduction" and row["components"] == fixed_components and row["supernodes"] == fixed_supernodes
    ]
    topologies = config["topologies"]
    axes[1, 0].boxplot(
        [[row["relativeError"] for row in selected if row["topology"] == name] for name in topologies],
        tick_labels=topologies,
    )
    axes[1, 0].set(ylabel=r"Relative error, $(\hat{h}_Q-h)/h$")
    axes[1, 0].grid(axis="y", alpha=0.2)
    describe_axis(
        axes[1, 0],
        "Topology sensitivity at fixed settings",
        rf"Every family shares $(d,k)=({fixed_components},{fixed_supernodes})$: the same embedding and supernode budgets.",
    )

    # Variance explains the adjacency representation, not necessarily sparse cuts.
    # Show its relationship with actual cut error rather than treating it as proof.
    for topology in topologies:
        rows = [row for row in selected if row["topology"] == topology]
        axes[1, 1].scatter(
            [row["retainedVariance"] for row in rows],
            [row["relativeError"] for row in rows],
            label=topology,
            alpha=0.8,
        )
    axes[1, 1].set(
        xlabel=r"Retained variance, $\sum_{i=1}^{d}\sigma_i^2/\sum_{i=1}^{n}\sigma_i^2$",
        ylabel=r"Relative error, $(\hat{h}_Q-h)/h$",
    )
    axes[1, 1].legend(fontsize=8)
    axes[1, 1].grid(alpha=0.2)
    describe_axis(
        axes[1, 1],
        "Variance retained is not an accuracy guarantee",
        r"Retained variance $R_d^2$ can be large while clustering still merges a minimum cut.",
    )
    return save(
        figure,
        output,
        "accuracy",
        study="cheeger-reduction",
        note=r"Relative error is $(\hat{h}_Q(G)-h(G))/h(G)$; lower is better.",
    )


def cost(result: dict[str, Any], output: Path) -> list[str]:
    """
    Compare exact work with reduction cost and certified interval width.

    Args:
        result (dict[str, Any]): Completed reduction study.
        output (Path): Figure destination.

    Returns:
        list[str]: Cost figure artifacts.
    """
    records = result["records"]
    config = result["recipe"]
    size = [row for row in records if row["sweep"] == "size"]
    figure, axes = plt.subplots(1, 3, figsize=(18, 5), layout="constrained")

    # Compare end-to-end method times on the same size sweep. Reduced time
    # includes PCA, clustering, quotient enumeration and the spectral lower bound.
    for key, label, color in (
        ("exactDurationSeconds", r"exact $h(G)$ enumeration", "#a44960"),
        ("durationSeconds", r"PCA + $\hat{h}_Q(G)$ + $\lambda_2/2$", "#335c81"),
    ):
        points = defaultdict(list)
        for row in size:
            points[row["vertices"]].append(row[key])
        x_values = sorted(points)
        axes[0].plot(x_values, [statistics.median(points[value]) for value in x_values], marker="o", label=label, color=color)
    axes[0].set(xlabel=r"Original vertices, $n=|V|$", ylabel=r"Median measured time, $t$ (seconds)", yscale="log")
    axes[0].legend(fontsize=8)
    axes[0].grid(alpha=0.2)
    describe_axis(
        axes[0],
        "End-to-end cost as the graph grows",
        r"Measured $t(n)$ includes PCA, clustering, $\hat{h}_Q(G)$ and the $\lambda_2/2$ lower bound.",
    )

    # With graph size fixed, every increase in k doubles the candidate count
    # approximately; the horizontal exact-search reference remains unchanged.
    reduction = [
        row
        for row in records
        if row["sweep"] == "reduction" and row["components"] == config["fixedComponents"] and row["topology"] == config["fixedTopology"]
    ]
    cuts = defaultdict(list)
    for row in reduction:
        cuts[row["supernodes"]].append(row["evaluatedCuts"])
    x_values = sorted(cuts)
    axes[1].plot(
        x_values,
        [statistics.median(cuts[value]) for value in x_values],
        marker="o",
        label=r"quotient $2^{k-1}-1$",
    )
    axes[1].axhline(reduction[0]["exactCuts"], color="#a44960", linestyle="--", label=r"exact $2^{n-1}-1$")
    axes[1].set(xlabel=r"Quotient supernodes, $k$", ylabel=r"Distinct cuts, $N_{cuts}$", yscale="log")
    axes[1].legend(fontsize=8)
    axes[1].grid(alpha=0.2)
    describe_axis(
        axes[1],
        "Search-space reduction",
        r"Clustering searches $2^{k-1}-1$ supernode unions instead of $2^{n-1}-1$ vertex cuts.",
    )

    # Interval width is observable without ground truth; actual error needs the
    # exact reference. Plot both so a conservative certificate is not mistaken
    # for the error we happened to observe in this experiment.
    intervals = defaultdict(list)
    errors = defaultdict(list)
    for row in reduction:
        intervals[row["supernodes"]].append(row["intervalWidth"])
        errors[row["supernodes"]].append(row["absoluteError"])
    axes[2].plot(
        x_values,
        [statistics.median(intervals[value]) for value in x_values],
        marker="o",
        label=r"certificate $\hat{h}_Q-\lambda_2/2$",
    )
    axes[2].plot(
        x_values,
        [statistics.median(errors[value]) for value in x_values],
        marker="o",
        label=r"observed $\hat{h}_Q-h$",
    )
    axes[2].set(xlabel=r"Quotient supernodes, $k$", ylabel=r"Expansion difference, $\Delta h$")
    axes[2].legend(fontsize=8)
    axes[2].grid(alpha=0.2)
    describe_axis(
        axes[2],
        "Certificate width versus observed error",
        r"The interval $[\lambda_2/2,\hat{h}_Q]$ can be wider than measured error $\hat{h}_Q-h$.",
    )
    return save(figure, output, "cost", study="cheeger-reduction")


def stability(result: dict[str, Any], output: Path) -> list[str]:
    """
    Show how cached and freshly reduced cuts behave as a stable graph changes.

    Args:
        result (dict[str, Any]): Completed reduction study.
        output (Path): Figure destination.

    Returns:
        list[str]: Stability figure artifacts.
    """
    records = [row for row in result["records"] if row["sweep"] == "stability"]
    figure, axes = plt.subplots(1, 3, figsize=(20, 5.5), layout="constrained")

    # Both methods see the same changed graph. The cached method retains its old
    # groups but still measures the cut against current edges.
    for key, label, color in (
        ("cachedRelativeError", "reuse cached partition", "#d47a49"),
        ("relativeError", "refresh PCA partition", "#248266"),
    ):
        points = defaultdict(list)
        for row in records:
            points[row["edgeChurn"]].append(row[key])
        x_values = sorted(points)
        axes[0].plot(x_values, [statistics.median(points[value]) for value in x_values], marker="o", label=label, color=color)
    axes[0].set(xlabel=r"Edge replacement fraction, $\rho_E$", ylabel=r"Median relative error, $(\hat{h}_Q-h)/h$")
    axes[0].legend(fontsize=8)
    axes[0].grid(alpha=0.2)
    describe_axis(
        axes[0],
        "Cached-cut accuracy under graph churn",
        r"At edge churn $\rho_E=0$ the graph is steady; later points preserve $V$ while replacing edges.",
    )

    # Cached timings omit building the baseline partition; fresh timings include
    # that work. Keep exact enumeration as a separately measured reference.
    for key, label, color in (
        ("cachedDurationSeconds", "cached quotient search", "#d47a49"),
        ("durationSeconds", "fresh reduction", "#248266"),
        ("exactDurationSeconds", "exact enumeration", "#a44960"),
    ):
        points = defaultdict(list)
        for row in records:
            points[row["edgeChurn"]].append(row[key])
        x_values = sorted(points)
        axes[1].plot(x_values, [statistics.median(points[value]) for value in x_values], marker="o", label=label, color=color)
    axes[1].set(xlabel=r"Edge replacement fraction, $\rho_E$", ylabel=r"Median measured time, $t$ (seconds)", yscale="log")
    axes[1].legend(fontsize=8)
    axes[1].grid(alpha=0.2)
    describe_axis(
        axes[1],
        "Cost of reuse, refresh and exact search",
        r"Runtime $t(\rho_E)$ for cached reuse skips PCA but reevaluates lifted cuts on current edges $E_t$.",
    )

    # Draw each fresh certificate separately, with exact h marked inside it.
    # Seed-level intervals remain visible instead of being averaged into one band.
    for index, row in enumerate(records):
        axes[2].plot(
            [row["lowerBound"], row["upperBound"]],
            [row["edgeChurn"], row["edgeChurn"]],
            color="#8aa0b8",
            alpha=0.35,
            label=r"$[\lambda_2/2,\hat{h}_Q]$" if index == 0 else None,
        )
        axes[2].scatter(
            row["exact"],
            row["edgeChurn"],
            color="#a44960",
            s=18,
            label=r"exact $h(G)$" if index == 0 else None,
        )
    axes[2].set(xlabel=r"Cheeger expansion, $h$", ylabel=r"Edge replacement fraction, $\rho_E$")
    axes[2].legend(fontsize=8)
    axes[2].grid(alpha=0.2)
    describe_axis(
        axes[2],
        "Certified intervals contain the exact value",
        r"The guarantee is $\lambda_2/2\leq h(G)\leq\hat{h}_Q(G)$; red marks exact $h(G)$.",
    )
    return save(
        figure,
        output,
        "stability",
        study="cheeger-reduction",
        note="Cached results remain witnessed upper bounds, not exact constants or permission to pass a hard minimum.",
    )


def render(result: dict[str, Any], output: Path) -> list[str]:
    """
    Produce every PCA Cheeger reduction figure from recorded measurements.

    Args:
        result (dict[str, Any]): Completed local study result.
        output (Path): Figure destination.

    Returns:
        list[str]: Six PNG/SVG artifacts.
    """

    # Rebuild the figures from recorded measurements without changing their data.
    return accuracy(result, output) + cost(result, output) + stability(result, output)
