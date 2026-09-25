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
CHECKS = set(PIPELINE["jobs"]) - {"verified", "tag", "publish"}


def test_only_one_workflow_accepts_events_and_every_component_is_reachable():
    """
    Every push, pull request or manual run has one entry point with no detached follow-up run.
    """
    workflows = {path.name: yaml.load(path.read_text(), Loader=yaml.BaseLoader) for path in DIRECTORY.glob("*.yml")}
    assert set(PIPELINE["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert PIPELINE["on"]["push"] == {"branches": ["main"], "tags": ["v[0-9]*"]}
    for name, workflow in workflows.items():
        if name != "ci.yml":
            assert set(workflow["on"]) == {"workflow_call"}, name

    # Traverse actual local calls, rejecting orphan workflow files and recursion.
    reached = set()

    def visit(name, ancestors=()):
        assert name not in ancestors
        reached.add(name)
        for job in workflows[name]["jobs"].values():
            target = job.get("uses", "")
            if target.startswith("./.github/workflows/"):
                visit(Path(target).name, (*ancestors, name))

    visit("ci.yml")
    assert reached == set(workflows)


def test_all_validation_branches_join_before_tagging_or_publishing():
    """
    Studies are required release checks rather than unrelated workflow statuses.
    """
    gate = PIPELINE["jobs"]["verified"]
    assert gate["if"] == "always()" and set(gate["needs"]) == CHECKS
    assert {"benchmarks", "reachability", "process-studies", "chart", "operator", "lightweight-packages"} <= CHECKS
    for name in ("tag", "publish"):
        assert "verified" in PIPELINE["jobs"][name]["needs"]
        assert "always()" not in PIPELINE["jobs"][name]["if"]


def test_all_checkout_and_workflow_calls_use_the_resolved_source():
    """
    Manual tag runs and pull-request merge runs test one immutable commit across every suite.
    """
    for name, job in PIPELINE["jobs"].items():
        if name in {"source", "verified"}:
            continue
        dependencies = job["needs"]
        assert "source" in ([dependencies] if isinstance(dependencies, str) else dependencies)
        if "uses" in job:
            assert job["with"]["ref"] == "${{ needs.source.outputs.sha }}", name
        else:
            assert job["steps"][0]["with"]["ref"] == "${{ needs.source.outputs.sha }}", name
    for filename in ("benchmarks.yml", "reachability.yml", "process-studies.yml"):
        workflow = yaml.load((DIRECTORY / filename).read_text(), Loader=yaml.BaseLoader)
        assert workflow["on"]["workflow_call"]["inputs"]["ref"]["required"] == "true"
        for job in workflow["jobs"].values():
            checkout = job["steps"][0]
            assert checkout["uses"] == "actions/checkout@v4"
            assert checkout["with"]["ref"] in {"${{ inputs.ref }}", "${{ needs.refresh-prepare.outputs.revision }}"}


def test_manual_cloud_benchmarks_remain_explicit_and_environment_protected():
    """
    Consolidation must not grant ordinary pushes or pull requests access to the cloud runner.
    """
    inputs = PIPELINE["on"]["workflow_dispatch"]["inputs"]
    assert inputs["full-refresh"]["default"] == "false" and inputs["tag"]["default"] == ""
    benchmarks = PIPELINE["jobs"]["benchmarks"]
    assert benchmarks["with"]["full-refresh"] == "${{ github.event_name == 'workflow_dispatch' && inputs.full-refresh }}"
    workflow = yaml.load((DIRECTORY / "benchmarks.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["jobs"]["refresh-prepare"]["if"] == "inputs.full-refresh"
    assert workflow["jobs"]["refresh-study"]["environment"] == "benchmarks"
    assert workflow["jobs"]["refresh-study"]["needs"] == "refresh-prepare"


def test_tagged_chart_artifacts_cannot_collide_with_initial_validation():
    """
    Initial checks and post-tag revalidation upload different names within the same workflow run.
    """
    chart = yaml.load((DIRECTORY / "chart.yml").read_text(), Loader=yaml.BaseLoader)
    tag = yaml.load((DIRECTORY / "tag.yml").read_text(), Loader=yaml.BaseLoader)
    prefix = chart["on"]["workflow_call"]["inputs"]["artifact-prefix"]["default"]
    first = PIPELINE["jobs"]["chart"]["with"].get("artifact-prefix", prefix)
    second = tag["jobs"]["chart"]["with"]["artifact-prefix"]
    assert first != second
    action = next(step for step in chart["jobs"]["chart"]["steps"] if step.get("uses") == "astrivant/hypothesis-helm@main")
    assert action["with"]["artifact-name"] == "${{ inputs.artifact-prefix }}-${{ matrix.chart }}"


def run_gate(results, *, main):
    """
    Execute the workflow's real aggregate check against synthetic job conclusions.
    """
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", PIPELINE["jobs"]["verified"]["steps"][0]["run"]],
        env={**os.environ, "RESULTS_JSON": json.dumps(results), "MAIN_PUSH": str(main).lower()},
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize("name", sorted(CHECKS))
@pytest.mark.parametrize("status", ["failure", "cancelled", "skipped"])
def test_gate_rejects_incomplete_main_runs(name, status):
    """
    A failed, cancelled or unexpectedly skipped suite must never permit release writes.
    """
    results = {job: {"result": "success"} for job in CHECKS}
    results[name]["result"] = status
    result = run_gate(results, main=True)
    assert result.returncode != 0 and name in result.stderr


@pytest.mark.parametrize("main", [True, False])
def test_gate_accepts_success_and_only_the_expected_compose_skip(main):
    """
    PR and tag runs can skip main-only Compose while every study remains mandatory.
    """
    results = {job: {"result": "success"} for job in CHECKS}
    if not main:
        results["compose"]["result"] = "skipped"
    result = run_gate(results, main=main)
    assert result.returncode == 0, result.stderr
    if not main:
        results["benchmarks"]["result"] = "skipped"
        assert run_gate(results, main=False).returncode != 0


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
