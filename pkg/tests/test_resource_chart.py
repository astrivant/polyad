"""
Exercise named Helm instances, typed references, defaults and independent CRD packaging.
"""

from __future__ import annotations

import json
import runpy
import subprocess
from pathlib import Path

import jsonschema
import pytest
import yaml
from deepdiff import DeepDiff

from polyad_schemas import resource_schema

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts/polyad-crds"
CHECKER = runpy.run_path(str(ROOT / "scripts/validation/check-values.py"))


def render(tmp_path, values, *, parent=False, include_crds=False, success=True):
    """
    Render an isolated values overlay, retaining failures for useful contract assertions.
    """
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump({"polyadResources": values} if parent else values))
    command = ["helm", "template", "test", str(ROOT / "charts/polyad" if parent else CHART), "-n", "apps", "-f", str(path)]
    if include_crds:
        command.append("--include-crds")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if not success:
        assert result.returncode, result.stdout
        return result.stderr
    assert result.returncode == 0, result.stderr
    return list(filter(None, yaml.safe_load_all(result.stdout)))


def test_cross_resource_references_and_crd_defaults(tmp_path):
    """
    Resolve names, defaulted fields, chained numeric inputs and native workload templates.
    """
    values = yaml.safe_load((ROOT / "pkg/tests/data/resource-chart-values.yaml").read_text())
    resources = render(tmp_path, values)
    by_kind = {item["kind"]: item for item in resources}
    assert len(resources) == 3
    assert by_kind["Daemon"]["spec"]["replicas"] == by_kind["ReplicaGroup"]["spec"]["replicas"] == 3
    assert by_kind["ReplicaGroup"]["spec"]["template"]["ref"] == "worker"
    assert by_kind["Graph"]["spec"]["nodes"][0] == {"name": "workers", "kind": "ReplicaGroup", "ref": "workers", "slots": 1}
    assert by_kind["Graph"]["spec"]["slots"] == 64
    for resource in resources:
        assert resource["metadata"]["namespace"] == "apps"
        jsonschema.validate(resource, resource_schema(resource["kind"]))
    inherited = [item for item in render(tmp_path, values, parent=True) if item["metadata"]["name"] in {"worker", "workers", "pipeline"}]
    assert not DeepDiff(resources, inherited, ignore_order=True)


def test_templates_preserve_false_zero_and_materialize_objects(tmp_path):
    """
    Do not replace explicit false/zero with defaults or create absent optional controllers.
    """
    values = {
        "variables": {"off": False, "zero": 0, "definition": {"kind": "Graph", "ref": "pipeline"}},
        "replicaGroups": {
            "copies": {
                "spec": {
                    "template": "{{ toJson .Values.variables.definition }}",
                    "replicas": "{{ .Values.variables.zero }}",
                    "inheritReplicas": "{{ .Values.variables.off }}",
                    "connectivity": {},
                }
            }
        },
    }
    resource = render(tmp_path, values)[0]
    assert resource["spec"]["replicas"] == 0
    assert resource["spec"]["inheritReplicas"] is False
    assert resource["spec"]["template"] == {"kind": "Graph", "ref": "pipeline"}
    assert resource["spec"]["connectivity"]["mode"] == "Independent"
    assert "throughput" not in resource["spec"]


@pytest.mark.parametrize("value,expected", [("true", "type integer"), ("-1", "below minimum"), ("257", "exceeds maximum")])
def test_resolved_integer_contracts_are_enforced(tmp_path, value, expected):
    """
    Reject rendered values that bypass input validation only because they are templates.
    """
    error = render(
        tmp_path,
        {
            "replicaGroups": {
                "copies": {
                    "spec": {
                        "template": {"kind": "Graph", "ref": "pipeline"},
                        "replicas": '{{ "' + value + '" }}',
                    }
                }
            }
        },
        success=False,
    )
    assert expected in error


@pytest.mark.parametrize(
    "values,expected",
    [
        ({"graphs": [{"name": "pipeline"}]}, "object"),
        ({"graphs": {"Bad Name": {"spec": {"nodes": []}}}}, "additional propert"),
        ({"graphs": {"pipeline": {"metadata": {"name": "other"}, "spec": {"nodes": []}}}}, "metadata.name must equal"),
        ({"graphs": {"pipeline": {"spec": {"nodes": [], "mode": '{{ "wrong" }}'}}}}, "allowed choice"),
        ({"graphs": {"pipeline": {"spec": '{{ dict "unknown" true | toJson }}'}}}, "is required"),
    ],
)
def test_invalid_named_instance_inputs_fail(tmp_path, values, expected):
    """
    Keep identity stable and validate typed fields after whole-object templating.
    """
    assert expected.lower() in render(tmp_path, values, success=False).lower()


def test_reference_cycles_and_pass_budget_fail_closed(tmp_path):
    """
    Bound template evaluation and reject cycles instead of emitting unresolved contracts.
    """
    values = {"variables": {"a": "{{ .Values.variables.b }}", "b": "{{ .Values.variables.a }}"}}
    assert "maxTplPasses" in render(tmp_path, values, success=False)
    assert "maxTplPasses" in render(tmp_path, {"maxTplPasses": 0}, success=False)


def test_defaults_are_empty_and_references_are_completely_commented_and_typed(tmp_path):
    """
    Every kind has an inert, complete reference with annotations derived from its schema.
    """
    catalog = json.loads((CHART / "files/resource-catalog.json").read_text())
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert len(catalog) == len(list((CHART / "crds").glob("*.yaml"))) == 17
    assert all(values[key] == {} for key in catalog)
    assert render(tmp_path, {}) == []
    for key in catalog:
        path = CHART / f"values-{key}.reference.yaml"
        CHECKER["validate"](path)
        assert yaml.safe_load(path.read_text()) == {key: {}}
        decoded = CHECKER["commented_example"](path.read_text())
        assert f"@param {key}.example.spec" in decoded
        assert "Required." in decoded
        assert "Optional." in decoded
    definitions = render(tmp_path, {}, include_crds=True)
    assert len(definitions) == 17
    assert {item["kind"] for item in definitions} == {"CustomResourceDefinition"}


def test_separate_crd_version_is_pinned_and_can_be_disabled(tmp_path):
    """
    Reuse exactly one definition source in the parent and support preinstalled APIs.
    """
    own = yaml.safe_load((CHART / "Chart.yaml").read_text())
    parent = yaml.safe_load((ROOT / "charts/polyad/Chart.yaml").read_text())
    dependency = next(item for item in parent["dependencies"] if item["name"] == "polyad-crds")
    assert dependency["alias"] == "polyadResources"
    assert dependency["version"] == own["version"]
    lock = yaml.safe_load((ROOT / "charts/polyad/Chart.lock").read_text())
    assert next(item for item in lock["dependencies"] if item["name"] == own["name"])["version"] == own["version"]
    assert not (ROOT / "charts/polyad/crds").exists()
    objects = render(tmp_path, {}, parent=True, include_crds=True)
    names = [item["metadata"]["name"] for item in objects if item["kind"] == "CustomResourceDefinition"]
    assert len(names) == len(set(names)) == 17
    objects = render(tmp_path, {"enabled": False}, parent=True, include_crds=True)
    assert not any(item["kind"] == "CustomResourceDefinition" for item in objects)


def test_partial_named_overlays_keep_type_coverage(tmp_path):
    """
    Allow partial instance objects to merge across files without losing field types.
    """
    source = "replicaGroups:\n  workers:\n    spec:\n      replicas: 4\n"
    path = tmp_path / "overlay.yaml"
    schema = json.loads((CHART / "values.schema.json").read_text())
    path.write_text(
        f"# yaml-language-server: $schema={CHART / 'values.reference.schema.json'}\n" + CHECKER["annotated_values"](source, schema)
    )
    CHECKER["validate"](path)


@pytest.mark.parametrize("port", [8080, "http", "{{ 8080 }}", '{{ "http" }}'])
def test_native_integer_or_string_and_nullable_fields(tmp_path, port):
    """
    Preserve CRD union types while resolving nullable object contracts and defaults.
    """
    values = {
        "dragonflies": {
            "cache": {
                "spec": {
                    "replicas": 1,
                    "additionalContainers": [
                        {"name": "sidecar", "image": "busybox:1.37.0", "readinessProbe": {"httpGet": {"port": port}}},
                    ],
                }
            }
        },
        "graphs": {
            "pipeline": {
                "spec": {
                    "nodes": [],
                    "throughput": {
                        "unit": "jobs",
                        "tiers": [{"threshold": 0, "cheeger": {"minimum": 0}}],
                        "demand": '{{ dict "name" "queueDepth" "unit" "jobs" | toJson }}',
                    },
                }
            }
        },
    }
    resources = render(tmp_path, values)
    cache = next(item for item in resources if item["kind"] == "Dragonfly")
    result = cache["spec"]["additionalContainers"][0]["readinessProbe"]["httpGet"]["port"]
    assert result == (8080 if "8080" in str(port) else "http")
    graph = next(item for item in resources if item["kind"] == "Graph")
    assert graph["spec"]["throughput"]["demand"] == {"name": "queueDepth", "unit": "jobs"}
    for resource in resources:
        group, version = resource["apiVersion"].split("/")
        jsonschema.validate(resource, resource_schema(resource["kind"], version, group=group))
