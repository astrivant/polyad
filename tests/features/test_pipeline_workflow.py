"""
Keep all checks and release stages inside one source-pinned GitHub Actions run.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.workflows import BUILD, JOBS, MEASURE, PIPELINE, TEST, WORKFLOWS

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / ".github/workflows"
GATES = ("verified", "test-complete", "build-complete", "measure-complete")
CHECKS = {name: set(JOBS[name]["needs"]) for name in GATES}


def test_only_the_entry_workflow_has_triggers_and_stages_remain_in_the_same_run():
    """
    Split implementation files without introducing independent pipelines or mutable stage refs.
    """
    assert set(WORKFLOWS) == {"ci.yml", "stage-test.yml", "stage-build.yml", "stage-measure.yml", "stage-deploy.yml"}
    assert set(PIPELINE["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert PIPELINE["on"]["push"] == {"branches": ["**"], "tags": ["**"]}
    assert len(JOBS) == sum(len(workflow["jobs"]) for workflow in WORKFLOWS.values())

    for name in ("test", "build", "measure", "deploy"):
        workflow = WORKFLOWS[f"stage-{name}.yml"]
        call = JOBS[f"{name}-stage"]
        assert set(workflow["on"]) == {"workflow_call"}
        assert call["uses"] == f"./.github/workflows/stage-{name}.yml"
        assert call["with"]["sha"] == "${{ needs.source.outputs.sha }}"
        assert set(call["with"]) <= set(workflow["on"]["workflow_call"]["inputs"])
        assert workflow["on"]["workflow_call"]["inputs"]["sha"]["required"] == "true"
        assert workflow["permissions"] == {"contents": "read"}
        assert workflow["defaults"]["run"]["shell"] == "bash"

    # Dependencies must stay within their file; reusable calls form the parent graph.
    def visit(jobs, name, ancestors=()):
        assert name not in ancestors and name in jobs
        dependencies = jobs[name].get("needs", [])
        for dependency in [dependencies] if isinstance(dependencies, str) else dependencies:
            visit(jobs, dependency, (*ancestors, name))

    for workflow in WORKFLOWS.values():
        jobs = workflow["jobs"]
        for name, job in jobs.items():
            visit(jobs, name)
            if "uses" in job:
                assert "runs-on" not in job and "steps" not in job
            else:
                assert "runs-on" in job and "steps" in job
                for step in job["steps"]:
                    action = step.get("uses", "")
                    if action.startswith("./"):
                        assert (ROOT / action / "action.yml").is_file()


def test_all_validation_branches_join_before_publishing():
    """
    Require complete stage results, while allowing Test and Build to start in parallel.
    """
    assert CHECKS["verified"] == {"source", "test-stage", "build-stage"}
    for name in ("test-stage", "build-stage"):
        assert JOBS[name]["needs"] == "source" and "if" not in JOBS[name]
    for workflow, gate in ((TEST, "test-complete"), (BUILD, "build-complete"), (MEASURE, "measure-complete")):
        assert CHECKS[gate] == set(workflow["jobs"]) - {gate}
    assert all(JOBS[name]["if"] == "always()" for name in GATES)
    for name in ("measure-stage", "deploy-stage"):
        assert JOBS[name]["needs"] == ["source", "verified"]
        assert "!cancelled() && needs.verified.result == 'success'" in JOBS[name]["if"]
    assert "coverage-badge" not in set().union(*CHECKS.values())
    assert "secrets" not in JOBS["test-stage"] and "secrets" not in JOBS["build-stage"]
    assert "secrets" not in JOBS["measure-stage"]


def test_all_checkouts_use_the_resolved_source():
    """
    Pass the selected immutable source through every reusable workflow boundary.
    """
    for filename, workflow in WORKFLOWS.items():
        for name, job in workflow["jobs"].items():
            if name == "source":
                continue
            checkouts = [step for step in job.get("steps", []) if step.get("uses") == "actions/checkout@v4"]
            for checkout in checkouts:
                expected = "${{ needs.source.outputs.sha }}" if filename == "ci.yml" else "${{ inputs.sha }}"
                assert checkout["with"]["ref"] == expected, (filename, name)


def test_manual_cloud_benchmarks_remain_explicit_and_environment_protected():
    """
    Reusable workflows must not grant ordinary pushes or pull requests access to the cloud runner.
    """
    inputs = PIPELINE["on"]["workflow_dispatch"]["inputs"]
    assert inputs["full-refresh"]["default"] == "false" and inputs["tag"]["default"] == ""
    assert inputs["refresh"]["default"] == "false" and inputs["pull-request"]["default"] == ""
    assert JOBS["measure-stage"]["if"] == ("!cancelled() && needs.verified.result == 'success' && needs.source.outputs.refresh == 'true'")
    assert JOBS["measure-stage"]["with"]["full-refresh"] == "${{ inputs.full-refresh || false }}"
    assert JOBS["measure-stage"]["with"]["context"] == "${{ inputs.context || '' }}"
    study = JOBS["benchmark-refresh"]
    assert study["if"] == "inputs.full-refresh"
    assert study["environment"] == "benchmarks"
    assert "needs" not in study
    assert study["env"]["BENCHMARK_CONTEXT"] == "${{ inputs.context }}"
    assert study["runs-on"] == "${{ vars.POLYAD_BENCHMARK_RUNNER || 'polyad-benchmarks' }}"
    source = JOBS["source"]
    request = source["steps"][-1]
    assert request["if"] == "github.event_name == 'workflow_dispatch' && (inputs.refresh || inputs.full-refresh)"
    assert source["outputs"]["refresh"] == "${{ steps.request.outputs.refresh }}"
    assert source["permissions"] == {"contents": "read", "pull-requests": "read"}


def test_studies_share_one_optional_matrix_and_validation_tests_run_on_every_ref():
    """
    Preparation and collection are steps, not extra job columns or mandatory benchmark runs.
    """
    study = JOBS["studies"]
    assert "needs" not in study and "if" not in study
    assert study["strategy"]["fail-fast"] == "false"
    assert {row["suite"] for row in study["strategy"]["matrix"]["include"]} == {"cheeger", "reachability", "process"}
    assert not any(name.endswith(("-prepare", "-study", "-finish")) for name in JOBS)
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
        assert "if" not in JOBS[name], name
    assert JOBS["reachability-sdk"]["strategy"]["matrix"]["python"] == ["3.11", "3.12", "3.13", "3.14"]
    assert JOBS["process-tests"]["strategy"]["matrix"]["python"] == ["3.13", "3.14"]


@pytest.mark.parametrize("name", ["studies", "benchmark-refresh"])
def test_consolidated_studies_keep_completion_checks_and_failure_artifacts(name):
    """
    Finish still validates every prepared result, including after a measurement failure.
    """
    steps = JOBS[name]["steps"]
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
    command = next(step["run"] for step in JOBS["studies"]["steps"] if "selected_studies(root)" in step.get("run", ""))
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
    command = JOBS["source"]["steps"][-1]["run"]
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


def run_gate(gate, results, *, release=False, full_refresh=False):
    """
    Execute each workflow's real gate against synthetic job conclusions.
    """
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", JOBS[gate]["steps"][0]["run"]],
        env={
            **os.environ,
            "RESULTS_JSON": json.dumps(results),
            "RELEASE_TAG": "v0.0.1-alpha99" if release else "",
            "FULL_REFRESH": str(full_refresh).lower(),
        },
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize("gate,name", [(gate, name) for gate in GATES for name in sorted(CHECKS[gate])])
@pytest.mark.parametrize("status", ["failure", "cancelled", "skipped"])
def test_gate_rejects_every_incomplete_job_when_all_features_are_required(gate, name, status):
    """
    A failed, cancelled or unexpectedly skipped suite must never permit downstream execution.
    """
    results = {job: {"result": "success"} for job in CHECKS[gate]}
    results[name]["result"] = status
    result = run_gate(gate, results, release=True, full_refresh=True)
    assert result.returncode != 0 and name in result.stderr


@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("release,full_refresh", [(False, False), (True, False), (False, True)])
def test_gate_accepts_only_the_skips_expected_for_the_selected_features(gate, release, full_refresh):
    """
    Only unselected chart packaging and cloud measurements may skip inside their stages.
    """
    results = {job: {"result": "success"} for job in CHECKS[gate]}
    optional = set()
    if gate == "build-complete" and not release:
        optional.add("chart-package")
    if gate == "measure-complete" and not full_refresh:
        optional.add("benchmark-refresh")
    for name in optional:
        results[name]["result"] = "skipped"
    result = run_gate(gate, results, release=release, full_refresh=full_refresh)
    assert result.returncode == 0, result.stderr

    # An optional job still fails the stage if it actually ran and failed.
    for name in optional:
        results[name]["result"] = "failure"
        assert run_gate(gate, results, release=release, full_refresh=full_refresh).returncode != 0
        results[name]["result"] = "skipped"


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
        ["bash", "-e", "-o", "pipefail", "-c", JOBS["source"]["steps"][1]["run"]],
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
