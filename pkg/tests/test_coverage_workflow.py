"""
Keep coverage complete across Python versions and badge writes isolated from releases.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


def test_badge_is_the_only_writer_and_never_blocks_package_publication():
    """
    Match hypothesis-helm's badge action without granting write access to PR or tag jobs.
    """
    badge = JOBS["coverage-badge"]
    assert badge["needs"] == ["source", "test-stage"]
    assert badge["if"] == ("github.event_name == 'push' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)")
    assert badge["permissions"] == {"contents": "write"}
    assert badge["concurrency"] == {"group": "coverage-badge", "cancel-in-progress": "false"}
    assert PIPELINE["permissions"] == {"contents": "read"}
    assert {name for name, job in JOBS.items() if job.get("permissions", {}).get("contents") == "write"} == {"coverage-badge"}
    assert "coverage-badge" not in JOBS["verified"]["needs"]
    assert "coverage-badge" not in JOBS["deploy-stage"]["needs"]
    action = badge["steps"][-1]
    assert action["uses"] == "we-cli/coverage-badge-action@8a0b6ee05f6dd0f294089cbe7a848452a2b43eef"
    assert action["if"] == "steps.badge.outputs.changed == 'true'"
    download = next(step for step in badge["steps"] if step.get("uses", "").startswith("actions/download-artifact@"))
    assert download["with"]["name"] == "python-coverage-all"
    readme = (ROOT / "README.md").read_text()
    assert "https://raw.githubusercontent.com/astrivant/polyad/gh-pages/badges/coverage.svg" in readme
    assert "](https://github.com/astrivant/polyad/actions/workflows/ci.yml)" in readme


@pytest.mark.parametrize("previous,changed", [(None, True), ("81", True), ("82", False)])
def test_badge_only_updates_when_displayed_coverage_changes(tmp_path, previous, changed):
    """
    Exercise first publication, changed coverage and no-op refreshes without a remote write.
    """
    (tmp_path / "coverage.json").write_text(json.dumps({"totals": {"percent_covered_display": "82"}}))
    old = tmp_path / "old.svg"
    if previous is not None:
        old.write_text(f'<svg xmlns="http://www.w3.org/2000/svg"><text>{previous}%</text></svg>')
    git = tmp_path / "git"
    git.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        '  ls-remote) if [ -f "$OLD_BADGE" ]; then printf "abc refs/heads/gh-pages\\n"; fi ;;\n'
        "  fetch) exit 0 ;;\n"
        '  show) cat "$OLD_BADGE" ;;\n'
        "  *) exit 99 ;;\nesac\n"
    )
    git.chmod(0o755)
    (tmp_path / "python").symlink_to(sys.executable)
    output = tmp_path / "output"
    command = next(step["run"] for step in JOBS["coverage-badge"]["steps"] if step.get("id") == "badge")
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "OLD_BADGE": str(old),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"changed={str(changed).lower()}\n"
