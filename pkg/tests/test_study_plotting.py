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
from polyad_benchmarks.studies.descriptions import INTRODUCTIONS, describe, describe_axis

ROOT = Path(__file__).resolve().parents[2]
PLOTTED = ("load", "symbiosis", "reachability-state", "reachability-routing", "cheeger-reduction", "cheeger-strategies")


def result_for(study):
    """
    Read measured local results or an explicitly synthetic cloud plotting test fixture.
    """
    if study == "load":
        return json.loads((ROOT / "pkg/tests/data/studies/load-results.json").read_text())
    directory = "cheeger-reduction-deprecated" if study == "cheeger-reduction" else study
    return json.loads((ROOT / "studies" / directory / "results.json").read_text())["studies"][study]


def test_every_study_has_figures_and_base_imports_stay_light():
    """
    Require plot coverage for every study without loading Matplotlib or numerical backends at base import.
    """
    # Archived measurements remain renderable without rejoining refresh suites.
    assert set(plotting.FIGURE_NAMES) == set(refresh.STUDIES + refresh.LOCAL_STUDIES) | {"cheeger-reduction"}
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
            assert b'id="subplot-description"' in data
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


def test_strategy_timeline_plots_fresh_work_separately_from_cache_hits(monkeypatch, tmp_path):
    """
    Show every returned reduction certificate, not just successful cache lookups.
    """
    import matplotlib.pyplot as plt

    from polyad_benchmarks.studies.cheeger_strategies import plotting as strategies

    result = result_for("cheeger-strategies")
    captured = []

    def capture(figure, *args, **kwargs):
        captured.append(figure)
        return []

    monkeypatch.setattr(strategies, "save", capture)
    try:
        strategies.timeline(result, tmp_path)
        stages = [
            axis for axis in captured[0].axes if [label.get_text() for label in axis.get_yticklabels()] == list(strategies.STAGE_NAMES)
        ]
        assert stages
        for axis in stages:
            shown = {int(collection.get_offsets()[0, 1]) for collection in axis.collections}
            assert shown == {0, 1, 2}
    finally:
        for figure in captured:
            plt.close(figure)


def test_churn_adds_pid_legend_and_plots_observed_refresh_feedback(monkeypatch, tmp_path):
    """
    Render the additional comparator from real observations, including its control signal.
    """
    import matplotlib.pyplot as plt

    from polyad.graph.reduction import clear_reduction_cache
    from polyad_benchmarks.studies.cheeger_strategies import plotting as strategies
    from polyad_benchmarks.studies.cheeger_strategies.experiment import Experiment

    config = json.loads((ROOT / "studies/cheeger-strategies/fixtures/scenario.json").read_text())
    config.update(fixedVertices=8, fixedComponents=2, fixedSupernodes=4, topologies=["path"], seeds=[11], repetitions=1)
    experiment = Experiment(config)
    figures = []
    monkeypatch.setattr(strategies, "save", lambda figure, *args, **kwargs: figures.append(figure) or [])
    try:
        experiment.pid_churn()
        strategies.churn({"records": experiment.records, "recipe": config}, tmp_path)
        assert len(figures[0].axes) == 6
        for index in (0, 1, 2):
            assert "PID cached spectral" in figures[0].axes[index].get_legend_handles_labels()[1]
        intervals = figures[0].axes[4].lines[0].get_ydata()
        assert list(intervals) == pytest.approx([row["pid"]["intervalAfter"] for row in experiment.records])
        events = {line.get_label(): list(line.get_ydata()) for line in figures[0].axes[5].lines}
        assert events["Cache fallback (PID failure)"] == [row["pid"]["failure"] for row in experiment.records]
        assert events["Scheduled fresh reduction"] == [row["pid"]["refreshScheduled"] for row in experiment.records]

        # Archived results without PID observations retain their original layout.
        strategies.churn({"records": []}, tmp_path)
        assert len(figures[1].axes) == 4
    finally:
        clear_reduction_cache()
        for figure in figures:
            plt.close(figure)


def test_outer_pid_plots_measured_targets_and_sparse_window_feedback(monkeypatch, tmp_path):
    """
    Plot real controller state without filling missing outer-loop observations with zero.
    """
    import matplotlib.pyplot as plt

    from polyad.graph.reduction import clear_reduction_cache
    from polyad_benchmarks.studies.cheeger_strategies import plotting as strategies
    from polyad_benchmarks.studies.cheeger_strategies.experiment import Experiment

    config = json.loads((ROOT / "studies/cheeger-strategies/fixtures/scenario.json").read_text())
    config.update(fixedVertices=8, fixedComponents=2, fixedSupernodes=4, seeds=[11], repetitions=1)
    config["pidFeedback"]["observationsPerPhase"] = 4
    experiment = Experiment(config)
    figures = []
    monkeypatch.setattr(strategies, "save", lambda figure, *args, **kwargs: figures.append(figure) or [])
    try:
        experiment.pid_feedback()
        strategies.pid_feedback({"records": experiment.records, "recipe": config}, tmp_path)
        figure = figures[0]
        assert len(figure.axes) == 6
        labels = figure.axes[0].get_legend_handles_labels()[1]
        assert {"Fixed-target PID", "Adaptive-target PID", "Computation-time goal"} <= set(labels)
        target = next(line for line in figure.axes[1].lines if line.get_label() == "Adaptive-target PID")
        rows = [row for row in experiment.records if row["strategy"] == "Adaptive-target PID"]
        assert list(target.get_ydata()) == [row["pid"]["targetCacheRate"] for row in rows]
        updates = [row for row in rows if row["outerPid"]["updated"]]
        errors = next(line for line in figure.axes[4].lines if line.get_label() == "Adaptive-target PID")
        assert list(errors.get_xdata()) == [row["step"] for row in updates]
        assert list(errors.get_ydata()) == [row["outerPid"]["normalizedError"] for row in updates]
        strategies.pid_feedback({"records": []}, tmp_path)
        assert all(any("not collected" in text.get_text() for text in axis.texts) for axis in figures[1].axes)
    finally:
        clear_reduction_cache()
        for figure in figures:
            plt.close(figure)


def test_accuracy_feedback_plots_certificates_and_audited_tail_separately(monkeypatch, tmp_path):
    """
    Show the controller's actual signal and independently checked error without mixing them.
    """
    import matplotlib.pyplot as plt

    from polyad.graph.reduction import clear_reduction_cache
    from polyad_benchmarks.studies.cheeger_strategies import quality
    from polyad_benchmarks.studies.cheeger_strategies.experiment import Experiment

    config = json.loads((ROOT / "studies/cheeger-strategies/fixtures/scenario.json").read_text())
    config.update(fixedVertices=8, fixedComponents=2, fixedSupernodes=4, seeds=[11], repetitions=1)
    config["pidAccuracy"]["observationsPerPhase"] = 4
    experiment = Experiment(config)
    figures = []
    monkeypatch.setattr(quality, "save", lambda figure, *args, **kwargs: figures.append(figure) or [])
    try:
        experiment.pid_accuracy()
        quality.pid_accuracy({"records": experiment.records, "recipe": config}, tmp_path)
        rows = [row for row in experiment.records if row["strategy"] == "Accuracy-target PID"]
        assert len(figures[0].axes) == 6
        for index, field in ((0, "certificateGap"), (1, "relativeError"), (4, "durationSeconds"), (5, "millicoreSeconds")):
            line = next(line for line in figures[0].axes[index].lines if line.get_label() == "Accuracy-target PID")
            assert list(line.get_ydata()) == pytest.approx([row[field] for row in rows])
        updates = [row for row in rows if row["accuracyPid"]["updated"]]
        line = next(line for line in figures[0].axes[3].lines if line.get_label() == "Accuracy-target PID")
        assert list(line.get_xdata()) == [row["step"] for row in updates]
        assert list(line.get_ydata()) == [row["accuracyPid"]["normalizedError"] for row in updates]
        quality.pid_accuracy({"records": []}, tmp_path)
        assert all(any("not collected" in text.get_text() for text in axis.texts) for axis in figures[1].axes)
    finally:
        clear_reduction_cache()
        for figure in figures:
            plt.close(figure)


def test_cpu_companions_plot_measured_work_and_paired_relative_cost(monkeypatch, tmp_path):
    """
    Keep CPU work separate from occupied cores and do not invent archived measurements.
    """
    import matplotlib.pyplot as plt

    from polyad_benchmarks.studies.cheeger_strategies import costs

    recipe = {"fixedComponents": 2, "fixedTopology": "path"}
    shared = {"sweep": "pid-feedback", "seed": 11, "repeat": 0, "step": 0, "phaseStep": 0, "durationSeconds": 0.002}
    rows = [
        {**shared, "strategy": "Fresh spectral", "cpuSeconds": 0.002, "averageMillicores": 1000, "millicoreSeconds": 2},
        {**shared, "strategy": "Adaptive-target PID", "cpuSeconds": 0.001, "averageMillicores": 500, "millicoreSeconds": 1},
    ]
    figures = []
    monkeypatch.setattr(costs, "save", lambda figure, *args, **kwargs: figures.append(figure) or [])
    try:
        costs.cpu_cost({"records": rows, "recipe": recipe}, tmp_path)
        assert len(figures[0].axes) == 4 and len(figures[1].axes) == 6
        for panel, value in ((1, 500), (2, 1), (3, 0.5)):
            line = next(line for line in figures[0].axes[panel].lines if line.get_label() == "Adaptive-target PID")
            assert list(line.get_ydata()) == [value]
        costs.cpu_cost({"records": [{**shared, "strategy": "Fresh spectral"}], "recipe": recipe}, tmp_path)
        assert all(any("not collected" in text.get_text() for text in axis.texts) for axis in figures[2].axes)
    finally:
        for figure in figures:
            plt.close(figure)


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


@pytest.mark.parametrize("size", [(4.8, 4.8), (8, 3.5)])
def test_subplot_descriptions_fit_beneath_titles_and_above_panels(size):
    """
    Keep each panel explanation between its title and measured plotting area.
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=size, layout="constrained")
    canvas = FigureCanvasAgg(figure)
    axis = figure.subplots()
    description = describe_axis(
        axis,
        "Measured service state",
        "Color reports contract state; outlines identify active worker replacement.",
    )
    axis.plot([0, 1], [0, 1])
    canvas.draw()
    renderer = canvas.get_renderer()
    title_bounds = axis.title.get_window_extent(renderer)
    description_bounds = description.get_window_extent(renderer)
    panel_bounds = axis.get_window_extent(renderer)
    assert axis.title.get_gid() == "subplot-title"
    assert description.get_gid() == "subplot-description"
    assert title_bounds.y0 > description_bounds.y1
    assert description_bounds.y0 >= panel_bounds.y1
    assert 0 <= description_bounds.x0 < description_bounds.x1 <= figure.bbox.width


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
