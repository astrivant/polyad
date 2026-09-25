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

ROOT = Path(__file__).resolve().parents[2]


def test_flux_configuration_covers_registry():
    """
    Emit the same spec fragment in YAML and JSON for every supported Polyad kind.
    """
    command = [sys.executable, str(ROOT / "scripts/gitops/flux-health.py")]
    yaml_document = yaml.safe_load(subprocess.check_output(command, text=True))
    json_document = json.loads(subprocess.check_output([*command, "--json"], text=True))

    # Both output formats must carry the same complete per-kind health contract.
    assert yaml_document == json_document
    assert set(json_document) == {"spec"}
    checks = json_document["spec"]["healthCheckExprs"]
    assert len(checks) == len(POLYAD_KINDS)
    assert {item["kind"] for item in checks} == POLYAD_KINDS
    for item in checks:
        assert item["apiVersion"] == RESOURCE_TYPES[item["kind"]].api_version
        assert set(item) == {"apiVersion", "kind", "inProgress", "failed", "current"}

        # GraphPolicy has no status schema or publisher; checking it would leave
        # an inert policy waiting forever on a CEL variable that never exists.
        if item["kind"] == "GraphPolicy":
            assert item["inProgress"] == "has(metadata.deletionTimestamp)"
            assert item["current"] == "true"
            assert item["failed"] == "false"
        else:
            assert "status.progressing" in item["inProgress"]
            crd = yaml.safe_load((ROOT / "charts/polyad-crds/crds" / f"{RESOURCE_TYPES[item['kind']].plural}.yaml").read_text())
            status = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]
            assert status["default"] == {}
    cases = json.loads((ROOT / "pkg/tests/flux/cases.json").read_text())
    assert {case["object"]["kind"] for case in cases} == POLYAD_KINDS
