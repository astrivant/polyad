"""
Keep clean-runner Helm, Python and container prerequisites complete before release.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.test_runtime_capabilities import probe

ROOT = Path(__file__).resolve().parents[2]
HELPER = "scripts/tooling/build-chart-dependencies.sh"


def build_dependencies(*charts, fail=""):
    """
    Record the real helper's Helm calls without network access or repository writes.
    """
    result = subprocess.run(
        [
            "bash",
            "-c",
            'helm() { printf "%s\\n" "$*"; [[ -z "$HELM_TEST_FAIL" || " $* " != *" $HELM_TEST_FAIL "* ]]; }; export -f helm; bash "$@"',
            "helm-dependency-test",
            str(ROOT / HELPER),
            *charts,
        ],
        env={**os.environ, "HELM_TEST_FAIL": fail},
        capture_output=True,
        text=True,
    )
    return result, [shlex.split(line) for line in result.stdout.splitlines()]


@pytest.mark.parametrize("charts", [(), ("charts/polyad",), ("charts/polyad-benchmarks",)])
def test_repository_setup_covers_all_declared_and_locked_dependencies(charts):
    """
    Register all HTTP repositories before the first locked build, including disabled features.
    """
    result, calls = build_dependencies(*charts)
    assert result.returncode == 0, result.stderr
    repositories = [call for call in calls if call[:2] == ["repo", "add"]]
    expected = {
        dependency["repository"]
        for pattern in ("*/Chart.yaml", "*/Chart.lock")
        for path in (ROOT / "charts").glob(pattern)
        for dependency in yaml.safe_load(path.read_text()).get("dependencies", [])
        if dependency["repository"].startswith(("https://", "http://"))
    }
    assert {call[3] for call in repositories} == expected
    assert len({call[2] for call in repositories}) == len(repositories) == len(expected)
    assert all(call[4:] == ["--force-update"] for call in repositories)

    # The script must bootstrap repositories before Helm consumes a lockfile.
    targets = charts or ("charts/polyad", "charts/polyad-benchmarks")
    assert calls == repositories + [["dependency", "build", chart, "--skip-refresh"] for chart in targets]


@pytest.mark.parametrize("fail", ["kedacore", "charts/polyad"])
def test_dependency_failures_stop_later_builds(fail):
    """
    A failed repository registration or chart build cannot leave a successful CI step.
    """
    result, calls = build_dependencies(fail=fail)
    assert result.returncode != 0
    assert fail in calls[-1]
    assert not any(call[:3] == ["dependency", "build", "charts/polyad-benchmarks"] for call in calls)


def test_dependency_build_requires_a_lockfile(tmp_path):
    """
    Reject an unlocked chart instead of silently resolving new versions during release.
    """
    result, calls = build_dependencies(str(tmp_path))
    assert result.returncode == 2
    assert "Missing committed dependency lock" in result.stderr
    assert not any(call[0] == "dependency" for call in calls)
    assert not (tmp_path / "Chart.lock").exists()


def test_ci_and_release_use_one_repository_bootstrap():
    """
    Cover every chart-consuming job and reject new inline dependency builds.
    """
    consumers = set()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        for job, config in workflow["jobs"].items():
            for step in config.get("steps", []):
                command = step.get("run", "")
                assert "helm dependency build" not in command, (path.name, job)
                assert "helm dependency update" not in command, (path.name, job)
                if HELPER in command:
                    consumers.add((path.name, job))
    assert consumers == {
        ("ci.yml", "python"),
        ("ci.yml", "compose"),
        ("ci.yml", "operator"),
        ("chart.yml", "chart"),
        ("chart.yml", "package"),
        ("benchmarks.yml", "smoke"),
    }


def test_plot_tests_have_operator_dependencies_and_child_process_import_paths():
    """
    Plot experiments use the production solver, while child interpreters cannot inherit pytest's sys.path.
    """
    for filename, job in (("ci.yml", "python"), ("benchmarks.yml", "smoke")):
        workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
        config = workflow["jobs"][job]
        assert "pkg/polyad-benchmarks" in config["env"]["PYTHONPATH"].split(":")
        commands = [step.get("run", "") for step in config["steps"]]
        assert any("poetry sync --all-extras" in command or "poetry install --all-extras" in command for command in commands)
        assert not any("poetry --project pkg/polyad-benchmarks run python -m pytest" in command for command in commands)


def test_mermaid_dependencies_are_installed_for_every_python_version():
    """
    An npm download cache or a matrix-specific pre-commit run cannot supply node_modules.
    """
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["python"]["steps"]
    install = next(index for index, step in enumerate(steps) if "npm ci --prefix scripts/validation/mermaid" in step.get("run", ""))
    test = next(index for index, step in enumerate(steps) if "npm test --prefix scripts/validation/mermaid" in step.get("run", ""))
    assert install < test
    assert "if" not in steps[install]
    assert "--ignore-scripts" in steps[install]["run"]


@pytest.mark.parametrize("chart", ["polyad", "polyad-crds", "polyad-benchmarks"])
def test_chart_packages_keep_runtime_inputs_but_omit_duplicate_validator_schemas(chart, tmp_path):
    """
    Exclude source-tree CI copies from Helm's size-limited release without stripping customer references.
    """
    source = ROOT / "charts" / chart
    subprocess.run(["helm", "package", str(source), "--destination", str(tmp_path)], check=True, capture_output=True, text=True)
    with tarfile.open(next(tmp_path.glob("*.tgz"))) as archive:
        members = set(archive.getnames())
    assert f"{chart}/values.schema.json" in members
    assert not any(name.startswith(f"{chart}/schemas/") for name in members)

    # Files consumed by templates and documented values overlays stay distributable.
    for pattern in ("files/**/*", "plans/*", "references/*", "values-*.reference.yaml", "crds/*.yaml"):
        for path in source.glob(pattern):
            if path.is_file():
                assert f"{chart}/{path.relative_to(source)}" in members, path


def test_development_image_copies_local_dependencies_before_installation():
    """
    Every Poetry path dependency must exist before the development layer installs it.
    """
    dockerfile = (ROOT / "services/operator/Dockerfile").read_text()
    base = dockerfile.split("FROM build-tools AS production-build")[0]
    development = dockerfile.split("FROM build-tools AS development")[1]
    before_install = base + development.split("poetry sync --with dev")[0]
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["poetry"]
    dependencies = [*metadata["dependencies"].values(), *metadata["group"]["dev"]["dependencies"].values()]
    for dependency in dependencies:
        if isinstance(dependency, dict) and "path" in dependency:
            path = dependency["path"]
            assert f"COPY {path} ./{path}\n" in before_install, path


def test_container_optional_import_check_allows_dependency_capability_probes():
    """
    Exercise the exact image check without Docker, preserving lazy application encryption.
    """
    result = probe(
        {},
        code="import json, runpy; checks = runpy.run_path('scripts/testing/check-container.py'); "
        "checks['check_optional_imports'](); print(json.dumps(True))",
    )
    assert result is True


def test_container_optional_import_check_still_rejects_active_encryption():
    """
    Accept a Redis capability probe but fail when Polyad actually loads record encryption.
    """
    result = probe(
        {},
        code="""
import json, runpy
checks = runpy.run_path('scripts/testing/check-container.py')
import polyad.sql.encryption
try:
    checks['check_optional_imports']()
except AssertionError as error:
    print(json.dumps(str(error)))
else:
    raise AssertionError('record encryption was not detected')
""",
    )
    assert "polyad.sql.encryption" in result
