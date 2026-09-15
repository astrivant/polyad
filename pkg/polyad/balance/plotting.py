"""
Render deterministic layered dependency graphs without a graph-layout dependency.
"""

from collections.abc import Mapping
from pathlib import Path

from polyad.graph import Work


def plot_graph(works: Mapping[str, Work], output: Path, *, title: str) -> None:
    """
    Save a directed graph with prerequisite depth on the vertical axis.

    Args:
        works (Mapping[str, Work]): Validated acyclic graph at one scheduling boundary.
        output (Path): Destination PNG.
        title (str): Revision or event description.

    Returns:
        None: A standalone matplotlib figure is written to disk.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    levels: dict[str, int] = {}
    while len(levels) < len(works):
        pending = [name for name, work in works.items() if name not in levels and all(parent in levels for parent in work.requires)]
        if not pending:
            raise ValueError("cannot plot cyclic or unresolved prerequisites")
        for name in pending:
            levels[name] = max((levels[parent] + 1 for parent in works[name].requires), default=0)
    layers = {level: [name for name in works if levels[name] == level] for level in sorted(set(levels.values()))}
    positions = {name: (index - (len(names) - 1) / 2, -level) for level, names in layers.items() for index, name in enumerate(names)}
    figure = Figure(figsize=(max(6, max((len(names) for names in layers.values()), default=1) * 2), max(4, len(layers) * 1.5)))
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    for name, work in works.items():
        for parent in work.requires:
            axis.annotate(
                "",
                xy=positions[name],
                xytext=positions[parent],
                arrowprops={"arrowstyle": "->", "color": "#64748b", "shrinkA": 22, "shrinkB": 22},
            )
    for name, (x, y) in positions.items():
        axis.text(x, y, name, ha="center", va="center", bbox={"boxstyle": "round,pad=0.6", "fc": "#dbeafe", "ec": "#2563eb"})
    xs = [position[0] for position in positions.values()] or [0]
    axis.set_xlim(min(xs) - 1, max(xs) + 1)
    axis.set_ylim(-max(levels.values(), default=0) - 0.7, 0.7)
    axis.set_title(title + "\nArrows show which units must finish before their dependents can start.", fontsize=11)
    axis.axis("off")
    figure.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=140)
    figure.clear()
