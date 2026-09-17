"""
Verify importable JSON Schemas against serialized models, manifests and Helm overlays.
"""

from __future__ import annotations

import json
import runpy
import sys
import tomllib
from importlib.resources import files
from pathlib import Path

import jsonschema
import pytest
import yaml
from deepdiff import DeepDiff

from polyad_schemas import available_schemas, event_schema, load_schema, resource_schema, schema_for
from polyad_types import (
    CheegerComputation,
    ConnectionRequest,
    Graph,
    GraphMetrics,
    NetworkPort,
    ObjectMeta,
    ReplicaConnectivity,
    ReplicaTemplate,
    Replication,
    to_dict,
    to_document,
)
from polyad_types.discovery import ServiceAccess
from polyad_types.event_codec import decode_event
from polyad_types.resources import ConfigMap
from polyad_types.topology import GraphNode, PolyGraph

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts/schemas/generate-json-schemas.py"


@pytest.mark.parametrize("name", available_schemas())
def test_all_artifacts_have_valid_dialects_and_only_resolvable_local_references(name):
    """
    Ensure validators can resolve every bundled reference without fetching a URL.
    """
    schema = load_schema(name)
    jsonschema.validators.validator_for(schema).check_schema(schema)

    def check(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                assert ref.startswith("#/"), ref
                target = schema
                for segment in ref[2:].split("/"):
                    key = segment.replace("~1", "/").replace("~0", "~")
                    target = target[int(key)] if isinstance(target, list) else target[key]
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)


def test_generated_artifacts_match_sources():
    """
    Detect artifact drift from public models, CRDs and canonical chart values.
    """
    generated = runpy.run_path(str(GENERATOR))["artifacts"]()
    assert set(available_schemas()) == {*generated, "events"}
    for name, schema in generated.items():
        assert not DeepDiff(schema, load_schema(name)), name


def test_chart_and_package_outputs_share_the_same_contracts_and_licenses():
    """
    Prevent either generated distribution from acquiring independently maintained constraints.
    """
    generator = runpy.run_path(str(GENERATOR))
    outputs = generator["outputs"]()
    chart_paths = set((ROOT / "charts/polyad/schemas").glob("*.json"))
    assert chart_paths == {path for path in outputs if path.parent == ROOT / "charts/polyad/schemas" and path.suffix == ".json"}
    for path, expected in outputs.items():
        assert path.read_text() == expected, path
        if path in chart_paths:
            chart = json.loads(expected)
            jsonschema.Draft7Validator.check_schema(chart)
            group, version = chart["properties"]["apiVersion"]["const"].split("/")
            kind = chart["properties"]["kind"]["const"]
            package = resource_schema(kind, version, group=group)
            assert not DeepDiff({**package, "$schema": chart["$schema"]}, chart)


def test_catalog_checks_detect_dependency_and_snapshot_drift(tmp_path, monkeypatch):
    """
    Require a reviewed source refresh when dependencies or pinned snapshots change.
    """
    generator = runpy.run_path(str(GENERATOR))
    catalog = json.loads((ROOT / "schemas/sources.json").read_text())
    entry = catalog["providers"][0]["resources"][0]
    assert generator["checked_source"](entry)
    altered = tmp_path / "snapshot.json"
    altered.write_text("{}")
    with pytest.raises(ValueError, match="source digest mismatch"):
        generator["checked_source"]({**entry, "path": str(altered)})
    chart = tmp_path / "charts/polyad/Chart.yaml"
    chart.parent.mkdir(parents=True)
    chart.write_text("dependencies: []\n")
    monkeypatch.setitem(generator["upstream_catalog"].__globals__, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="schema pin differs from Chart.yaml"):
        generator["upstream_catalog"]()


def test_check_mode_detects_modified_missing_and_obsolete_outputs_without_writing(tmp_path, monkeypatch):
    """
    Check every output directory and fail safely without fixing files during pre-commit.
    """
    generator = runpy.run_path(str(GENERATOR))
    package = tmp_path / "package"
    chart = tmp_path / "chart"
    package.mkdir()
    chart.mkdir()
    modified = chart / "graph.json"
    modified.write_text("changed")
    orphan = package / "removed.schema.json"
    orphan.write_text("old")
    missing = package / "new.schema.json"
    namespace = generator["main"].__globals__
    for name, value in {
        "ROOT": tmp_path,
        "OUTPUT": package,
        "CHART_SCHEMAS": chart,
        "outputs": lambda: {modified: "expected", missing: "new"},
    }.items():
        monkeypatch.setitem(namespace, name, value)
    monkeypatch.setattr(sys, "argv", [str(GENERATOR), "--check"])
    assert generator["main"]() == 1
    assert modified.read_text() == "changed"
    assert orphan.read_text() == "old"
    assert not missing.exists()


def test_upstream_resource_groups_disambiguate_gateway_contracts():
    """
    Give Istio and Gateway API different identities despite sharing kind and API version.
    """
    istio = resource_schema("Gateway", "v1", group="networking.istio.io")
    gateway = resource_schema("Gateway", "v1", group="gateway.networking.k8s.io")
    assert istio["properties"]["apiVersion"]["const"] == "networking.istio.io/v1"
    assert gateway["properties"]["apiVersion"]["const"] == "gateway.networking.k8s.io/v1"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway", "metadata": {}, "spec": {}}, istio)


def test_categorized_schema_modules_work_without_importing_models():
    """
    Preserve the module-specific APIs and expose files through their matching resource packages.
    """
    from polyad_schemas.events import event_schema as event_contract
    from polyad_schemas.helm import values_schema
    from polyad_schemas.models import schema_for as model_contract
    from polyad_schemas.resources import resource_schema as manifest_contract

    assert model_contract("polyad_types.network.NetworkPort") == schema_for(NetworkPort)
    assert event_contract() == event_schema()
    assert manifest_contract("Graph") == resource_schema("Graph")
    assert values_schema() == load_schema("helm-values")
    assert values_schema(partial=True) == load_schema("helm-reference")
    for category in ("models", "resources", "events", "helm"):
        assert any(item.name.endswith(".schema.json") for item in files(f"polyad_schemas.{category}").iterdir())


def test_schemas_extra_pins_a_standalone_package_without_types_dependencies():
    """
    Keep optional operator installation and standalone wheel metadata consistent.
    """
    root = tomllib.loads((ROOT / "pyproject.toml").read_text())
    schemas = tomllib.loads((ROOT / "pkg/polyad-schemas/pyproject.toml").read_text())["project"]
    types = tomllib.loads((ROOT / "pkg/polyad-types/pyproject.toml").read_text())["project"]
    assert schemas["dependencies"] == []
    assert root["project"]["optional-dependencies"]["schemas"] == [f"polyad-schemas=={schemas['version']}"]
    assert root["tool"]["poetry"]["dependencies"]["polyad-schemas"]["optional"]
    assert not any("polyad-schemas" in dependency for dependency in types["dependencies"])


@pytest.mark.parametrize(
    "model",
    [
        CheegerComputation(maxVertices=32, priorityCuts=(("first", "second"),)),
        Replication(ReplicaTemplate("Graph", "pipeline"), connectivity=ReplicaConnectivity(mode="Ring")),
        ConnectionRequest("edge", "test", "Graph", "pipeline", "uid", "first", "second", 60, (NetworkPort(8080),)),
        ServiceAccess(),
        GraphMetrics(observedGeneration=2),
        PolyGraph(nodes=(GraphNode(name="pipeline", kind="Graph", ref="template"),)),
        Graph(metadata=ObjectMeta(name="pipeline", extra={"managedFields": [{"manager": "polyad"}]}), spec={"nodes": []}),
        ConfigMap(metadata=ObjectMeta(name="settings"), data={"mode": "demo"}),
    ],
)
def test_model_schemas_accept_serialized_shapes_and_preserved_extensions(model):
    """
    Validate concrete consumer documents, including generic graphs and native extensions.
    """
    document = json.loads(json.dumps(to_dict(model)))
    jsonschema.validate(document, schema_for(type(model)))


@pytest.mark.parametrize(
    "model,document",
    [
        (NetworkPort, {"port": 0}),
        (NetworkPort, {"port": True}),
        (NetworkPort, {"port": 80, "unexpected": "field"}),
        (CheegerComputation, {"priorityCuts": [[123]]}),
        (CheegerComputation, {"timeoutSeconds": 301}),
        (ServiceAccess, {"discovery": "Everything"}),
        (PolyGraph, {"nodes": [{"name": "worker", "kind": "Daemon", "ref": "worker"}]}),
        (Graph, {"apiVersion": "polyad.astrivant.com/v1alpha1", "kind": "Graph", "metadata": {}, "spec": {}}),
        (Graph, {"apiVersion": "v1", "kind": "Graph", "metadata": {"name": "bad"}, "spec": {}}),
        (ConfigMap, {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "bad"}, "spec": {}}),
    ],
)
def test_model_schemas_reject_wrong_types_limits_enums_and_resource_identity(model, document):
    """
    Reject malformed documents before consumers send them to the operator.
    """
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema_for(model))


def test_event_envelopes_keep_the_same_contract_through_both_schema_apis():
    """
    Preserve event wire requirements despite constructor defaults on the Python classes.
    """
    for document in json.loads((ROOT / "pkg/tests/data/events.json").read_text()):
        model_schema = schema_for(type(decode_event(document)))
        jsonschema.validate(document, model_schema)
        jsonschema.validate(document, event_schema())
        incomplete = {key: value for key, value in document.items() if key != "id"}
        for schema in (model_schema, event_schema()):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(incomplete, schema)


@pytest.mark.parametrize("filename", ["finite.yaml", "polygraph.yaml", "repeated-graph.yaml", "cheeger-tuning.yaml"])
def test_manifest_schemas_validate_documented_examples(filename):
    """
    Keep resource artifacts compatible with the repository's executable examples.
    """
    for document in yaml.safe_load_all((ROOT / "examples" / filename).read_text()):
        if document and document.get("apiVersion", "").startswith("polyad.astrivant.com/"):
            jsonschema.validate(document, resource_schema(document["kind"]))


def test_crd_contract_checks_full_spec_beyond_the_resource_envelope():
    """
    Distinguish an extensible Python resource envelope from a complete manifest contract.
    """
    document = to_document(Graph(metadata=ObjectMeta(name="pipeline"), spec={"mode": "wrong", "nodes": []}))
    jsonschema.validate(document, schema_for(Graph))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, resource_schema("Graph"))
    document["spec"]["mode"] = "persistent"
    jsonschema.validate(document, resource_schema("Graph"))
    document["kind"] = "PolyGraph"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, resource_schema("Graph"))


def test_helm_full_and_partial_contracts_work_offline_with_nested_references():
    """
    Preserve array-entry validation and canonical references inside partial overlays.
    """
    full = load_schema("helm-values")
    partial = load_schema("helm-reference")
    defaults = yaml.safe_load((ROOT / "charts/polyad/values.yaml").read_text())
    jsonschema.validate(defaults, full)
    jsonschema.validate({"ha": True, "events": {"maxEventBytes": 2048}}, partial)
    key = {
        "name": "service",
        "direction": "Inbound",
        "existingSecret": "credentials",
        "endpoints": ["events"],
        "requestsPerMinute": 60,
        "maxConcurrentRequests": 8,
    }
    jsonschema.validate({"authentication": {"services": [key]}}, partial)
    for invalid in ({"ha": "true"}, {"events": {"maxEventBytes": 1}}, {"authentication": {"services": [{"name": "incomplete"}]}}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, partial)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"ha": True}, full)


def test_schema_loads_are_independent_and_names_cannot_escape_package():
    """
    Keep consumer edits isolated and reject unknown artifact and model names.
    """
    original = load_schema("models")
    loaded = load_schema("models")
    loaded["$defs"].clear()
    assert load_schema("models") == original
    assert event_schema() == load_schema("events")
    for name in ("../events", "", "unpublished"):
        with pytest.raises(ValueError, match="unknown packaged schema"):
            load_schema(name)
    with pytest.raises(ValueError, match="no packaged model schema"):
        schema_for(str)


def test_openapi_conversion_preserves_nullable_enum_and_numeric_bounds():
    """
    Translate OpenAPI-only keywords into equivalent standard validation rules.
    """
    convert = runpy.run_path(str(GENERATOR))["json_schema"]
    schema = convert({"type": "string", "nullable": True, "enum": ["Ready"], "x-kubernetes-validations": [{"rule": "true"}]})
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid(None)
    assert validator.is_valid("Ready")
    assert not validator.is_valid("Wrong")
    assert convert({"type": "number", "minimum": 0, "exclusiveMinimum": True}) == {"type": "number", "exclusiveMinimum": 0}
