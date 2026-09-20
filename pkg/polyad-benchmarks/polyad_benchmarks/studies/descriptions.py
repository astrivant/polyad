"""
State the question each study figure answers and keep its subtitle above the panels.
"""

from __future__ import annotations

from textwrap import fill
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure
    from matplotlib.text import Text

__all__ = (
    "INTRODUCTIONS",
    "describe",
    "describe_axis",
)


INTRODUCTIONS = {
    "load": {
        "outcomes": (
            "Cluster load: request outcomes",
            "How many offered requests completed, failed or were skipped during this run?",
        ),
        "latencies": (
            "Cluster load: observed request latency",
            "How long did API acceptance and successful completion take across the recorded requests?",
        ),
    },
    "symbiosis": {
        "interactions": (
            "Symbiosis: service relationships under the same demand",
            "How does each relationship affect the participants' capacity and whether their queues stay within the modeled limits?",
        ),
        "guard-cost": (
            "Symbiosis: runtime guard cost",
            "How much evaluation time and serialized storage does each relationship's reachability guard require?",
        ),
    },
    "reachability-state": {
        "state-tradeoffs": (
            "Reachability state: representation and resolution",
            "Which decisions disagree with the full analytic model when state is reduced, and how many grid points does each model need?",
        ),
        "analysis-cost": (
            "Reachability state: guard and analysis costs",
            "How do state representation and grid resolution change guard time, memory requirements and measured analysis time?",
        ),
    },
    "reachability-routing": {
        "routing": (
            "Reachability rerouting: consumer assignments and queue peaks",
            "Does guarded rerouting use the spare consumer while keeping outstanding work within the modeled queue limits?",
        ),
        "outcomes": (
            "Reachability rerouting: measured outcomes",
            "How does guarded rerouting change completion, rejection and latency under the same processing budget?",
        ),
    },
    "cheeger-reduction": {
        "accuracy": (
            "PCA Cheeger reduction: approximation accuracy",
            "How do retained dimensions, quotient size and graph topology change error against exact edge expansion?",
        ),
        "cost": (
            "PCA Cheeger reduction: work and uncertainty",
            "When does quotient search reduce computation, and how much certified uncertainty remains?",
        ),
        "stability": (
            "PCA Cheeger reduction: cached insurance for steady graphs",
            "How do cached and refreshed quotient cuts behave as a previously stable graph changes?",
        ),
    },
    "cheeger-strategies": {
        "churn": (
            "Cheeger strategies: accuracy and cost under churn",
            "How do cached cuts, PID-controlled refresh, fresh spectral cuts, PCA and the selector compare as graphs change?",
        ),
        "activation": (
            "Cheeger strategies: when each tier decides",
            "Which tier finishes as graph churn and the minimum, maximum or two-sided policy threshold change?",
        ),
        "parameters": (
            "Cheeger strategies: controlled parameter comparisons",
            "How do embedding dimensions, quotient size, original size and density affect error and computation?",
        ),
        "cache": (
            "Cheeger strategies: cache gates and competing boundaries",
            "When do churn limits and cache capacity cause a boundary to refresh or fall back to exact search?",
        ),
        "timeline": (
            "Cheeger strategies: one changing service boundary",
            "What does the selector do during steady periods, tighter policy, churn bursts and service membership changes?",
        ),
        "controls": (
            "Cheeger strategies: work limits and decision correctness",
            "Which controls resolve the policy, which exhaust their budgets, and does every decisive answer match exhaustive truth?",
        ),
        "pid-feedback": (
            "Cheeger strategies: adaptive cache targets and refresh feedback",
            "Can a slower time-feedback PID adjust cache reuse without hiding its refresh costs or cut errors?",
        ),
        "pid-cost": (
            "Cheeger strategies: CPU expense of cache-target feedback",
            "How much CPU work does each controller consume, and how does that differ from occupied millicores while it computes?",
        ),
        "cpu-cost": (
            "Cheeger strategies: CPU work across controlled graph changes",
            "How do churn, graph size and quotient size change CPU cost per calculation and average occupied cores?",
        ),
    },
    "soul": {
        "topology": (
            "Soul: observed process graph",
            "How do service connections and child workers change during load and constraints, then recover?",
        ),
        "adaptations": (
            "Soul: how services cope",
            "When do services change their worker counts or pause admission, and what happens to their backlogs?",
        ),
        "outcomes": (
            "Soul: measured outcomes under the same offered load",
            "How does local adaptation change completed work, completion latency and trial duration compared with fixed services?",
        ),
        "strategies": (
            "Soul: exercised SDK strategies",
            "Which SDK strategies run during local adaptation, and how often do guards allow, block or lack evidence for an action?",
        ),
        "service-level": (
            "Soul: service level through adaptation",
            "How do measured availability, latency and each service's contract state change while Soul adapts workers and admission?",
        ),
        "resources": (
            "Soul: resource discovery and response",
            "How does the local VPA analogue vary admitted memory, what does the SDK observe from the real cgroup, "
            "and how do workers respond?",
        ),
    },
    "nature": {
        "topology": (
            "Nature: observed process graph",
            "Which services survive, change implementation, join or retire as the required output changes and returns?",
        ),
        "adaptations": (
            "Nature: local adaptation inside selected services",
            "How do backlogs, worker counts and admission change within services as Natural Selection changes their composition?",
        ),
        "outcomes": (
            "Nature: measured outcomes under the same offered load",
            "How does composition selection change completed work, latency and duration when the required output changes?",
        ),
        "strategies": (
            "Nature: exercised SDK strategies",
            "Which SDK strategies run inside selected services, and how often do their guards allow, block or lack evidence for an action?",
        ),
        "service-level": (
            "Nature: service level through composition change",
            "How do availability, latency and service contract states change while Natural Selection replaces capabilities "
            "and Soul adapts workers?",
        ),
        "resources": (
            "Nature: resource discovery inside changing compositions",
            "How does modeled vertical allocation interact with real SDK cgroup observations and worker adaptation "
            "across service incarnations?",
        ),
    },
}


def describe_axis(axis: Axes, title: str, description: str) -> Text:
    """
    Place a concise interpretation beneath a subplot title.

    Args:
        axis (Axes): Subplot receiving the heading.
        title (str): Short name for the measurement shown.
        description (str): Sentence explaining how to read the panel.

    Returns:
        Text: Description artist positioned above the plotting area.
    """
    axis.set_title(title, pad=34, fontsize=11)
    axis.title.set_gid("subplot-title")
    artist = axis.text(
        0.5,
        1.01,
        fill(description, width=58),
        transform=axis.transAxes,
        ha="center",
        va="bottom",
        fontsize=8,
        color="#64748b",
        clip_on=False,
    )
    artist.set_gid("subplot-description")
    return artist


def describe(figure: Figure, study: str, name: str, *, note: str = "") -> float:
    """
    Add a title, question subtitle and optional reading aid above the plot panels.

    Args:
        figure (Figure): Completed study figure before export.
        study (str): Registered study name.
        name (str): Figure basename within that study.
        note (str): Optional legend explanation or run identity, placed below the question.

    Returns:
        float: Upper subplot boundary in figure coordinates, leaving room for the header.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    title, question = INTRODUCTIONS[study][name]

    # Measure rendered text height; a fixed margin would clip longer explanations on smaller figures.
    canvas = FigureCanvasAgg(figure)
    renderer = canvas.get_renderer()  # type: ignore[no-untyped-call]
    width, height = figure.get_figwidth(), figure.get_figheight()
    offset = 0.10
    for text, size, chars, gid, color in (
        (title, 16, 8, "plot-title", "#172033"),
        (question, 11, 12, "plot-question", "#475569"),
        (note, 9, 14, "plot-note", "#475569"),
    ):
        if not text:
            continue
        artist = figure.text(
            0.5,
            1 - offset / height,
            fill(text, width=max(30, int(width * chars))),
            ha="center",
            va="top",
            fontsize=size,
            color=color,
        )
        artist.set_gid(gid)
        artist.set_in_layout(False)
        offset += artist.get_window_extent(renderer).height / figure.dpi + 0.08
    return 1 - (offset + 0.10) / height
