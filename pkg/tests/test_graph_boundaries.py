"""
Keep the boundary surface small and compose repetition from ordinary graph activation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from cattrs.errors import CattrsError
from jsonschema import Draft7Validator

from polyad.compiler.registry import BOUNDARY_KINDS, RESOURCE_TYPES
from polyad_types.api.requests import ActivationRequest, CompositionItem
from polyad_types.graphs.replication import ReplicaTemplate
from polyad_types.graphs.topology import topology
from polyad_types.networking.access import NetworkPeer
from polyad_types.serialization import converter
from tests.test_composition import settle
from tests.test_operator import FakeAPI, resource


def test_unknown_kinds_are_rejected_across_public_inputs():
    """
    Accept only supported resource kinds across graph, composition and replica inputs.
    """
    assert BOUNDARY_KINDS == {"Graph", "PolyGraph", "ReplicaGroup"}
    kind = "Unknown"
    assert kind not in RESOURCE_TYPES
    with pytest.raises(ValueError, match="unsupported graph kind"):
        topology({"nodes": []}, kind)
    with pytest.raises(ValueError):
        topology({"nodes": [{"name": "work", "kind": kind, "ref": "definition"}]})
    with pytest.raises(ValueError, match="composition cannot create"):
        CompositionItem(id="work", kind=kind, spec={"nodes": []})
    with pytest.raises(ValueError):
        ActivationRequest(requestId="run", graph="root", graphUid="uid-root", node="work", kind=kind)
    for model, value in ((NetworkPeer, {"kind": kind, "graph": "root"}), (ReplicaTemplate, {"kind": kind, "ref": "definition"})):
        with pytest.raises(CattrsError, match="not in literal"):
            converter.structure(value, model)


@pytest.mark.parametrize("filename", ["spot-workload.yaml", "spot-interruption.yaml"])
def test_spot_examples_compile_ordinary_workloads_with_placement(filename):
    """
    Compile finite Jobs on explicit spot placement with the declared retry budget.
    """

    async def run():
        documents = list(yaml.safe_load_all((Path("examples") / filename).read_text()))
        for document in documents:
            descriptor = RESOURCE_TYPES[document["kind"]]
            crd = yaml.safe_load((Path("charts/polyad-crds/crds") / f"{descriptor.plural}.yaml").read_text())
            Draft7Validator(crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]).validate(document)
        api = FakeAPI(*(resource(document["kind"], document["metadata"]["name"], document["spec"]) for document in documents))
        await settle(api)
        (job,) = api.children("Job")
        assert job["spec"]["template"]["spec"]["nodeSelector"] == {"polyad.astrivant.com/capacity": "spot"}
        assert job["spec"]["backoffLimit"] == 10

    asyncio.run(run())


def test_repeated_graph_example_uses_durable_activation_and_fresh_executions():
    """
    Complete one ordinary graph and create a new instance only after a new timer pulse.
    """

    async def run():
        documents = list(yaml.safe_load_all(Path("examples/repeated-graph.yaml").read_text()))
        schemas = {
            document["spec"]["names"]["kind"]: document["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
            for path in Path("charts/polyad-crds/crds").glob("*.yaml")
            for document in [yaml.safe_load(path.read_text())]
        }
        for document in documents:
            Draft7Validator(schemas[document["kind"]]).validate(document)
        api = FakeAPI(*(resource(document["kind"], document["metadata"]["name"], document["spec"]) for document in documents))
        await settle(api)
        (first,) = api.children("Job")
        (receipt,) = api.children("Activation")
        assert receipt["status"]["phase"] == "Running"
        first["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        await settle(api)
        assert receipt["status"]["phase"] == "Completed"
        root = api.objects[("Graph", "test", "recurring")]
        assert root["status"]["activations"]["run"]["completed"] == 1
        assert not root["status"]["completed"]
        assert len(api.children("Job")) == 1
        receipt["status"]["admittedAt"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
        for _ in range(15):
            await settle(api, 1)
            # Finish acknowledged foreground deletion, retaining boundaries until children drain.
            for key, child in list(api.objects.items()):
                meta = child["metadata"]
                if meta.get("deletionTimestamp") and not meta.get("finalizers") and not await api.owned("test", meta["uid"]):
                    del api.objects[key]
        (second,) = api.children("Job")
        assert second["metadata"]["uid"] != first["metadata"]["uid"]
        assert len(api.children("Activation")) == 2
        assert {item["status"]["phase"] for item in api.children("Activation")} == {"Completed", "Running"}
        assert all(key[0] in RESOURCE_TYPES for key in api.objects)

    asyncio.run(run())
