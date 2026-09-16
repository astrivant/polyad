"""
Check the public metrics models, merge-patch semantics and generated CRD contracts.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from attrs import field, frozen
from jsonschema import ValidationError, validate

from polyad.compiler.asts import AST, GraphMetrics, ObjectMeta, ResourceMetrics, SubgraphMetrics, converter, to_document
from polyad.compiler.passes.schema import structural_schema
from polyad.graph import measure_topology, topology_metrics
from polyad.graph.topology import topology
from polyad.operator.graph_status import instance_metrics, observe_graph
from tests.test_graph_metrics import diamond
from tests.test_operator import resource

ROOT = Path(__file__).resolve().parents[1]
CRDS = ("graphs.yaml", "polygraphs.yaml", "replicagroups.yaml")


def metrics_schema(document):
    """
    Locate the shared metrics property in a graph CRD.
    """
    return document["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]["properties"]["metrics"]


def test_typed_observations_preserve_dictionary_contract():
    """
    Both public forms expose the same real graph measurements and recursive counts.
    """
    graph = topology(diamond())
    measured = measure_topology(graph)
    assert measured.admission.layerWidths == [1, 2, 1]
    assert to_document(measured) == topology_metrics(graph)
    parent = resource("Graph", "root", diamond())
    observation = observe_graph(parent, [])
    assert isinstance(observation, GraphMetrics)
    assert observation.execution.pendingNodes == 4
    assert observation.rollup.leafNodes == 4
    assert to_document(observation) == instance_metrics(parent, [])
    restored = converter.structure(to_document(observation), GraphMetrics)
    assert restored == observation
    assert isinstance(restored.resources, ResourceMetrics)


def test_nullable_status_and_extension_round_trip():
    """
    Clear stale metrics with explicit nulls while preserving future fields independently.
    """
    document = to_document(GraphMetrics(subgraphs=[SubgraphMetrics(name="child")]))
    document["future"] = {"values": [1]}
    document["resources"]["futureCount"] = 2
    original = copy.deepcopy(document)
    model = converter.structure(document, GraphMetrics)
    document["future"]["values"].append(2)
    assert to_document(model) == original
    assert to_document(model)["topology"] is None
    assert to_document(model)["subgraphs"][0]["observedGeneration"] is None
    assert "uid" not in to_document(ObjectMeta(name="graph"))
    # Merge patches remove null properties. Reading them back remains supported.
    del original["execution"]
    assert converter.structure(original, GraphMetrics).execution is None
    assert GraphMetrics().subgraphs is not GraphMetrics().subgraphs


@pytest.mark.parametrize("name", CRDS)
def test_chart_metrics_are_generated_from_models(name):
    """
    All graph kinds publish the exact generated schema, including descriptions.
    """
    document = yaml.safe_load((ROOT / "charts/polyad/crds" / name).read_text())
    assert metrics_schema(document) == structural_schema(GraphMetrics)


def test_schema_constraints_and_required_fields():
    """
    Propagate attrs requirements, nullable types, descriptions and counter constraints.
    """

    @frozen(kw_only=True)
    class Example(AST):
        """
        Example observation with one required identity.
        """

        identity: str = field(metadata={"schema": {"description": "Stable identity."}})
        optional: int | None = None

    schema = structural_schema(Example)
    assert schema["required"] == ["identity"]
    assert schema["properties"]["optional"] == {"type": "integer", "nullable": True}
    assert schema["properties"]["identity"]["description"] == "Stable identity."
    assert "extra" not in schema["properties"]
    with pytest.raises(ValidationError):
        validate({"resources": {"total": -1}}, structural_schema(GraphMetrics))
    with pytest.raises(ValidationError):
        validate({"scope": "subtree"}, structural_schema(GraphMetrics))
    with pytest.raises(TypeError, match="unsupported"):
        structural_schema(dict)
    schema["properties"]["identity"]["description"] = "changed"
    assert structural_schema(Example)["properties"]["identity"]["description"] == "Stable identity."


def test_regeneration_repairs_drift_without_changing_other_fields(tmp_path):
    """
    Check mode fails on drift; regeneration only replaces metrics and is repeatable.
    """
    originals = {}
    for name in CRDS:
        source = (ROOT / "charts/polyad/crds" / name).read_text()
        originals[name] = yaml.safe_load(source)
        # A valid YAML change within metrics must be noticed by the check hook.
        (tmp_path / name).write_text(source.replace("Metrics cover this scheduling boundary only.", "Stale description."))
    command = [sys.executable, str(ROOT / "scripts/generate-status-schemas.py"), "--crd-dir", str(tmp_path)]
    result = subprocess.run([*command, "--check"], capture_output=True, text=True, check=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert all(name in result.stdout for name in CRDS)
    subprocess.run(command, check=True, capture_output=True)
    regenerated = {name: (tmp_path / name).read_text() for name in CRDS}
    for name, source in regenerated.items():
        assert yaml.safe_load(source) == originals[name]
    subprocess.run([*command, "--check"], check=True, capture_output=True)
    subprocess.run(command, check=True, capture_output=True)
    assert regenerated == {name: (tmp_path / name).read_text() for name in CRDS}
