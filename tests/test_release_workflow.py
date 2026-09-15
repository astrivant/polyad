"""
Validate safe tag derivation and release gating on the complete CI result.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "tag,package,chart",
    [
        ("v0.0.1-alpha3", "0.0.1a3", "0.0.1-alpha3"),
        ("refs/tags/v2.4.0-beta.12", "2.4.0b12", "2.4.0-beta12"),
        ("v2.4.0rc2", "2.4.0rc2", "2.4.0-rc2"),
        ("v2.4.0-alpha", "2.4.0a0", "2.4.0-alpha0"),
        ("v2.4.0", "2.4.0", "2.4.0"),
    ],
)
def test_release_preparation_stamps_all_artifacts(tmp_path, tag, package, chart):
    """
    Apply the same release version before every artifact is built, without changing dependencies.
    """
    for name in (
        "pyproject.toml",
        "clients/python/pyproject.toml",
        "charts/polyad/Chart.yaml",
        "charts/polyad/values.yaml",
        "charts/polyad/README.md",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    project = tmp_path / "pyproject.toml"
    original = tomllib.loads(project.read_text())
    # Always exercise stamping a mismatched source version, even on a release checkout.
    project.write_text(project.read_text().replace(f'version = "{original["tool"]["poetry"]["version"]}"', 'version = "0.0.0"', 1))
    command = [sys.executable, str(ROOT / ".github/prepare-release.py"), "--tag", tag]
    subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, text=True)
    first = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, text=True)
    assert all(path.read_bytes() == content for path, content in first.items())
    actual = tomllib.loads(project.read_text())
    assert actual["tool"]["poetry"]["version"] == package
    assert tomllib.loads((tmp_path / "clients/python/pyproject.toml").read_text())["tool"]["poetry"]["version"] == package
    actual["tool"]["poetry"]["version"] = original["tool"]["poetry"]["version"]
    assert actual == original
    metadata = yaml.safe_load((tmp_path / "charts/polyad/Chart.yaml").read_text())
    assert metadata["version"] == metadata["appVersion"] == chart
    values = yaml.safe_load((tmp_path / "charts/polyad/values.yaml").read_text())
    assert values["operator"]["image"]["tag"] == chart
    row = next(
        line for line in (tmp_path / "charts/polyad/README.md").read_text().splitlines() if line.startswith("| `operator.image.tag`")
    )
    assert f"`{chart}`" in row
    subprocess.run(
        [sys.executable, str(ROOT / ".github/release-version.py"), "--tag", tag.removeprefix("refs/tags/")],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize("tag", ["v1.0.0;echo bad", "v1.2", "refs/heads/main", "v1.0.0\nversion=oops"])
def test_release_preparation_rejects_invalid_tags_without_writes(tmp_path, tag):
    """
    Reject malformed tag input before reading or changing project metadata.
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / ".github/prepare-release.py"), "--tag", tag], cwd=tmp_path, capture_output=True, check=False
    )
    assert result.returncode != 0
    assert not list(tmp_path.iterdir())


def test_every_release_build_prepares_metadata_before_consuming_it():
    """
    Stamp every independent checkout, including all chart shards and the artifact publisher.
    """
    for filename, jobs in {
        "ci.yml": ("python", "container", "operator"),
        "chart.yml": ("chart", "package"),
        "publish.yml": ("publish",),
    }.items():
        workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
        for job in jobs:
            steps = workflow["jobs"][job]["steps"]
            assert steps[0]["uses"] == "actions/checkout@v4"
            assert steps[1]["uses"] == "./.github/actions/prepare-release"
    action = yaml.load((ROOT / ".github/actions/prepare-release/action.yml").read_text(), Loader=yaml.BaseLoader)
    stamp = action["runs"]["steps"][-1]
    assert "refs/tags/" in stamp["if"]
    assert stamp["env"]["RELEASE_REF"] == "${{ inputs.ref }}"
    assert stamp["run"] == 'python .github/prepare-release.py --tag "$RELEASE_REF"'


@pytest.mark.parametrize(
    "version,tag",
    [
        ("0.1.0", "v0.1.0"),
        ("1.2.3", "v1.2.3"),
        ("0.0.1a1", "v0.0.1-alpha1"),
        ("0.0.1a2", "v0.0.1-alpha2"),
        ("0.0.1a3", "v0.0.1-alpha3"),
        ("1.2.3a1", "v1.2.3-alpha1"),
        ("1.2.3b2", "v1.2.3-beta2"),
        ("1.2.3rc3", "v1.2.3-rc3"),
    ],
)
def test_release_tag_preserves_package_version(tmp_path, version, tag):
    """
    Read package metadata and append exactly one output without overwriting prior values.
    """
    project = tmp_path / "pyproject.toml"
    project.write_text(f"[tool.poetry]\nversion = {json.dumps(version)}\n")
    output = tmp_path / "output"
    output.write_text("previous=value\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/release-tag.py"), "--project", str(project), "--output", str(output)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == tag
    assert output.read_text() == f"previous=value\ntag={tag}\n"


@pytest.mark.parametrize("version", ["", "v1.2.3", "01.2.3", "1.2", "1.2.3\ntag=malicious", "1.2.3/other", 123])
def test_invalid_versions_cannot_emit_tag_outputs(tmp_path, version):
    """
    Reject unsupported or multiline metadata before writing any workflow output.
    """
    project = tmp_path / "pyproject.toml"
    project.write_text(f"[tool.poetry]\nversion = {json.dumps(version)}\n")
    output = tmp_path / "output"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/release-tag.py"), "--project", str(project), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not output.exists()


def test_tagging_waits_for_all_checks_and_checks_out_the_tested_commit():
    """
    Gate writes on successful main pushes and retain read-only permissions elsewhere.
    """
    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    workflow = yaml.load((ROOT / ".github/workflows/tag.yml").read_text(), Loader=yaml.BaseLoader)
    assert "tag" not in ci["jobs"]
    assert ci["permissions"] == {"contents": "read"}
    trigger = workflow["on"]["workflow_run"]
    assert trigger == {"workflows": ["Test"], "types": ["completed"]}
    tag = workflow["jobs"]["tag"]
    assert "head_branch == 'main'" in tag["if"] and "conclusion == 'success'" in tag["if"]
    assert "workflow_run.event == 'push'" in tag["if"]
    assert tag["permissions"] == {"contents": "write"}
    assert tag["steps"][0]["with"] == {"ref": "${{ github.event.workflow_run.head_sha }}", "persist-credentials": "false"}
    assert "workflow_call" in ci["on"]


@pytest.mark.parametrize(
    "package,tag,normalized",
    [
        ("0.1.0", "v0.1.0", "0.1.0"),
        ("0.1.0", "v0.2.0", None),
        ("0.1.0", "not-a-tag", None),
        ("0.1.0", "v0.0.1-alpha1", None),
        ("0.0.1a1", "v0.0.1-alpha1", "0.0.1a1"),
        ("0.0.1a2", "v0.0.1-alpha2", "0.0.1a2"),
        ("0.0.1a3", "v0.0.1-alpha3", "0.0.1a3"),
        ("0.0.1a2", "v0.0.1-alpha3", None),
        ("0.0.1a1", "v0.0.1-alpha2", None),
        ("0.0.1a1", "v0.0.1-alpha.1", "0.0.1a1"),
        ("0.0.1a1", "v0.0.1a1", "0.0.1a1"),
        ("0.0.1a1", "v0.0.1", None),
    ],
)
def test_publishing_validates_the_package_tag(tmp_path, package, tag, normalized):
    """
    Normalize equivalent prerelease spellings while rejecting a different release before emitting outputs.
    """
    (tmp_path / "pyproject.toml").write_text(f"[tool.poetry]\nversion = {json.dumps(package)}\n")
    output = tmp_path / "outputs"
    result = subprocess.run(
        [sys.executable, str(ROOT / ".github/release-version.py"), "--tag", tag, "--output", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is (normalized is not None)
    if normalized is None:
        assert not output.exists()
        if package == "0.0.1a2" and tag == "v0.0.1-alpha3":
            assert "The tagged commit must declare 0.0.1a3 in pyproject.toml" in result.stderr
            assert "prepare-release.py --tag v0.0.1-alpha3" in result.stderr
    else:
        assert output.read_text() == f"version={normalized}\n"


def test_publication_downloads_verified_artifacts_and_uses_environment_credentials():
    """
    Gate publication on reusable CI and download its distributions without rebuilding.
    """
    workflow = yaml.load((ROOT / ".github/workflows/publish.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["jobs"]["verify"]["uses"] == "./.github/workflows/ci.yml"
    publish = workflow["jobs"]["publish"]
    assert publish["needs"] == "verify" and publish["environment"] == "pypi"
    assert any(step.get("uses", "").startswith("actions/download-artifact@") for step in publish["steps"])
    assert not any("poetry build" in step.get("run", "") for step in publish["steps"])
    assert publish["steps"][-1]["env"]["POETRY_PYPI_TOKEN_PYPI"] == "${{ secrets.PYPI_API_TOKEN }}"


def test_default_chart_action_is_sharded_and_gates_tagged_packaging():
    """
    Require every shard to pass before packaging the exact commit those shards validated.
    """
    workflow = yaml.load((ROOT / ".github/workflows/chart.yml").read_text(), Loader=yaml.BaseLoader)
    chart = workflow["jobs"]["chart"]
    assert chart["needs"] == "source"
    assert chart["strategy"] == {"fail-fast": "false", "matrix": {"shard": ["1", "2", "3"]}}
    action = next(step for step in chart["steps"] if step.get("uses") == "astrivant/hypothesis-helm@main")
    inputs = action["with"]
    assert inputs["chart"] == "charts/polyad"
    assert inputs["shard"] == "${{ matrix.shard }}/3" and inputs["jobs"] == "2"
    assert not {"sample-random", "max-examples", "rerun", "cache", "filter", "exhaustive"}.intersection(inputs)
    assert "match" not in inputs and "continue-on-error" not in action
    package = workflow["jobs"]["package"]
    assert package["needs"] == ["source", "chart"]
    assert package["if"] == "inputs.release-tag != ''"
    for job in [chart, package]:
        assert job["steps"][0]["with"]["ref"] == "${{ needs.source.outputs.sha }}"
    assert package["steps"][0]["with"]["fetch-depth"] == "0"
    build = next(step for step in package["steps"] if step.get("id") == "package")
    assert build["run"].index('git rev-parse "refs/tags/$tag^{commit}"') < build["run"].index("helm package")
    assert package["steps"][-1]["with"]["if-no-files-found"] == "error"


def test_main_and_automatic_tags_use_the_same_chart_gate():
    """
    Main must validate before tagging; token-created tags explicitly invoke their chart build.
    """
    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    tag = yaml.load((ROOT / ".github/workflows/tag.yml").read_text(), Loader=yaml.BaseLoader)
    assert ci["on"]["push"]["branches"] == ["main"]
    assert "pull_request" in ci["on"]
    assert ci["jobs"]["chart"]["uses"] == "./.github/workflows/chart.yml"
    assert "startsWith" in ci["jobs"]["chart"]["with"]["release-tag"]
    followup = tag["jobs"]["chart"]
    assert followup["needs"] == "tag"
    assert followup["if"] == "needs.tag.outputs.tag != ''"
    assert followup["uses"] == ci["jobs"]["chart"]["uses"]
    assert followup["with"] == {"ref": "${{ needs.tag.outputs.sha }}", "release-tag": "${{ needs.tag.outputs.tag }}"}
    create = next(step for step in tag["jobs"]["tag"]["steps"] if step.get("id") == "create")
    assert "existing.data.object.sha === process.env.TESTED_SHA" in create["with"]["script"]
