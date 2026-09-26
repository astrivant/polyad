"""
Keep all checks and release stages inside one source-pinned GitHub Actions run.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / ".github/workflows"
PIPELINE = yaml.load((DIRECTORY / "ci.yml").read_text(), Loader=yaml.BaseLoader)
CHECKS = set(PIPELINE["jobs"]) - {"verified", "publish"}


def test_one_workflow_contains_every_job_without_local_actions_or_workflow_calls():
    """
    Every push, pull request or manual run has one entry point with no detached follow-up run.
    """
    workflows = {path.name: yaml.load(path.read_text(), Loader=yaml.BaseLoader) for path in DIRECTORY.glob("*.yml")}
    assert set(workflows) == {"ci.yml"}
    assert not list((ROOT / ".github/actions").rglob("action.y*ml"))
    assert set(PIPELINE["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert PIPELINE["on"]["push"] == {"branches": ["main"], "tags": ["v[0-9]*"]}
    for job in PIPELINE["jobs"].values():
        assert "uses" not in job
        assert "runs-on" in job and "steps" in job
        assert all(not step.get("uses", "").startswith("./") for step in job["steps"])

    # Traverse job dependencies, rejecting cycles and dangling edges after flattening.
    reached = set()

    def visit(name, ancestors=()):
        assert name not in ancestors
        reached.add(name)
        dependencies = PIPELINE["jobs"][name].get("needs", [])
        for dependency in [dependencies] if isinstance(dependencies, str) else dependencies:
            visit(dependency, (*ancestors, name))

    visit("publish")
    assert reached == set(PIPELINE["jobs"])


def test_all_validation_branches_join_before_publishing():
    """
    Studies are required release checks rather than unrelated workflow statuses.
    """
    gate = PIPELINE["jobs"]["verified"]
    assert gate["if"] == "always()" and set(gate["needs"]) == CHECKS
    assert {
        "cheeger-studies",
        "benchmark-smoke",
        "benchmark-images",
        "benchmark-refresh-prepare",
        "benchmark-refresh-study",
        "benchmark-refresh-finish",
        "reachability-sdk",
        "reachability-prepare",
        "reachability-study",
        "reachability-finish",
        "process-tests",
        "process-prepare",
        "process-study",
        "process-finish",
        "chart",
        "chart-package",
        "operator",
        "lightweight-packages",
    } <= CHECKS
    assert "verified" in PIPELINE["jobs"]["publish"]["needs"]
    assert "always()" not in PIPELINE["jobs"]["publish"]["if"]


def test_all_checkouts_use_the_resolved_source():
    """
    Manual tag runs and pull-request merge runs test one immutable commit across every suite.
    """
    for name, job in PIPELINE["jobs"].items():
        if name in {"source", "verified"}:
            continue
        dependencies = job["needs"]
        assert "source" in ([dependencies] if isinstance(dependencies, str) else dependencies)
        checkout = job["steps"][0]
        assert checkout["uses"] == "actions/checkout@v4"
        assert checkout["with"]["ref"] == "${{ needs.source.outputs.sha }}", name


def test_manual_cloud_benchmarks_remain_explicit_and_environment_protected():
    """
    Consolidation must not grant ordinary pushes or pull requests access to the cloud runner.
    """
    inputs = PIPELINE["on"]["workflow_dispatch"]["inputs"]
    assert inputs["full-refresh"]["default"] == "false" and inputs["tag"]["default"] == ""
    prepare = PIPELINE["jobs"]["benchmark-refresh-prepare"]
    assert prepare["if"] == "github.event_name == 'workflow_dispatch' && inputs.full-refresh"
    assert prepare["needs"] == ["source", "benchmark-smoke", "benchmark-images"]
    study = PIPELINE["jobs"]["benchmark-refresh-study"]
    assert study["environment"] == "benchmarks"
    assert study["needs"] == ["source", "benchmark-refresh-prepare"]
    assert study["env"]["BENCHMARK_CONTEXT"] == "${{ inputs.context }}"
    assert study["strategy"]["matrix"] == "${{ fromJSON(needs.benchmark-refresh-prepare.outputs.matrix) }}"


@pytest.mark.parametrize("suite,prerequisite", [("reachability", "reachability-sdk"), ("process", "process-tests")])
def test_study_matrices_keep_their_prepare_test_and_finish_barriers(suite, prerequisite):
    """
    Dynamic studies cannot run before their tests or finish before matrix artifacts arrive.
    """
    study = PIPELINE["jobs"][f"{suite}-study"]
    assert study["needs"] == ["source", prerequisite, f"{suite}-prepare"]
    assert study["strategy"]["matrix"] == f"${{{{ fromJSON(needs.{suite}-prepare.outputs.matrix) }}}}"
    finish = PIPELINE["jobs"][f"{suite}-finish"]
    assert finish["needs"] == ["source", f"{suite}-prepare", f"{suite}-study"]
    assert finish["if"] == f"always() && needs.{suite}-prepare.result == 'success'"


def test_releases_cannot_be_cancelled_by_newer_pr_commits():
    """
    Match hypothesis-helm's PR-only cancellation while isolating explicitly selected tags.
    """
    assert PIPELINE["concurrency"] == {
        "group": "polyad-${{ github.event_name }}-${{ inputs.tag || github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    assert PIPELINE["defaults"]["run"]["shell"] == "bash"


def run_gate(results, *, main=False, release=False, full_refresh=False):
    """
    Execute the workflow's real aggregate check against synthetic job conclusions.
    """
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", PIPELINE["jobs"]["verified"]["steps"][0]["run"]],
        env={
            **os.environ,
            "RESULTS_JSON": json.dumps(results),
            "MAIN_PUSH": str(main).lower(),
            "RELEASE_TAG": "v0.0.1-alpha99" if release else "",
            "FULL_REFRESH": str(full_refresh).lower(),
        },
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize("name", sorted(CHECKS))
@pytest.mark.parametrize("status", ["failure", "cancelled", "skipped"])
def test_gate_rejects_every_incomplete_job_when_all_features_are_required(name, status):
    """
    A failed, cancelled or unexpectedly skipped suite must never permit release writes.
    """
    results = {job: {"result": "success"} for job in CHECKS}
    results[name]["result"] = status
    result = run_gate(results, main=True, release=True, full_refresh=True)
    assert result.returncode != 0 and name in result.stderr


@pytest.mark.parametrize(
    "main,release,full_refresh",
    [(True, False, False), (False, False, False), (False, True, False), (False, False, True), (False, True, True)],
)
def test_gate_accepts_only_the_skips_expected_for_the_selected_features(main, release, full_refresh):
    """
    Main, PR, tag and manual runs allow only their unselected optional branches to skip.
    """
    results = {job: {"result": "success"} for job in CHECKS}
    optional = set() if main else {"compose"}
    if not release:
        optional.add("chart-package")
    if not full_refresh:
        optional.update({"benchmark-refresh-prepare", "benchmark-refresh-study", "benchmark-refresh-finish"})
    for name in optional:
        results[name]["result"] = "skipped"
    result = run_gate(results, main=main, release=release, full_refresh=full_refresh)
    assert result.returncode == 0, result.stderr

    # Even an optional job may not fail silently if it did run.
    for name in optional:
        results[name]["result"] = "failure"
        assert run_gate(results, main=main, release=release, full_refresh=full_refresh).returncode != 0
        results[name]["result"] = "skipped"
    results["benchmark-smoke"]["result"] = "skipped"
    assert run_gate(results, main=main, release=release, full_refresh=full_refresh).returncode != 0


@pytest.fixture
def source_repository(tmp_path):
    """
    Provide two local commits and tags without touching the workspace's repository.
    """

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Pipeline Test", "-c", "user.email=pipeline@example.invalid", *args], cwd=tmp_path, text=True
        ).strip()

    git("init", "--quiet")
    git("commit", "--quiet", "--allow-empty", "--no-gpg-sign", "-m", "old")
    # Ignore workstation signing defaults and exercise both lightweight and annotated tags.
    git("tag", "--no-sign", "v0.0.1-alpha1")
    git("commit", "--quiet", "--allow-empty", "--no-gpg-sign", "-m", "current")
    git("tag", "--no-sign", "--annotate", "v0.0.1-alpha2", "--message", "current")
    return tmp_path, git("rev-parse", "HEAD")


@pytest.mark.parametrize(
    "ref,requested,expected",
    [
        ("refs/heads/main", "", ""),
        ("refs/pull/1/merge", "", ""),
        ("refs/tags/v0.0.1-alpha2", "", "v0.0.1-alpha2"),
        ("refs/heads/main", "v0.0.1-alpha2", "v0.0.1-alpha2"),
        ("refs/heads/main", "v0.0.1-alpha1", None),
        ("refs/heads/main", "v0.0.1-alpha3", None),
        ("refs/heads/main", "v0.0.1-alpha2\nsha=bad", None),
    ],
)
def test_source_selection_fences_tags_and_preserves_safe_outputs(source_repository, ref, requested, expected):
    """
    Pin the checked-out commit and reject missing, moved or malformed release tags before emitting outputs.
    """
    directory, sha = source_repository
    output = directory / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", PIPELINE["jobs"]["source"]["steps"][1]["run"]],
        cwd=directory,
        env={**os.environ, "GITHUB_REF": ref, "REQUESTED_TAG": requested, "GITHUB_OUTPUT": str(output)},
        text=True,
        capture_output=True,
    )
    assert (result.returncode == 0) is (expected is not None), result.stderr
    if expected is None:
        assert not output.exists()
    else:
        assert output.read_text() == f"sha={sha}\nrelease-tag={expected}\n"
