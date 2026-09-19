"""
Verify optional plots use recorded values and prevent incomplete figure publication.
"""

from __future__ import annotations

import copy
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from polyad_benchmarks import refresh
from polyad_benchmarks.studies import plotting
from polyad_benchmarks.studies.descriptions import INTRODUCTIONS, describe

ROOT = Path(__file__).resolve().parents[2]
PLOTTED = ("load", "symbiosis", "reachability-state", "reachability-routing")


def result_for(study):
    """
    Read measured local results or an explicitly synthetic cloud plotting test fixture.
    """
    if study == "load":
        return json.loads((ROOT / "pkg/tests/data/studies/load-results.json").read_text())
    return json.loads((ROOT / "studies" / study / "results.json").read_text())["studies"][study]


def test_every_study_has_figures_and_base_imports_stay_light():
    """
    Require plot coverage for every study without loading Matplotlib or numerical backends at base import.
    """
    assert set(plotting.FIGURE_NAMES) == set(refresh.STUDIES + refresh.LOCAL_STUDIES)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from polyad_benchmarks.studies import plotting; "
            "assert 'matplotlib' not in sys.modules; assert 'jax' not in sys.modules",
        ],
        check=True,
    )


@pytest.mark.parametrize("study", PLOTTED)
def test_each_plotter_exports_complete_verifiable_artifacts(study, tmp_path):
    """
    Require real PNG/SVG output, intact measurements and rejection of missing or changed figures.
    """
    result = result_for(study)
    before = copy.deepcopy({key: value for key, value in result.items() if key not in {"figures", "artifacts"}})
    plotting.render(study, result, tmp_path)
    assert {key: value for key, value in result.items() if key not in {"figures", "artifacts"}} == before
    plotting.verify(study, result, tmp_path)
    for name in plotting.figure_names(study):
        data = (tmp_path / name).read_bytes()
        assert data.startswith(b"\x89PNG") if name.endswith("png") else b"<svg" in data
        if name.endswith("svg"):
            assert b'id="plot-question"' in data
            assert b"?" in data
    path = tmp_path / result["figures"][0]
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        plotting.verify(study, result, tmp_path)
    path.unlink()
    with pytest.raises(ValueError, match="missing"):
        plotting.verify(study, result, tmp_path)
    result["figures"][0] = "../unrelated.png"
    with pytest.raises(ValueError, match="inventory"):
        plotting.verify(study, result, tmp_path)


def test_load_latencies_do_not_count_timeouts_as_completed_or_invent_acceptance(monkeypatch, tmp_path):
    """
    Inspect plot data so missing measurements remain absent and failed requests stay visible.
    """
    from polyad_benchmarks.studies.load import plotting as load

    figures = {}

    def capture(figure, output, name, **kwargs):
        figures[name] = figure
        return plotting.save(figure, output, name, **kwargs)

    monkeypatch.setattr(load, "save", capture)
    load.render(result_for("load"), tmp_path)
    collections = figures["latencies"].axes[0].collections
    assert list(collections[0].get_offsets()[:, 1]) == [0.1, 0.2, 0.3]
    assert list(collections[1].get_offsets()[:, 1]) == [1.5, 2.5]
    assert [bar.get_height() for bar in figures["outcomes"].axes[0].patches] == [2, 1, 1, 1]
    for line in figures["latencies"].axes[1].lines:
        assert line.get_ydata()[0] == 0
        assert line.get_ydata()[-1] == 1


def test_disabled_numerical_analysis_is_labeled_without_fabricated_timings(monkeypatch, tmp_path):
    """
    Analytic-only studies show missing solver measurements explicitly.
    """
    from polyad_benchmarks.studies.reachability_state import plotting as state

    result = result_for("reachability-state")
    for record in result["records"]:
        record.pop("numerical", None)
    figures = {}

    def capture(figure, output, name, **kwargs):
        figures[name] = figure
        return plotting.save(figure, output, name, **kwargs)

    monkeypatch.setattr(state, "save", capture)
    state.render(result, tmp_path)
    axis = figures["analysis-cost"].axes[2]
    assert not axis.patches
    assert any("disabled" in text.get_text() for text in axis.texts)


def test_missing_plot_dependency_fails_before_cluster_access(monkeypatch, tmp_path):
    """
    Report the optional extra before initiating a cloud experiment.
    """
    real_import = importlib.import_module

    def missing(name):
        if name == "polyad_benchmarks.studies.load.plotting":
            raise ModuleNotFoundError("missing optional dependency", name="matplotlib")
        return real_import(name)

    root = tmp_path / "run"
    refresh.prepare(ROOT, root)
    monkeypatch.setattr(importlib, "import_module", missing)
    monkeypatch.setattr(refresh, "cluster_study", lambda *_: pytest.fail("cluster contacted without plot dependencies"))
    with pytest.raises(RuntimeError, match=r"polyad-benchmarks\[plots\]"):
        refresh.study_phase(ROOT, root, "load", "test-context")
    assert not json.loads((root / "statuses/load.json").read_text())["success"]


@pytest.mark.parametrize("size", [(6.4, 4.8), (14, 4), (24, 10)])
def test_question_subtitles_fit_between_titles_and_panels(size):
    """
    Keep every study's question readable at small, wide and topology figure sizes.
    """
    from matplotlib.figure import Figure

    questions = set()
    for study, figures in INTRODUCTIONS.items():
        for name, (_, question) in figures.items():
            assert question.endswith("?") and question not in questions
            questions.add(question)
            figure = Figure(figsize=size, layout="constrained")
            axis = figure.subplots()
            axis.set_title("Measured values")
            top = describe(figure, study, name, note="Recorded measurements; missing samples remain missing.")
            figure.get_layout_engine().set(rect=(0, 0, 1, top))
            figure.canvas.draw()
            renderer = figure.canvas.get_renderer()
            title, subtitle, note = figure.texts
            assert subtitle.get_gid() == "plot-question"
            assert " ".join(subtitle.get_text().split()) == question
            bounds = [artist.get_window_extent(renderer) for artist in (title, subtitle, note, axis.title)]
            assert all(upper.y0 > lower.y1 for upper, lower in zip(bounds, bounds[1:], strict=False))
            assert all(0 <= box.x0 < box.x1 <= figure.bbox.width for box in bounds)
            figure.clear()


@pytest.mark.parametrize("layout", [None, "constrained"])
def test_export_keeps_question_header_inside_image(layout, tmp_path):
    """
    Tight export must include header artists excluded from automatic panel layout.
    """
    from matplotlib.figure import Figure
    from PIL import Image

    figure = Figure(figsize=(6.4, 4.8), layout=layout)
    figure.subplots().plot([1, 2], [3, 4])
    plotting.save(figure, tmp_path, "topology", study="nature", note="Recorded process identities.")
    with Image.open(tmp_path / "topology.png") as exported:
        assert exported.height >= (4.8 - 0.3) * 160
