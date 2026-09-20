"""
Plot actual Cheeger tier activation, oracle accuracy and controlled parameter sweeps.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

import matplotlib

# Render files without a desktop or display server, including on CI workers.
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Patch

from polyad_benchmarks.studies.plotting import describe_axis, save

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

# Method colors compare whole algorithms; stage colors show the selector's
# internal path. Keep both mappings stable across every panel in the study.
COLORS = {"Exact": "#334155", "PCA": "#ad5389", "Fresh spectral": "#248266", "Cached spectral": "#dd862b", "Selector": "#377cbb"}
STAGES = {"CachedQuotient": 0, "FreshSpectralReduction": 1, "ExactEnumeration": 2}
STAGE_NAMES = ("Cached quotient", "Fresh spectral", "Exact search")
STAGE_COLORS = ("#f1b458", "#4fb19a", "#588fcb")


def lines(axis: Any, rows: list[dict[str, Any]], x: str, y: str, *, group: str = "strategy") -> None:
    """
    Plot medians and interquartile bands without dropping valid zero values.

    Args:
        axis (Any): Destination subplot.
        rows (list[dict[str, Any]]): Explicitly filtered measurements.
        x (str): Horizontal variable.
        y (str): Vertical variable; null values remain missing.
        group (str): Categorical series key.

    Returns:
        None: Draw lines, uncertainty bands and a compact legend.
    """

    # Group only present observations. A genuine zero error belongs in the plot;
    # a missing measurement must not be converted into a fabricated zero.
    values: dict[str, dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row.get(y) is not None:
            values[str(row[group])][float(row[x])].append(float(row[y]))

    # The middle line is the median and the shaded band is the middle 50% of
    # samples. This describes observed spread, not a statistical confidence bound.
    for label, points in sorted(values.items()):
        xs = sorted(points)
        quantiles = np.array([np.quantile(points[value], [0.25, 0.5, 0.75]) for value in xs])
        line = axis.plot(xs, quantiles[:, 1], marker="o", markersize=4, label=label, color=COLORS.get(label))[0]
        axis.fill_between(xs, quantiles[:, 0], quantiles[:, 2], color=line.get_color(), alpha=0.12)

    axis.grid(alpha=0.18)
    axis.legend(fontsize=8)


def churn(result: dict[str, Any], output: Path) -> list[str]:
    """
    Compare witnesses and full selector cost at identical requested rewiring levels.

    Args:
        result (dict[str, Any]): Measured study.
        output (Path): Artifact directory.

    Returns:
        list[str]: PNG and SVG paths.
    """
    rows = [row for row in result["records"] if row["sweep"] == "churn"]
    figure, axes = plt.subplots(2, 2, figsize=(16, 11), layout="constrained")

    # Reuse the same paired samples for error, total runtime and interval width.
    # Only time uses a logarithmic axis; zero error and zero width remain visible.
    for axis, field, title, description, ylabel in (
        (
            axes[0, 0],
            "relativeError",
            "Cut error as connections change",
            r"Each upper witness is compared with exact $h(G)$ on the same snapshot.",
            r"Relative error $(U-h)/h$",
        ),
        (
            axes[0, 1],
            "durationSeconds",
            "Measured time for the whole method",
            r"Selector time includes every tier attempted; cache priming is excluded.",
            "Time per calculation (seconds)",
        ),
        (
            axes[1, 0],
            "intervalWidth",
            "Remaining uncertainty about expansion",
            r"A narrow $[L,U]$ can answer policy; exact enumeration has zero width.",
            r"Certificate width $U-L$",
        ),
    ):
        lines(axis, rows, "replacement", field)
        axis.set(xlabel=r"Requested edge replacement fraction $r$", ylabel=ylabel)
        if field == "durationSeconds":
            axis.set_yscale("log")
        describe_axis(axis, title, description)

    # Topology change is shared by all methods and timing repeats. Select one
    # copy per graph so the achieved-churn panel does not count it repeatedly.
    unique = [row for row in rows if row["strategy"] == "Exact" and row["repeat"] == 0]
    lines(axes[1, 1], unique, "replacement", "edgeChurn", group="topology")
    axes[1, 1].set(xlabel=r"Requested edge replacement fraction $r$", ylabel=r"Achieved churn $|E_0\triangle E|/|E_0\cup E|$")
    describe_axis(
        axes[1, 1], "Requested versus achieved churn", "Connectivity and finite edge counts limit which replacements are possible."
    )
    return save(
        figure,
        output,
        "churn",
        study="cheeger-strategies",
        note="Lines: medians. Bands: middle 50% of samples across families, seeds and timing repeats; not confidence bounds.",
    )


def activation(result: dict[str, Any], output: Path) -> list[str]:
    """
    Map measured finishing tiers for each type of policy around its exact reference.

    Args:
        result (dict[str, Any]): Measured study.
        output (Path): Artifact directory.

    Returns:
        list[str]: PNG and SVG paths.
    """
    config = result["recipe"]

    # A categorical activation map needs one observed answer per cell. Hold seed
    # and cache gate fixed instead of averaging incompatible finishing stages.
    rows = [
        row
        for row in result["records"]
        if row["sweep"] == "threshold" and row["seed"] == config["seeds"][0] and row["cacheChurnLimit"] == config["fixedCacheChurnLimit"]
    ]
    figure, axes = plt.subplots(1, 3, figsize=(18, 7), layout="constrained")
    cmap = ListedColormap(STAGE_COLORS)
    for axis, kind in zip(axes, config["policyKinds"], strict=True):
        selected = [row for row in rows if row["policyKind"] == kind]

        # Color encodes the finishing tier; the overlaid symbol independently
        # records pass, violation or unknown. Unmeasured cells remain missing.
        grid = np.full((len(config["thresholdRatios"]), len(config["edgeReplacement"])), np.nan)
        for row in selected:
            i = config["thresholdRatios"].index(row["thresholdRatio"])
            j = config["edgeReplacement"].index(row["replacement"])
            grid[i, j] = STAGES[row["stage"]]
            axis.text(
                j,
                i,
                "+" if row["decision"] == "Pass" else "×" if row["decision"] == "Violate" else "?",
                ha="center",
                va="center",
                fontsize=14,
            )

        # Stages are categories, so use discrete color boundaries rather than
        # interpolating them as if an intermediate strategy existed.
        axis.imshow(grid, cmap=cmap, norm=BoundaryNorm([-0.5, 0.5, 1.5, 2.5], 3), origin="lower", aspect="auto")
        axis.set(
            xticks=range(len(config["edgeReplacement"])),
            xticklabels=config["edgeReplacement"],
            yticks=range(len(config["thresholdRatios"])),
            yticklabels=config["thresholdRatios"],
            xlabel="Requested edge replacement",
            ylabel=r"Threshold / exact reference $\theta/h$",
        )
        describe_axis(
            axis,
            {"minimum": "Required minimum", "maximum": "Allowed maximum", "range": "Required interval"}[kind],
            {
                "minimum": r"Policy: $h\geq\theta$. Exact search proves minima left unresolved by $L$.",
                "maximum": r"Policy: $h\leq\theta$. A lifted witness $U\leq\theta$ can settle it early.",
                "range": r"Policy: $0.9\theta\leq h\leq1.1\theta$. Both endpoints must be certified.",
            }[kind],
        )

    figure.legend(
        handles=[Patch(color=color, label=label) for label, color in zip(STAGE_NAMES, STAGE_COLORS, strict=True)],
        loc="lower center",
        ncol=3,
        bbox_to_anchor=(0.5, -0.04),
    )
    return save(
        figure,
        output,
        "activation",
        study="cheeger-strategies",
        note=f"Seed {config['seeds'][0]}; cache gate {config['fixedCacheChurnLimit']}. + passes, × violates. "
        "Oracle-normalized thresholds are experimental inputs only.",
    )


def parameters(result: dict[str, Any], output: Path) -> list[str]:
    """
    Separate spectral dimensions, quotient size, original graph size and density.

    Args:
        result (dict[str, Any]): Measured study.
        output (Path): Artifact directory.

    Returns:
        list[str]: PNG and SVG paths.
    """
    config, rows = result["recipe"], result["records"]
    figure, axes = plt.subplots(2, 3, figsize=(20, 11), layout="constrained")

    # Slice the dimension-by-quotient grid before aggregating. Each panel varies
    # one axis while holding the other settings fixed, avoiding mixed effects.
    fixed = [row for row in rows if row["sweep"] == "reduction" and row["topology"] == config["fixedTopology"]]
    selections = (
        (
            [row for row in fixed if row["supernodes"] == config["fixedSupernodes"]],
            "components",
            "relativeError",
            "Embedding dimensions",
            r"Only $d$ changes; graph family, original size and quotient size stay fixed.",
            r"Dimensions $d$",
            r"Relative error $(U-h)/h$",
        ),
        (
            [row for row in fixed if row["components"] == config["fixedComponents"]],
            "supernodes",
            "relativeError",
            "Quotient size",
            r"Only $k$ changes; more supernodes allow more original-graph cuts.",
            r"Supernodes $k$",
            r"Relative error $(U-h)/h$",
        ),
        (
            [row for row in fixed if row["components"] == config["fixedComponents"]],
            "supernodes",
            "durationSeconds",
            "The price of a larger quotient",
            r"Quotient enumeration grows as $2^{k-1}-1$ even with fixed $d$.",
            r"Supernodes $k$",
            "Time (seconds)",
        ),
        (
            [row for row in rows if row["sweep"] == "size"],
            "vertices",
            "durationSeconds",
            "Original graph size",
            r"The same $(d,k)$ budget is applied while $n$ increases.",
            r"Original vertices $n$",
            "Time (seconds)",
        ),
        (
            [row for row in rows if row["sweep"] == "density"],
            "probability",
            "relativeError",
            "Edge density and cut accuracy",
            "Random graph edge probability varies; connectivity repair is recorded in raw edges.",
            r"Edge probability $p$",
            r"Relative error $(U-h)/h$",
        ),
        (
            [row for row in rows if row["sweep"] == "density"],
            "probability",
            "durationSeconds",
            "Edge density and runtime",
            r"Original size, $d$ and $k$ stay fixed as adjacency becomes denser.",
            r"Edge probability $p$",
            "Time (seconds)",
        ),
    )

    # Panel metadata keeps the plain-language title, mathematical axis and
    # explanatory subtitle together, so the controlled comparison is explicit.
    for axis, (selected, x, y, title, description, xlabel, ylabel) in zip(axes.flat, selections, strict=True):
        lines(axis, selected, x, y)
        axis.set(xlabel=xlabel, ylabel=ylabel)
        if y == "durationSeconds":
            axis.set_yscale("log")
        describe_axis(axis, title, description)
    return save(
        figure,
        output,
        "parameters",
        study="cheeger-strategies",
        note=f"Fixed: n={config['fixedVertices']}, d={config['fixedComponents']}, k={config['fixedSupernodes']}; "
        "community graphs except the density sweep.",
    )


def cache(result: dict[str, Any], output: Path) -> list[str]:
    """
    Show churn-gate effects and cache contention among independent graph boundaries.

    Args:
        result (dict[str, Any]): Measured study.
        output (Path): Artifact directory.

    Returns:
        list[str]: PNG and SVG paths.
    """
    figure, axes = plt.subplots(2, 2, figsize=(16, 11), layout="constrained")
    rows = [row for row in result["records"] if row["sweep"] == "threshold"]
    gates = sorted({row["cacheChurnLimit"] for row in rows})

    # A stacked bar partitions the same policy grid by its finishing tier.
    # This is not the cache-hit rate: a reused partition may still need fallback.
    bottom = np.zeros(len(gates))
    for stage, label, color in zip(STAGES, STAGE_NAMES, STAGE_COLORS, strict=True):
        share = [np.mean([row["stage"] == stage for row in rows if row["cacheChurnLimit"] == gate]) for gate in gates]
        axes[0, 0].bar([str(gate) for gate in gates], share, bottom=bottom, label=label, color=color)
        bottom += share
    axes[0, 0].set(xlabel="Maximum permitted edge churn", ylabel="Fraction of policy evaluations")
    axes[0, 0].legend(fontsize=8)
    describe_axis(axes[0, 0], "Which tier settles the policy", "Each gate sees the same seed, churn, policy type and threshold grid.")

    # Averaging boolean hits yields a fraction. Taking their median would instead
    # jump between zero and one and hide intermediate reuse rates.
    hit = [
        {
            "policyKind": kind,
            "cacheChurnLimit": gate,
            "cacheHitFraction": float(
                np.mean([row["cacheHit"] for row in rows if row["policyKind"] == kind and row["cacheChurnLimit"] == gate])
            ),
        }
        for kind in result["recipe"]["policyKinds"]
        for gate in gates
    ]
    lines(axes[0, 1], hit, "cacheChurnLimit", "cacheHitFraction", group="policyKind")
    axes[0, 1].set(xlabel="Maximum permitted edge churn", ylabel="Fraction with usable cached partition", ylim=(-0.03, 1.03))
    describe_axis(
        axes[0, 1], "Reuse does not always settle policy", "A cache hit may still be followed by fresh reduction and exact enumeration."
    )

    # Omit the initial filling round. Later misses reveal eviction pressure, not
    # merely the fact that every newly seen boundary starts with an empty cache.
    capacity = [
        dict(row, cacheHitFraction=float(row["cacheHit"]))
        for row in result["records"]
        if row["sweep"] == "cache-capacity" and row["step"] >= result["recipe"]["boundaryCount"]
    ]

    # A mean is required for hit fractions; timing lines retain raw medians.
    for axis, field, title, ylabel in (
        (axes[1, 0], "cacheHitFraction", "Cache capacity across boundaries", "Warm-round cache hit fraction"),
        (axes[1, 1], "durationSeconds", "Cost of cache eviction", "Median warm-round time (seconds)"),
    ):
        if field == "cacheHitFraction":
            entries = sorted({row["cacheEntries"] for row in capacity})
            axis.plot(
                entries, [np.mean([row["cacheHitFraction"] for row in capacity if row["cacheEntries"] == n]) for n in entries], marker="o"
            )
        else:
            lines(axis, capacity, "cacheEntries", field)
            axis.set_yscale("log")
        axis.set(xlabel="Cached partition capacity", ylabel=ylabel)
        describe_axis(
            axis,
            title,
            f"Round-robin evaluation of {result['recipe']['boundaryCount']} separately named boundaries; initial fill excluded.",
        )

    return save(figure, output, "cache", study="cheeger-strategies")


def timeline(result: dict[str, Any], output: Path) -> list[str]:
    """
    Plot certificate changes and actual tier invocations through one graph's event sequence.

    Args:
        result (dict[str, Any]): Measured study.
        output (Path): Artifact directory.

    Returns:
        list[str]: PNG and SVG paths.
    """

    # Preserve one seed's event order. Pooling timelines would mix different
    # cache histories and obscure which actual event caused a strategy change.
    rows = [row for row in result["records"] if row["sweep"] == "timeline" and row["seed"] == result["recipe"]["seeds"][0]]
    figure, axes = plt.subplots(3, 1, figsize=(16, 14), layout="constrained")
    steps = [row["step"] for row in rows]
    axes[0].fill_between(
        steps,
        [row["lowerBound"] for row in rows],
        [row["upperBound"] for row in rows],
        alpha=0.2,
        color="#377cbb",
        label=r"Certificate $[L,U]$",
    )
    axes[0].plot(steps, [row["exactValue"] for row in rows], marker="o", color=COLORS["Exact"], label=r"Exact reference $h$")
    for kind, marker in (("minimum", "^"), ("maximum", "v")):
        selected = [row for row in rows if kind in row["policy"]]
        axes[0].scatter([row["step"] for row in selected], [row["policy"][kind] for row in selected], marker=marker, label=kind, s=65)
    axes[0].set(ylabel="Expansion and policy bound")

    # Changed edges can reset the cached lower bound to zero. A symmetric-log
    # scale retains that zero while accommodating much larger policy thresholds.
    axes[0].set_yscale("symlog", linthresh=0.25)
    axes[0].legend(ncol=4, fontsize=8)
    describe_axis(
        axes[0],
        "Certified interval through change",
        "Minimum probes use study truth to force a difficult threshold; wide maximums test reuse.",
    )

    # Plot every tier that returned a certificate, including fresh work that is
    # not a cache hit. The larger outlined marker identifies the finishing tier.
    for row in rows:
        visited = [attempt["stage"] for attempt in row["attempts"] if attempt["certificateReturned"]]
        if row["stage"] == "ExactEnumeration":
            visited.append("ExactEnumeration")
        for stage in visited:
            axes[1].scatter(
                row["step"],
                STAGES[stage],
                s=200 if stage == row["stage"] else 80,
                color=STAGE_COLORS[STAGES[stage]],
                edgecolors="black" if stage == row["stage"] else "none",
            )
    axes[1].set(yticks=range(3), yticklabels=STAGE_NAMES, ylim=(-0.5, 2.5))
    describe_axis(
        axes[1], "Every tier that actually ran", "Large outlined dots finish the decision; smaller dots show earlier work in that call."
    )

    # Use the whole call's elapsed time, not only its last tier's duration.
    axes[2].bar(steps, [row["durationSeconds"] for row in rows], color=[STAGE_COLORS[STAGES[row["stage"]]] for row in rows])
    axes[2].set(ylabel="Measured time (seconds)", yscale="log")
    describe_axis(axes[2], "Cost at each event", "Cache state persists through the sequence, including returning service membership.")

    # All three panels share event positions and labels for vertical comparison.
    for axis in axes:
        axis.set(xticks=steps, xticklabels=[f"{row['step']}: {row['event']}" for row in rows])
        axis.tick_params(axis="x", rotation=30, labelsize=8)
        axis.grid(alpha=0.15)
    return save(
        figure,
        output,
        "timeline",
        study="cheeger-strategies",
        note=f"One reproducible sequence, seed {result['recipe']['seeds'][0]}; raw results retain every seed.",
    )


def controls(result: dict[str, Any], output: Path) -> list[str]:
    """
    Expose budget behavior and decision errors without treating unknown answers as successful.

    Args:
        result (dict[str, Any]): Measured study.
        output (Path): Artifact directory.

    Returns:
        list[str]: PNG and SVG paths.
    """
    figure, axes = plt.subplots(2, 2, figsize=(18, 13), layout="constrained")
    rows = [row for row in result["records"] if row["sweep"] == "controls"]
    cases = list(dict.fromkeys(row["case"] for row in rows))

    # Preserve unresolved outcomes as their own segment. Exhausting a budget
    # is neither a successful policy pass nor proof that the policy was violated.
    left = np.zeros(len(cases))
    for decision, color in (("Pass", "#248266"), ("Violate", "#ad5389"), ("Unknown", "#c8cdd4")):
        fractions = [np.mean([row["decision"] == decision for row in rows if row["case"] == name]) for name in cases]
        axes[0, 0].barh(cases, fractions, left=left, label=decision, color=color)
        left += fractions
    axes[0, 0].legend(fontsize=8)
    axes[0, 0].set(xlabel="Fraction of seed trials")
    describe_axis(
        axes[0, 0], "Which controls reach a decision", r"All probes request the same difficult minimum $\theta=h$; unknown stays explicit."
    )

    # Compare all-tier cut work with the configured exact-search allowance. The
    # identity line makes additional quotient work visible as an overshoot.
    budgets = [row for row in rows if row["case"].startswith("cut budget")]
    lines(axes[0, 1], budgets, "maxCuts", "totalEvaluatedCuts")
    limits = sorted({row["maxCuts"] for row in budgets})
    axes[0, 1].plot(limits, limits, linestyle="--", color="#a44960", label="Configured cut budget")
    axes[0, 1].set(xlabel="Configured cut budget", ylabel="Total cuts across attempted tiers", xscale="log", yscale="log")
    axes[0, 1].legend(fontsize=8)
    describe_axis(
        axes[0, 1],
        "Reduction overhead versus cut budget",
        "This exposes that current quotient work is additional to the exact-search cut allowance.",
    )

    # A cooperative deadline is checked between operations, not by preempting
    # spectral routines. Plot wall-clock time against that requested deadline.
    timed = [row for row in rows if row["case"].startswith("time budget")]
    lines(axes[1, 0], timed, "timeoutSeconds", "durationSeconds")
    limits = sorted({row["timeoutSeconds"] for row in timed})
    axes[1, 0].plot(limits, limits, linestyle="--", color="#a44960", label="Configured deadline")
    axes[1, 0].set(xlabel="Configured time budget (seconds)", ylabel="Measured time (seconds)", xscale="log", yscale="log")
    axes[1, 0].legend(fontsize=8)
    describe_axis(
        axes[1, 0], "Cooperative timeout behavior", "Spectral preprocessing and quotient loops are not preempted by the deadline."
    )

    # Reuse paired churn trials for a fair decision comparison. A method may
    # have cut-value error yet decide correctly, or be accurate but inconclusive.
    comparisons = [row for row in result["records"] if row["sweep"] == "churn"]
    methods = list(COLORS)
    counts = []
    for method in methods:
        selected = [row for row in comparisons if row["strategy"] == method]
        counts.append(
            (
                sum(row["decisionMatchesOracle"] is True for row in selected),
                sum(row["decisionMatchesOracle"] is False for row in selected),
                sum(row["decision"] == "Unknown" for row in selected),
            )
        )

    bottom = np.zeros(len(methods))
    for index, (label, color) in enumerate((("Correct decision", "#248266"), ("Wrong decision", "#a44960"), ("Unknown", "#c8cdd4"))):
        values = [count[index] for count in counts]
        axes[1, 1].bar(methods, values, bottom=bottom, label=label, color=color)
        bottom += values
    axes[1, 1].tick_params(axis="x", rotation=20, labelsize=8)
    axes[1, 1].set(ylabel="Paired churn measurements")
    axes[1, 1].legend(fontsize=8)
    describe_axis(
        axes[1, 1],
        "Decision correctness against exhaustive truth",
        "A witness may have cut-value error yet prove the right policy answer; unknown is not a pass.",
    )
    return save(
        figure,
        output,
        "controls",
        study="cheeger-strategies",
        note="Publication rejects any certificate excluding the exact value or any incorrect decisive answer.",
    )


def render(result: dict[str, Any], output: Path) -> list[str]:
    """
    Render all measured strategy figures without rerunning graph computations.

    Args:
        result (dict[str, Any]): Saved measurements.
        output (Path): Figure directory.

    Returns:
        list[str]: Complete PNG/SVG inventory.
    """

    # Rendering consumes saved observations only; it never reruns the algorithms
    # or substitutes new timings while rebuilding a figure.
    return [path for plot in (churn, activation, parameters, cache, timeline, controls) for path in plot(result, output)]
