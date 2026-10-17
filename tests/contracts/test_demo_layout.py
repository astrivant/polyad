"""
Keep relocated demonstrations executable and shared study imports canonical.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from demo import nature, soul
from polyad_benchmarks.refresh import sources

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["soul", "nature"])
@pytest.mark.parametrize("invocation", ["relative", "absolute", "module"])
def test_relocated_demo_cli_help(name, invocation, tmp_path):
    """
    Support repository commands, absolute paths from another directory and module execution.
    """
    if invocation == "relative":
        arguments, directory = [f"demo/{name}.py"], ROOT
    elif invocation == "absolute":
        arguments, directory = [str(ROOT / "demo" / f"{name}.py")], tmp_path
    else:
        arguments, directory = ["-m", f"demo.{name}"], ROOT

    # Do not let a caller's repository PYTHONPATH hide a broken script bootstrap.
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, *arguments, "--help"], cwd=directory, env=environment, text=True, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--jobs" in result.stdout


def test_nature_uses_the_same_soul_module_as_the_studies():
    """
    Prevent duplicate planner types or globals under an obsolete top-level import name.
    """
    from polyad_benchmarks.studies.soul.runtime import service

    assert nature.soul is soul
    assert service.nature is nature
    assert service.soul is soul
    assert not (ROOT / "soul.py").exists()
    assert not (ROOT / "nature.py").exists()


def test_refresh_fingerprints_relocated_demo_sources(tmp_path):
    """
    Invalidate prepared study inputs when either demo or their package initializer changes.
    """
    directory = tmp_path / "demo"
    directory.mkdir()
    for name in ("__init__.py", "soul.py", "nature.py"):
        (directory / name).write_text("# demo source\n")
    previous = sources(tmp_path)
    assert set(previous) == {"demo/__init__.py", "demo/soul.py", "demo/nature.py"}
    for name in ("__init__.py", "soul.py", "nature.py"):
        (directory / name).write_text("# updated demo source\n")
        current = sources(tmp_path)
        assert current[f"demo/{name}"] != previous[f"demo/{name}"]
        previous = current
