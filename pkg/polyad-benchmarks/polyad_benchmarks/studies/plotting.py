"""
Load optional study plotters and export measured figures without starting experiments.
"""

from __future__ import annotations

import hashlib
import importlib
from typing import TYPE_CHECKING

from polyad_benchmarks.studies.descriptions import INTRODUCTIONS, describe_axis

__all__ = ["describe_axis"]

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

    from matplotlib.figure import Figure

FIGURE_NAMES = {study: tuple(figures) for study, figures in INTRODUCTIONS.items()}


def figure_names(study: str) -> list[str]:
    """
    Declare the complete PNG and SVG inventory required for a study to finish.

    Args:
        study (str): Registered study name.

    Returns:
        list[str]: Safe relative filenames in rendering order.
    """
    return [f"{name}.{suffix}" for name in FIGURE_NAMES[study] for suffix in ("png", "svg")]


def renderer(study: str) -> Callable[[dict[str, Any], Path], list[str]]:
    """
    Import only the requested plotter and report missing optional dependencies early.

    Args:
        study (str): Study name from the refresh inventory.

    Returns:
        Callable[[dict[str, Any], Path], list[str]]: Renderer consuming recorded results.

    Raises:
        ValueError: The study has no registered plotter.
        RuntimeError: Matplotlib must be installed through the optional plots extra.
    """
    if study not in FIGURE_NAMES:
        raise ValueError(f"no plotter registered for {study}")
    try:
        if study in {"soul", "nature"}:
            module = importlib.import_module("polyad_benchmarks.studies.soul.plotting")
            return lambda result, output: module.render(study, result["records"], output)
        module = importlib.import_module(f"polyad_benchmarks.studies.{study.replace('-', '_')}.plotting")
    except ModuleNotFoundError as error:
        if error.name and error.name.split(".")[0] == "matplotlib":
            raise RuntimeError("Study plots require pip install 'polyad-benchmarks[plots]'") from error
        raise
    return module.render  # type: ignore[no-any-return]


def save(figure: Figure, output: Path, name: str, *, study: str, note: str = "") -> list[str]:
    """
    Save one measured figure as a README image and a standalone vector artifact.

    Args:
        figure (Figure): Completed Matplotlib figure.
        output (Path): Artifact directory, created when absent.
        name (str): Figure basename from the study inventory.
        study (str): Study whose registered title and question describe this figure.
        note (str): Optional reading aid or run identity below the question subtitle.

    Returns:
        list[str]: Relative PNG and SVG filenames.
    """
    import matplotlib.pyplot as plt
    from matplotlib.layout_engine import ConstrainedLayoutEngine

    from polyad_benchmarks.studies.descriptions import describe

    output.mkdir(parents=True, exist_ok=True)
    paths = []
    try:
        top = describe(figure, study, name, note=note)
        engine = figure.get_layout_engine()
        if isinstance(engine, ConstrainedLayoutEngine):
            engine.set(rect=(0, 0, 1, top))
        else:
            figure.tight_layout(rect=(0, 0, 1, top))
        for suffix in ("png", "svg"):
            path = output / f"{name}.{suffix}"
            figure.savefig(
                path,
                dpi=160,
                bbox_inches="tight",
                bbox_extra_artists=[*figure.get_default_bbox_extra_artists(), *figure.texts],
            )
            if suffix == "svg":
                path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n")
            paths.append(path.name)
    finally:
        plt.close(figure)
    return paths


def fingerprint(study: str, output: Path) -> dict[str, str]:
    """
    Hash expected figures without accepting filenames from result documents.

    Args:
        study (str): Registered study name.
        output (Path): Directory containing rendered figures.

    Returns:
        dict[str, str]: Complete inventory and SHA-256 hashes.
    """
    return {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in figure_names(study)}


def verify(study: str, result: dict[str, Any], output: Path) -> None:
    """
    Reject missing, unexpected or modified figures before publishing any study.

    Args:
        study (str): Registered study name.
        result (dict[str, Any]): Measured result with figure inventory and artifact hashes.
        output (Path): Directory holding the figures.

    Returns:
        None: All expected figures exist and match the recorded hashes.
    """
    if result.get("figures") != figure_names(study):
        raise ValueError(f"{study} figure inventory is incomplete or unexpected")
    try:
        current = fingerprint(study, output)
    except OSError as error:
        raise ValueError(f"{study} figure artifact is missing or unreadable") from error
    if any(result.get("artifacts", {}).get(name) != digest for name, digest in current.items()):
        raise ValueError(f"{study} figure artifacts changed")


def render(study: str, result: dict[str, Any], output: Path) -> None:
    """
    Render recorded evidence and attach the publication inventory to its result.

    Args:
        study (str): Registered study name.
        result (dict[str, Any]): Full raw result; measurements are never generated here.
        output (Path): Destination for PNG and SVG figures.

    Returns:
        None: The result gains figure names and hashes while preserving existing artifacts.
    """
    result["figures"] = renderer(study)(result, output)
    result["artifacts"] = {**result.get("artifacts", {}), **fingerprint(study, output)}
    verify(study, result, output)
