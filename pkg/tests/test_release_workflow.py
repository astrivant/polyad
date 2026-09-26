"""
Validate explicit version tags and release gating on the complete CI result.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]


def test_library_versions_and_internal_dependencies_are_aligned():
    """
    Reject source and lockfile drift before incompatible distributions are released.
    """
    projects = [ROOT / "pyproject.toml", *sorted((ROOT / "pkg").glob("*/pyproject.toml"))]
    metadata = {path: tomllib.loads(path.read_text()) for path in projects}
    version = metadata[ROOT / "pyproject.toml"]["project"]["version"]
    names = {value["project"]["name"] for value in metadata.values()}
    for path, value in metadata.items():
        project = value["project"]
        assert project["version"] == version, path
        dependencies = project.get("dependencies", []) + [
            dependency for group in project.get("optional-dependencies", {}).values() for dependency in group
        ]
        for dependency in map(Requirement, dependencies):
            if dependency.name in names:
                assert str(dependency.specifier) == f"=={version}", (path, dependency)
        lock_path = path.with_name("poetry.lock")
        if lock_path.exists():
            for package in tomllib.loads(lock_path.read_text())["package"]:
                if package["name"] in names:
                    assert package["version"] == version, (lock_path, package["name"])


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
    Stamp every distribution and its shared-model pin without changing third-party dependencies.
    """
    for name in (
        "pyproject.toml",
        "pkg/polyad-sdk/pyproject.toml",
        "pkg/polyad-types/pyproject.toml",
        "pkg/polyad-schemas/pyproject.toml",
        "pkg/polyad-benchmarks/pyproject.toml",
        "pkg/polyad-benchmarks/poetry.lock",
        "poetry.lock",
        "charts/polyad/Chart.yaml",
        "charts/polyad-crds/Chart.yaml",
        "charts/polyad-benchmarks/Chart.yaml",
        "charts/polyad-benchmarks/values.yaml",
        "charts/polyad-benchmarks/README.md",
        "charts/polyad/values.yaml",
        "charts/polyad/README.md",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    project = tmp_path / "pyproject.toml"
    original = tomllib.loads(project.read_text())

    # Always exercise stamping a mismatched source version, even on a release checkout.
    project.write_text(project.read_text().replace(f'version = "{original["project"]["version"]}"', 'version = "0.0.0"', 1))
    command = [sys.executable, str(ROOT / ".github/prepare-release.py"), "--tag", tag]
    subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, text=True)
    first = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, text=True)
    assert all(path.read_bytes() == content for path, content in first.items())
    actual = tomllib.loads(project.read_text())
    assert actual["project"]["version"] == package
    assert tomllib.loads((tmp_path / "pkg/polyad-sdk/pyproject.toml").read_text())["project"]["version"] == package
    assert tomllib.loads((tmp_path / "pkg/polyad-types/pyproject.toml").read_text())["project"]["version"] == package
    assert tomllib.loads((tmp_path / "pkg/polyad-schemas/pyproject.toml").read_text())["project"]["version"] == package
    benchmark = tomllib.loads((tmp_path / "pkg/polyad-benchmarks/pyproject.toml").read_text())["project"]
    assert benchmark["version"] == package
    assert f"polyad-sdk=={package}" in benchmark["dependencies"]
    assert f"polyad-types=={package}" in benchmark["dependencies"]
    assert actual["project"]["optional-dependencies"]["schemas"] == [f"polyad-schemas=={package}"]
    for filename in ("pyproject.toml", "pkg/polyad-sdk/pyproject.toml"):
        metadata = tomllib.loads((tmp_path / filename).read_text())
        assert f"polyad-types=={package}" in metadata["project"]["dependencies"]
    lock = tomllib.loads((tmp_path / "poetry.lock").read_text())
    assert next(item for item in lock["package"] if item["name"] == "polyad-types")["version"] == package
    assert next(item for item in lock["package"] if item["name"] == "polyad-schemas")["version"] == package
    actual["project"]["version"] = original["project"]["version"]
    actual["project"]["dependencies"] = [
        f"polyad-types=={original['project']['version']}" if item.startswith("polyad-types==") else item
        for item in actual["project"]["dependencies"]
    ]
    actual["project"]["optional-dependencies"]["schemas"] = original["project"]["optional-dependencies"]["schemas"]
    assert actual == original
    metadata = yaml.safe_load((tmp_path / "charts/polyad/Chart.yaml").read_text())
    assert metadata["version"] == metadata["appVersion"] == chart
    assert (tmp_path / "charts/polyad-crds/Chart.yaml").read_bytes() == (ROOT / "charts/polyad-crds/Chart.yaml").read_bytes()
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
    stamp = next(step for step in action["runs"]["steps"] if step.get("name") == "Stamp release metadata")
    assert "refs/tags/" in stamp["if"]
    assert stamp["env"]["RELEASE_REF"] == "${{ inputs.ref }}"
    assert stamp["run"] == 'python .github/prepare-release.py --tag "$RELEASE_REF"'
    refresh = action["runs"]["steps"][-1]
    assert refresh["if"] == f"inputs.refresh-locks == 'true' && ({stamp['if']})"
    assert action["inputs"]["refresh-locks"]["default"] == "true"
    assert "poetry lock\n" in refresh["run"] and "poetry check --lock" in refresh["run"]


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
    project.write_text(f"[project]\nversion = {json.dumps(version)}\n")
    output = tmp_path / "output"
    output.write_text("previous=value\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/release/release-tag.py"), "--project", str(project), "--output", str(output)],
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
    project.write_text(f"[project]\nversion = {json.dumps(version)}\n")
    output = tmp_path / "output"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/release/release-tag.py"), "--project", str(project), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not output.exists()


def test_only_explicit_tags_enable_releases_without_repository_write_permissions():
    """
    Main and PR builds must never create tags or upload packages as a side effect.
    """
    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert ci["permissions"] == {"contents": "read"}
    assert "tag" not in ci["jobs"]
    assert not (ROOT / ".github/workflows/tag.yml").exists()
    assert all(job.get("permissions", {}).get("contents", "read") == "read" for job in ci["jobs"].values())
    assert ci["jobs"]["publish"]["if"] == "needs.source.outputs.release-tag != ''"


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
    (tmp_path / "pyproject.toml").write_text(f"[project]\nversion = {json.dumps(package)}\n")
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


def test_publication_downloads_verified_artifacts_and_passes_credentials():
    """
    Gate publication inside the one pipeline and download its distributions without rebuilding.
    """
    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    workflow = yaml.load((ROOT / ".github/workflows/publish.yml").read_text(), Loader=yaml.BaseLoader)
    caller = ci["jobs"]["publish"]
    assert caller["needs"] == ["source", "verified", "python"]
    assert caller["if"] == "needs.source.outputs.release-tag != ''"
    assert caller["with"] == {"ref": "${{ needs.source.outputs.sha }}", "tag": "${{ needs.source.outputs.release-tag }}"}
    assert caller["uses"] == "./.github/workflows/publish.yml"
    assert caller["secrets"] == {"PYPI_API_TOKEN": "${{ secrets.PYPI_API_TOKEN }}"}
    assert set(workflow["on"]) == {"workflow_call"} and "verify" not in workflow["jobs"]
    assert workflow["on"]["workflow_call"]["secrets"]["PYPI_API_TOKEN"]["required"] == "false"
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    publish = workflow["jobs"]["publish"]
    assert publish["environment"] == "pypi"
    assert publish["steps"][0]["with"]["ref"] == "${{ inputs.ref }}"
    assert publish["steps"][1]["with"]["refresh-locks"] == "false"
    assert any(step.get("uses", "").startswith("actions/download-artifact@") for step in publish["steps"])
    assert not any("poetry build" in step.get("run", "") for step in publish["steps"])
    assert publish["steps"][-1]["env"]["POETRY_PYPI_TOKEN_PYPI"] == "${{ secrets.PYPI_API_TOKEN }}"
    upload = next(step for step in ci["jobs"]["python"]["steps"] if step.get("name") == "Upload verified distributions")
    assert upload["with"]["retention-days"] == "30"


def test_default_chart_action_is_sharded_and_gates_tagged_packaging():
    """
    Require every shard to pass before packaging the exact commit those shards validated.
    """
    workflow = yaml.load((ROOT / ".github/workflows/chart.yml").read_text(), Loader=yaml.BaseLoader)
    chart = workflow["jobs"]["chart"]
    assert chart["needs"] == "source"
    assert chart["runs-on"] == "ubuntu-24.04"
    assert chart["strategy"] == {
        "fail-fast": "false",
        "matrix": {
            "chart": ["polyad", "polyad-crds"],
            "shard": ["1", "2", "3"],
            "include": [{"chart": "polyad-benchmarks", "shard": "1"}],
        },
    }
    action = next(step for step in chart["steps"] if step.get("uses", "").startswith("astrivant/hypothesis-helm@"))
    assert action["uses"] == "astrivant/hypothesis-helm@05681f04b41256a41355e6320e52a7ca17f67b74"
    inputs = action["with"]
    assert inputs["chart"] == "charts/${{ matrix.chart }}"
    assert inputs["artifact-name"] == "hypothesis-helm-${{ matrix.chart }}"
    assert inputs["artifact-dir"] == "reports/hypothesis-helm/${{ matrix.chart }}"
    assert inputs["shard"] == "${{ matrix.shard }}/3" and inputs["jobs"] == "4"
    assert inputs["kubeconform"] == "true" and inputs["schema-version"] == "1.35.0"
    assert inputs["kubeconform-binary"] == "scripts/validation/kubeconform.sh"
    assert not {"sample-random", "max-examples", "rerun", "cache", "filter", "exhaustive"}.intersection(inputs)
    assert "match" not in inputs and "continue-on-error" not in action
    policy = next(step for step in chart["steps"] if step.get("run", "").startswith("cp charts/polyad-crds/"))
    assert policy["if"] == "matrix.chart == 'polyad-crds'"
    assert chart["steps"].index(policy) < chart["steps"].index(action)
    assert yaml.safe_load((ROOT / "charts/polyad-crds/.hypothesis-helm.yaml").read_text()) == {"ignored": ["HH1107"]}
    package = workflow["jobs"]["package"]
    assert package["needs"] == ["source", "chart"]
    assert package["if"] == "inputs.release-tag != ''"
    for job in [chart, package]:
        assert job["steps"][0]["with"]["ref"] == "${{ needs.source.outputs.sha }}"
    assert package["steps"][0]["with"]["fetch-depth"] == "0"
    build = next(step for step in package["steps"] if step.get("id") == "package")
    assert build["run"].index('git rev-parse "refs/tags/$tag^{commit}"') < build["run"].index("helm package")
    assert "helm package charts/polyad-crds --destination dist/chart" in build["run"]
    assert package["steps"][-1]["with"]["if-no-files-found"] == "error"


def test_explicit_tags_use_the_same_chart_gate_and_source():
    """
    Tagged chart builds and publication remain inside the same verified parent run.
    """
    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert ci["on"]["push"]["branches"] == ["main"]
    assert "pull_request" in ci["on"]
    assert ci["jobs"]["chart"]["uses"] == "./.github/workflows/chart.yml"
    assert ci["jobs"]["chart"]["with"]["release-tag"] == "${{ needs.source.outputs.release-tag }}"
    assert ci["jobs"]["chart"]["with"]["ref"] == "${{ needs.source.outputs.sha }}"
    assert ci["jobs"]["publish"]["with"]["tag"] == "${{ needs.source.outputs.release-tag }}"
    assert "chart" in ci["jobs"]["verified"]["needs"]


@pytest.mark.parametrize("problem", [None, "missing-token", "missing-wheel", "missing-sdist"])
def test_publish_checks_all_five_artifact_pairs_before_any_upload(tmp_path, problem):
    """
    Reject incomplete releases up front and publish matching packages in dependency order.
    """
    version = "0.0.1a99"
    packages = ("polyad_types", "polyad_schemas", "polyad_sdk", "polyad_benchmarks", "polyad")
    distributions = tmp_path / "dist"
    distributions.mkdir()
    for package in packages:
        for suffix in ("-py3-none-any.whl", ".tar.gz"):
            (distributions / f"{package}-{version}{suffix}").write_text("verified artifact")

    # A missing final package must fail before earlier packages can be uploaded.
    if problem == "missing-wheel":
        (distributions / f"polyad-{version}-py3-none-any.whl").unlink()
    if problem == "missing-sdist":
        (distributions / f"polyad-{version}.tar.gz").unlink()
    poetry = tmp_path / "poetry"
    poetry.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PUBLISH_LOG"\n')
    poetry.chmod(0o755)
    log = tmp_path / "uploads"
    workflow = yaml.load((ROOT / ".github/workflows/publish.yml").read_text(), Loader=yaml.BaseLoader)
    command = workflow["jobs"]["publish"]["steps"][-1]["run"]
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", command],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "POETRY_PYPI_TOKEN_PYPI": "" if problem == "missing-token" else "test-token-not-sent",
            "PACKAGE_VERSION": version,
            "PUBLISH_LOG": str(log),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is (problem is None), result.stderr
    if problem:
        assert not log.exists()
    else:
        assert log.read_text().splitlines() == [
            *(f"-C pkg/{name.replace('_', '-')} publish --dist-dir ../../dist --no-interaction" for name in packages[:-1]),
            "publish --no-interaction",
        ]
