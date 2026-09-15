"""
Verify Flux customization generation covers the registry without overriding native health.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from polyad.compiler.registry import POLYAD_KINDS, RESOURCE_TYPES

ROOT = Path(__file__).resolve().parents[1]


def test_flux_configuration_covers_registry():
    """
    Emit the same spec fragment in YAML and JSON for every supported Polyad kind.
    """
    command = [sys.executable, str(ROOT / "scripts/flux-health.py")]
    yaml_document = yaml.safe_load(subprocess.check_output(command, text=True))
    json_document = json.loads(subprocess.check_output([*command, "--json"], text=True))
    assert yaml_document == json_document
    assert set(json_document) == {"spec"}
    checks = json_document["spec"]["healthCheckExprs"]
    assert len(checks) == len(POLYAD_KINDS)
    assert {item["kind"] for item in checks} == POLYAD_KINDS
    for item in checks:
        assert item["apiVersion"] == RESOURCE_TYPES[item["kind"]].api_version
        assert set(item) == {"apiVersion", "kind", "inProgress", "failed", "current"}
    cases = json.loads((ROOT / "tests/flux/cases.json").read_text())
    assert {case["object"]["kind"] for case in cases} == POLYAD_KINDS
