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

from polyad.compiler.activation import ActivationRequest
from polyad.compiler.passes.composition import CompositionItem
from polyad.compiler.registry import BOUNDARY_KINDS, RESOURCE_TYPES
from polyad.graph.network import NetworkPeer
from polyad.graph.replication import ReplicaTemplate
from polyad.graph.topology import converter, topology
from tests.test_composition import settle
from tests.test_operator import FakeAPI, resource


@pytest.mark.parametrize("kind", ["Feedback", "EphemeralGraph"])
def test_retired_boundaries_are_rejected_across_public_inputs(kind):
    """
    Reject retired kinds instead of silently interpreting them as ordinary graphs.
    """
    assert BOUNDARY_KINDS == {"Graph", "PolyGraph", "ReplicaGroup"}
    assert kind not in RESOURCE_TYPES
    with pytest.raises(ValueError, match="unsupported graph kind"):
        topology({"nodes": []}, kind)
    with pytest.raises(ValueError):
        topology({"nodes": [{"name": "removed", "kind": kind, "ref": "definition"}]})
    with pytest.raises(ValueError, match="composition cannot create"):
        CompositionItem(id="removed", kind=kind, spec={"nodes": []})
    with pytest.raises(ValueError):
        ActivationRequest(requestId="run", graph="root", graphUid="uid-root", node="work", kind=kind)
    for model, value in ((NetworkPeer, {"kind": kind, "graph": "root"}), (ReplicaTemplate, {"kind": kind, "ref": "definition"})):
        with pytest.raises(CattrsError, match="not in literal"):
            converter.structure(value, model)


def test_repeated_graph_example_uses_durable_activation_and_fresh_executions():
    """
    Complete one ordinary graph and create a new instance only after a new timer pulse.
    """

    async def run():
        documents = list(yaml.safe_load_all(Path("examples/repeated-graph.yaml").read_text()))
        schemas = {
            document["spec"]["names"]["kind"]: document["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
            for path in Path("charts/polyad/crds").glob("*.yaml")
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
