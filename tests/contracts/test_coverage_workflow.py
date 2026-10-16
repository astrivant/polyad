"""
Keep coverage complete across Python versions and badge writes isolated from releases.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.workflows import JOBS, PIPELINE

ROOT = Path(__file__).resolve().parents[2]


def test_coverage_measures_every_runtime_package_with_portable_paths():
    """
    Include unimported production files without counting tests or third-party dependencies.
    """
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    coverage = project["tool"]["coverage"]["run"]
    assert coverage["relative_files"] is True
    assert set(coverage["source"]) == {
        "pkg/polyad",
        "pkg/polyad-sdk/polyad_sdk",
        "pkg/polyad-types/polyad_types",
        "pkg/polyad-schemas/polyad_schemas",
        "pkg/polyad-benchmarks/polyad_benchmarks",
    }
    assert all((ROOT / path).is_dir() for path in coverage["source"])
    assert coverage["omit"] == ["*/tests/*"]
    assert "pytest-cov" in project["tool"]["poetry"]["group"]["dev"]["dependencies"]
    assert set(project["tool"]["poetry"]["group"]["coverage"]["dependencies"]) == {"coverage"}


def test_matrix_results_and_combined_reports_are_uploaded():
    """
    Upload hidden databases for both interpreters and retain reports for PRs as well as main.
    """
    python = JOBS["python"]
    test = next(step for step in python["steps"] if step.get("name") == "Run Python tests in parallel")
    assert "--cov --cov-config=pyproject.toml --cov-report=" in test["run"]
    assert "--junitxml=.cache/tests/pytest.xml" in test["run"]
    assert test["env"]["COVERAGE_FILE"] == ".cache/tests/.coverage.${{ matrix.python }}"
    upload = next(step for step in python["steps"] if step.get("name") == "Upload Python test results")
    # Failed test runs retain their results; setup failures have no results to upload.
    assert upload["if"] == f"always() && steps.{test['id']}.outcome != 'skipped'"
    assert upload["with"]["include-hidden-files"] == "true"
    assert upload["with"]["name"] == "python-test-results-${{ matrix.python }}"

    combined = JOBS["coverage"]
    assert combined["needs"] == "python" and "if" not in combined
    combine = next(step["run"] for step in combined["steps"] if "coverage combine" in step.get("run", ""))
    versions = python["strategy"]["matrix"]["python"]
    assert f"for version in {' '.join(versions)}; do" in combine
    assert "coverage json -o coverage.json" in combine
    assert "coverage html -d .cache/coverage-html" in combine
    assert "GITHUB_STEP_SUMMARY" in combine
    install = next(step["run"] for step in combined["steps"] if step.get("name") == "Install locked coverage tools")
    assert "poetry install --only coverage --no-root --no-interaction" in install
    artifact = combined["steps"][-1]["with"]
    assert artifact["name"] == "python-coverage-${{ matrix.package }}" and artifact["retention-days"] == "30"
    assert "coverage.json" in artifact["path"] and ".cache/coverage-html/" in artifact["path"]
    assert "coverage" in JOBS["test-complete"]["needs"]


def test_coverage_matrix_reports_each_package_without_rerunning_tests():
    """
    Slice shared coverage data into package reports and a properly weighted overall report.
    """
    job = JOBS["coverage"]
    assert job["strategy"]["fail-fast"] == "false"
    assert job["strategy"]["matrix"]["include"] == [
        {"package": "all", "files": "pkg/*"},
        {"package": "polyad", "files": "pkg/polyad/*"},
        {"package": "polyad-sdk", "files": "pkg/polyad-sdk/polyad_sdk/*"},
        {"package": "polyad-types", "files": "pkg/polyad-types/polyad_types/*"},
        {"package": "polyad-schemas", "files": "pkg/polyad-schemas/polyad_schemas/*"},
        {"package": "polyad-benchmarks", "files": "pkg/polyad-benchmarks/polyad_benchmarks/*"},
    ]
    assert job["env"] == {"COVERAGE_PACKAGE": "${{ matrix.package }}", "COVERAGE_INCLUDE": "${{ matrix.files }}"}
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "pytest" not in commands
    assert commands.count('--include "$COVERAGE_INCLUDE"') == 3
    assert "--format=markdown" in commands


@pytest.mark.parametrize("missing", [None, "3.13", "3.14"])
def test_combination_rejects_missing_matrix_members_before_reporting(tmp_path, missing):
    """
    An incomplete artifact set must fail before any summary or badge can be produced.
    """
    for version in JOBS["python"]["strategy"]["matrix"]["python"]:
        if version == missing:
            continue
        path = tmp_path / f".cache/coverage-shards/python-test-results-{version}/.coverage.{version}"
        path.parent.mkdir(parents=True)
        path.write_text("coverage data")
    poetry = tmp_path / "poetry"
    poetry.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$POETRY_LOG"\n')
    poetry.chmod(0o755)
    log = tmp_path / "commands"
    command = next(step["run"] for step in JOBS["coverage"]["steps"] if "coverage combine" in step.get("run", ""))
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "POETRY_LOG": str(log),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "COVERAGE_PACKAGE": "polyad-sdk",
            "COVERAGE_INCLUDE": "pkg/polyad-sdk/polyad_sdk/*",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is (missing is None), result.stderr
    assert log.exists() is (missing is None)


def test_badge_is_a_separate_writer_and_never_blocks_package_publication():
    """
    Keep measured main-branch coverage separate from tag publication and protected source history.
    """
    badge = JOBS["coverage-badge"]
    assert badge["needs"] == ["source", "test-stage"]
    assert badge["if"] == ("github.event_name == 'push' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)")
    assert badge["permissions"] == {"contents": "write"}
    assert badge["concurrency"] == {"group": "coverage-badge", "cancel-in-progress": "false"}
    assert PIPELINE["permissions"] == {"contents": "read"}
    assert {name for name, job in JOBS.items() if job.get("permissions", {}).get("contents") == "write"} == {
        "coverage-badge",
        "deploy-stage",
        "publish",
    }
    assert "coverage-badge" not in JOBS["verified"]["needs"]
    assert "coverage-badge" not in JOBS["deploy-stage"]["needs"]
    action = badge["steps"][-1]
    assert action["uses"] == "actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3"
    assert "scripts/coverage/publish-coverage.mjs" in action["with"]["script"]
    assert badge["steps"][0]["with"]["persist-credentials"] == "false"
    download = next(step for step in badge["steps"] if step.get("uses", "").startswith("actions/download-artifact@"))
    assert download["with"]["name"] == "python-coverage-all"
    readme = (ROOT / "README.md").read_text()
    assert "https://raw.githubusercontent.com/astrivant/polyad/gh-pages/badges/coverage.svg" in readme
    assert "](https://github.com/astrivant/polyad/actions/workflows/ci.yml)" in readme


def test_equal_coverage_keeps_each_measurement_source(tmp_path: Path) -> None:
    """
    Refresh source evidence even when consecutive revisions have equal coverage.

    Args:
        tmp_path (Path): Isolated reports and output directories.

    Returns:
        None: Equal badge bytes retain distinct, verifiable source receipts.
    """
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"totals": {"num_statements": 100, "covered_lines": 82}}))
    outputs = [tmp_path / "first", tmp_path / "second"]
    for output, source in zip(outputs, ("a" * 40, "b" * 40), strict=True):
        subprocess.run(
            ["node", str(ROOT / "scripts/coverage/coverage-badge.mjs"), str(report), str(output), source],
            check=True,
        )
        assert json.loads((output / "coverage.json").read_text())["source"] == source
    assert (outputs[0] / "coverage.svg").read_bytes() == (outputs[1] / "coverage.svg").read_bytes()
