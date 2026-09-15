"""
Validate safe tag derivation and release gating on the complete CI result.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("version", ["0.1.0", "1.2.3", "1.2.3a1", "1.2.3b2", "1.2.3rc3"])
def test_release_tag_preserves_package_version(tmp_path, version):
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
    assert result.stdout.strip() == f"v{version}"
    assert output.read_text() == f"previous=value\ntag=v{version}\n"


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


@pytest.mark.parametrize("tag,valid", [("v0.1.0", True), ("v0.2.0", False), ("not-a-tag", False)])
def test_publishing_validates_the_package_tag(tag, valid):
    """
    Reject publication when the tag does not identify the checkout's package version.
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / ".github/release-version.py"), "--tag", tag], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert (result.returncode == 0) is valid


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
