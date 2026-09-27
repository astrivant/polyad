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
CHECKS = set(PIPELINE["jobs"]) - {"verified", "publish", "coverage-badge"}


def test_one_workflow_contains_every_job_without_local_actions_or_workflow_calls():
    """
    Every push, pull request or manual run has one entry point with no detached follow-up run.
    """
    workflows = {path.name: yaml.load(path.read_text(), Loader=yaml.BaseLoader) for path in DIRECTORY.glob("*.yml")}
    assert set(workflows) == {"ci.yml"}
    assert not list((ROOT / ".github/actions").rglob("action.y*ml"))
    assert set(PIPELINE["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert PIPELINE["on"]["push"] == {"branches": ["**"], "tags": ["**"]}
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
    visit("coverage-badge")
    assert reached == set(PIPELINE["jobs"])


def test_all_validation_branches_join_before_publishing():
    """
    Requested studies and ordinary checks must succeed inside the same pipeline.
    """
    gate = PIPELINE["jobs"]["verified"]
    assert gate["if"] == "always()" and set(gate["needs"]) == CHECKS
    assert {
        "studies",
        "benchmark-smoke",
        "benchmark-images",
        "benchmark-refresh",
        "reachability-sdk",
        "process-tests",
        "chart",
        "chart-package",
        "operator",
        "lightweight-packages",
        "coverage",
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
    assert inputs["refresh"]["default"] == "false" and inputs["pull-request"]["default"] == ""
    study = PIPELINE["jobs"]["benchmark-refresh"]
    assert study["if"] == "needs.source.outputs.refresh == 'true' && inputs.full-refresh"
    assert study["environment"] == "benchmarks"
    assert study["needs"] == ["source", "benchmark-smoke", "benchmark-images"]
    assert study["env"]["BENCHMARK_CONTEXT"] == "${{ inputs.context }}"
    assert study["runs-on"] == "${{ vars.POLYAD_BENCHMARK_RUNNER || 'polyad-benchmarks' }}"
    source = PIPELINE["jobs"]["source"]
    request = source["steps"][-1]
    assert request["if"] == "github.event_name == 'workflow_dispatch' && (inputs.refresh || inputs.full-refresh)"
    assert source["outputs"]["refresh"] == "${{ steps.request.outputs.refresh }}"
    assert source["permissions"] == {"contents": "read", "pull-requests": "read"}


def test_studies_share_one_optional_matrix_and_validation_tests_run_on_every_ref():
    """
    Preparation and collection are steps, not extra job columns or mandatory benchmark runs.
    """
    study = PIPELINE["jobs"]["studies"]
    assert study["needs"] == "source"
    assert study["if"] == "needs.source.outputs.refresh == 'true'"
    assert study["strategy"]["fail-fast"] == "false"
    assert {row["suite"] for row in study["strategy"]["matrix"]["include"]} == {"cheeger", "reachability", "process"}
    assert not any(name.endswith(("-prepare", "-study", "-finish")) for name in PIPELINE["jobs"])
    for name in (
        "python",
        "terraform",
        "chart",
        "container",
        "compose",
        "operator",
        "benchmark-smoke",
        "benchmark-images",
        "reachability-sdk",
        "process-tests",
    ):
        assert "if" not in PIPELINE["jobs"][name], name
    assert PIPELINE["jobs"]["reachability-sdk"]["strategy"]["matrix"]["python"] == ["3.11", "3.12", "3.13", "3.14"]
    assert PIPELINE["jobs"]["process-tests"]["strategy"]["matrix"]["python"] == ["3.13", "3.14"]


@pytest.mark.parametrize("name", ["studies", "benchmark-refresh"])
def test_consolidated_studies_keep_completion_checks_and_failure_artifacts(name):
    """
    Finish still validates every prepared result, including after a measurement failure.
    """
    steps = PIPELINE["jobs"][name]["steps"]
    prepare = next(index for index, step in enumerate(steps) if step.get("id") == "prepare")
    assert "--ci-phase prepare" in steps[prepare]["run"]
    assert "selected_studies(root)" in steps[prepare + 1]["run"]
    finish = steps[prepare + 2]
    assert finish["if"] == "!cancelled() && steps.prepare.outcome == 'success'"
    assert "--ci-phase finish" in finish["run"] and "--publish" in finish["run"]
    artifact = steps[-1]
    assert artifact["if"] == "always()"
    assert artifact["with"]["include-hidden-files"] == "true"
    assert artifact["with"]["retention-days"] == "30"
    assert not any(step.get("uses", "").startswith("actions/download-artifact@") for step in steps)


@pytest.mark.parametrize("failed", [None, "first"])
def test_suite_runner_attempts_every_prepared_study_and_propagates_failure(tmp_path, monkeypatch, failed):
    """
    Exercise the workflow's shared loop without generating measurements or contacting a cluster.
    """
    from polyad_benchmarks import refresh

    attempted = []

    def study_phase(project, root, study, context):
        assert project == Path.cwd() and root == tmp_path and context == "test-context"
        attempted.append(study)
        if study == failed:
            raise RuntimeError("measurement failed")

    monkeypatch.setenv("STUDY_ROOT", str(tmp_path))
    monkeypatch.setenv("BENCHMARK_CONTEXT", "test-context")
    monkeypatch.setattr(refresh, "selected_studies", lambda root: ("first", "second", "newly-registered"))
    monkeypatch.setattr(refresh, "study_phase", study_phase)
    command = next(step["run"] for step in PIPELINE["jobs"]["studies"]["steps"] if "selected_studies(root)" in step.get("run", ""))
    script = command.split("<<'PY'\n", 1)[1].removesuffix("PY\n")
    if failed:
        with pytest.raises(SystemExit, match="Studies failed:.*first"):
            exec(compile(script, "<study-workflow>", "exec"), {})
    else:
        exec(compile(script, "<study-workflow>", "exec"), {})
    assert attempted == ["first", "second", "newly-registered"]


@pytest.mark.parametrize(
    "problem",
    [
        None,
        "local-only",
        "closed",
        "draft",
        "fork",
        "wrong-base-repo",
        "wrong-base",
        "default-branch",
        "stale-sha",
        "wrong-ref",
        "bad-number",
        "no-refresh",
        "release",
        "no-context",
    ],
)
def test_manual_refresh_requires_the_current_head_of_an_open_same_repository_pr(tmp_path, problem):
    """
    Reject stale, forked or unrelated requests before exposing a validated refresh output.
    """
    metadata = {
        "state": "open",
        "draft": False,
        "head": {"repo": {"full_name": "astrivant/polyad"}, "ref": "study-change", "sha": "a" * 40},
        "base": {"repo": {"full_name": "astrivant/polyad"}, "ref": "main"},
    }
    output = tmp_path / "output"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PR_METADATA": str(tmp_path / "pr.json"),
        "PR_NUMBER": "42",
        "GITHUB_REPOSITORY": "astrivant/polyad",
        "GITHUB_REF": "refs/heads/study-change",
        "SOURCE_SHA": "a" * 40,
        "DEFAULT_BRANCH": "main",
        "REFRESH": "true",
        "FULL_REFRESH": "true",
        "BENCHMARK_CONTEXT": "test-context",
        "RELEASE_TAG": "",
        "GITHUB_OUTPUT": str(output),
    }

    # Each mutation represents a request that must fail before study or cloud execution.
    if problem == "local-only":
        env["FULL_REFRESH"] = "false"
        env["BENCHMARK_CONTEXT"] = ""
    elif problem == "closed":
        metadata["state"] = "closed"
    elif problem == "draft":
        metadata["draft"] = True
    elif problem == "fork":
        metadata["head"]["repo"]["full_name"] = "fork/polyad"
    elif problem == "wrong-base-repo":
        metadata["base"]["repo"]["full_name"] = "another/polyad"
    elif problem == "wrong-base":
        metadata["base"]["ref"] = "release"
    elif problem == "default-branch":
        metadata["head"]["ref"] = "main"
        env["GITHUB_REF"] = "refs/heads/main"
    elif problem == "stale-sha":
        metadata["head"]["sha"] = "b" * 40
    elif problem == "wrong-ref":
        env["GITHUB_REF"] = "refs/heads/another-branch"
    elif problem == "bad-number":
        env["PR_NUMBER"] = "../42"
    elif problem == "no-refresh":
        env["REFRESH"] = "false"
    elif problem == "release":
        env["RELEASE_TAG"] = "v0.0.1-alpha99"
    elif problem == "no-context":
        env["BENCHMARK_CONTEXT"] = ""
    Path(env["PR_METADATA"]).write_text(json.dumps(metadata))
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\ncat "$PR_METADATA"\n')
    gh.chmod(0o755)
    command = PIPELINE["jobs"]["source"]["steps"][-1]["run"]
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], env=env, text=True, capture_output=True)
    allowed = problem in {None, "local-only"}
    assert (result.returncode == 0) is allowed, result.stdout + result.stderr
    if allowed:
        assert output.read_text() == "refresh=true\n"
    else:
        assert not output.exists()


def test_releases_cannot_be_cancelled_by_newer_pr_commits():
    """
    Match hypothesis-helm's PR-only cancellation while isolating explicitly selected tags.
    """
    assert PIPELINE["concurrency"] == {
        "group": "polyad-${{ github.event_name }}-${{ inputs.tag || github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    assert PIPELINE["defaults"]["run"]["shell"] == "bash"


def run_gate(results, *, release=False, refresh=False, full_refresh=False):
    """
    Execute the workflow's real aggregate check against synthetic job conclusions.
    """
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", PIPELINE["jobs"]["verified"]["steps"][0]["run"]],
        env={
            **os.environ,
            "RESULTS_JSON": json.dumps(results),
            "RELEASE_TAG": "v0.0.1-alpha99" if release else "",
            "REFRESH": str(refresh).lower(),
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
    result = run_gate(results, release=True, refresh=True, full_refresh=True)
    assert result.returncode != 0 and name in result.stderr


@pytest.mark.parametrize(
    "release,refresh,full_refresh",
    [(False, False, False), (True, False, False), (False, True, False), (False, True, True)],
)
def test_gate_accepts_only_the_skips_expected_for_the_selected_features(release, refresh, full_refresh):
    """
    Main, PR, tag and manual runs allow only their unselected optional branches to skip.
    """
    results = {job: {"result": "success"} for job in CHECKS}
    optional = set()
    if not release:
        optional.add("chart-package")
    if not refresh:
        optional.add("studies")
    if not full_refresh:
        optional.add("benchmark-refresh")
    for name in optional:
        results[name]["result"] = "skipped"
    result = run_gate(results, release=release, refresh=refresh, full_refresh=full_refresh)
    assert result.returncode == 0, result.stderr

    # Even an optional job may not fail silently if it did run.
    for name in optional:
        results[name]["result"] = "failure"
        assert run_gate(results, release=release, refresh=refresh, full_refresh=full_refresh).returncode != 0
        results[name]["result"] = "skipped"
    results["benchmark-smoke"]["result"] = "skipped"
    assert run_gate(results, release=release, refresh=refresh, full_refresh=full_refresh).returncode != 0


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
        ("refs/tags/experiment", "", ""),
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
