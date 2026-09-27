"""
Read the entry workflow and its called stages without losing their dependency boundaries.
"""

from __future__ import annotations

from pathlib import Path

import yaml

__all__ = ("BUILD", "DEPLOY", "JOBS", "MEASURE", "PIPELINE", "TEST", "WORKFLOWS")

_DIRECTORY = Path(__file__).resolve().parents[2] / ".github/workflows"
WORKFLOWS = {path.name: yaml.load(path.read_text(), Loader=yaml.BaseLoader) for path in _DIRECTORY.glob("*.yml")}
PIPELINE = WORKFLOWS["ci.yml"]
TEST = WORKFLOWS["stage-test.yml"]
BUILD = WORKFLOWS["stage-build.yml"]
MEASURE = WORKFLOWS["stage-measure.yml"]
DEPLOY = WORKFLOWS["stage-deploy.yml"]

# Job IDs are deliberately unique across files; graph tests verify this invariant.
JOBS = {name: job for workflow in WORKFLOWS.values() for name, job in workflow["jobs"].items()}
