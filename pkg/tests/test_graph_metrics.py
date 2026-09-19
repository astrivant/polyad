"""
Verify graph shape, observed inventory, nested freshness and status write fences.
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from kubernetes.client.exceptions import ApiException

from polyad.graph import topology_metrics
from polyad.operator.observability.graph_status import instance_metrics
from polyad.operator.reconciliation.controller import Controller, Pending
from polyad_types.graphs.topology import topology
from tests.test_operator import FakeAPI, resource, template


def diamond():
    """
    Create parallel branches with a common root and join.
    """
    return {
        "nodes": [
            {"name": "a", "kind": "Workload", "ref": "worker"},
            {"name": "b", "kind": "Workload", "ref": "worker", "requires": [{"node": "a"}]},
            {"name": "c", "kind": "Workload", "ref": "worker", "requires": [{"node": "a"}]},
            {"name": "d", "kind": "Workload", "ref": "worker", "requires": [{"node": "b"}, {"node": "c"}]},
        ]
    }


def test_diamond_and_observed_induced_shape():
    """
    Depth counts node layers; observed subsets drop absent vertices and edges.
    """
    graph = topology(diamond())
    metrics = topology_metrics(graph)
    assert metrics["nodeCount"] == 4
    assert metrics["admission"] == {
        "edgeCount": 4,
        "depth": 3,
        "breadth": 2,
        "layerWidths": [1, 2, 1],
        "breadthDepth": 6,
        "rootCount": 1,
        "leafCount": 1,
        "maxFanIn": 2,
        "maxFanOut": 2,
    }
    observed = topology_metrics(graph, {"a", "b", "c"})
    assert observed["nodeCount"] == 3
    assert observed["admission"]["layerWidths"] == [1, 2]
    assert observed["admission"]["edgeCount"] == 2


def test_empty_and_disconnected_graphs():
    """
    Empty boundaries and isolated vertices have defined finite measurements.
    """
    empty = topology_metrics(topology({"nodes": []}))
    assert empty["nodeCount"] == empty["admission"]["depth"] == empty["admission"]["breadth"] == 0
    assert empty["connections"]["weakComponents"] == empty["connections"]["condensation"]["depth"] == 0
    spec = diamond()
    for node in spec["nodes"]:
        node.pop("requires", None)
    independent = topology_metrics(topology(spec))
    assert independent["admission"]["layerWidths"] == [4]
    assert independent["connections"]["strongComponents"] == independent["connections"]["weakComponents"] == 4


def test_cycle_components_and_duplicate_edges():
    """
    Cycles collapse to components and repeated declarations do not inflate edges.
    """
    spec = diamond()
    spec["nodes"][1]["requires"].append({"node": "a", "condition": "ready"})
    spec["connections"] = [
        {"source": "a", "target": "b"},
        {"source": "b", "target": "a"},
        {"source": "b", "target": "c"},
        {"source": "c", "target": "c"},
        {"source": "a", "target": "b"},
    ]
    shape = topology_metrics(topology(spec))
    assert shape["admission"]["edgeCount"] == 4
    assert shape["connections"] == {
        "edgeCount": 4,
        "weakComponents": 2,
        "strongComponents": 3,
        "cyclicComponents": 2,
        "largestStrongComponent": 2,
        "condensation": {"depth": 2, "breadth": 2, "layerWidths": [2, 1], "breadthDepth": 4},
    }


def test_instance_metrics_progress_and_idempotence():
    """
    Refresh counts after creation and completion without issuing unchanged status writes.
    """

    async def scenario():
        spec = diamond()
        spec["slots"] = 2
        key = "Graph", "test", "pipeline"
        api = FakeAPI(resource("Graph", "pipeline", spec), resource("Workload", "worker", {"template": template()}))
        controller = Controller(api)
        await controller.reconcile(key)
        metrics = api.objects[key]["status"]["metrics"]
        assert metrics["topology"]["nodeCount"] == 4
        assert metrics["observedTopology"]["nodeCount"] == 1
        assert metrics["execution"]["pendingNodes"] == 3
        assert metrics["execution"]["reservedSlots"] == metrics["execution"]["activeNodes"] == 1
        # One pass updates the lifecycle node facts after observing the created Job.
        await controller.reconcile(key)
        calls = len(api.calls)
        await controller.reconcile(key)
        assert len(api.calls) == calls
        api.children("Job")[0]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        await controller.reconcile(key)
        metrics = api.objects[key]["status"]["metrics"]
        assert metrics["observedTopology"]["admission"]["layerWidths"] == [1, 2]
        assert metrics["execution"]["completedNodes"] == 1
        assert metrics["execution"]["activeNodes"] == metrics["execution"]["reservedSlots"] == 2
        assert metrics["execution"]["availableSlots"] == 0

    asyncio.run(scenario())


def test_pending_missing_definition_and_deletion_report_inventory():
    """
    Blocked admission and delayed finalizers still publish graph measurements.
    """

    async def scenario():
        key = "Graph", "test", "pipeline"
        api = FakeAPI(resource("Graph", "pipeline", {"nodes": [{"name": "a", "kind": "Workload", "ref": "missing"}]}))
        controller = Controller(api)
        with pytest.raises(Pending):
            await controller.reconcile(key)
        status = api.objects[key]["status"]
        assert "missing" in status["message"]
        assert status["metrics"]["topology"]["nodeCount"] == status["metrics"]["execution"]["pendingNodes"] == 1
        api.objects[("Workload", "test", "missing")] = resource("Workload", "missing", {"template": template()})
        await controller.reconcile(key)
        assert api.objects[key]["status"]["message"] == ""
        api.objects[key]["metadata"]["deletionTimestamp"] = "now"
        with pytest.raises(Pending):
            await controller.reconcile(key)
        status = api.objects[key]["status"]
        assert status["phase"] == "Draining"
        assert status["metrics"]["resources"]["terminating"] == status["metrics"]["execution"]["terminatingNodes"] == 1
        assert status["metrics"]["execution"]["activeNodes"] == 0
        assert not status["ready"] and not status["completed"]

    asyncio.run(scenario())


def test_replacement_and_suspension_retain_live_inventory():
    """
    Removed nodes leave the desired topology while cleanup remains visible.
    """

    async def scenario():
        key = "Graph", "test", "pipeline"
        api = FakeAPI(
            resource("Graph", "pipeline", {"nodes": [{"name": "a", "kind": "Workload", "ref": "worker"}]}),
            resource("Workload", "worker", {"template": template()}),
        )
        controller = Controller(api)
        await controller.reconcile(key)
        api.objects[key]["spec"]["nodes"] = []
        api.objects[key]["metadata"]["generation"] = 2
        with pytest.raises(Pending):
            await controller.reconcile(key)
        metrics = api.objects[key]["status"]["metrics"]
        assert metrics["observedGeneration"] == 2
        assert metrics["topology"]["nodeCount"] == metrics["observedTopology"]["nodeCount"] == 0
        assert metrics["resources"]["total"] == metrics["resources"]["terminating"] == 1
        api.objects[key]["spec"]["suspend"] = True
        await controller.reconcile(key)
        assert api.objects[key]["status"]["phase"] == "Draining"
        job = api.children("Job")[0]
        del api.objects[("Job", "test", job["metadata"]["name"])]
        await controller.reconcile(key)
        assert api.objects[key]["status"]["phase"] == "Suspended"
        assert api.objects[key]["status"]["metrics"]["resources"]["total"] == 0

    asyncio.run(scenario())


def test_nested_metrics_require_current_child_generation():
    """
    Expose immediate summaries and reject stale generations without flattening descendants.
    """

    async def scenario():
        key = "Graph", "test", "loop"
        api = FakeAPI(
            resource("Graph", "loop", {"nodes": [{"name": "child", "kind": "Graph", "ref": "body"}]}),
            resource("Graph", "body", {"templateOnly": True, "nodes": []}),
        )
        controller = Controller(api)
        await controller.reconcile(key)
        assert api.objects[key]["status"]["metrics"]["execution"]["observedNodes"] == 1
        child = next(item for item in api.children("Graph") if item["metadata"].get("ownerReferences"))
        child_key = "Graph", "test", child["metadata"]["name"]
        # First persist the finalizer; the next pass can execute the child graph.
        with pytest.raises(Pending):
            await controller.reconcile(child_key)
        await controller.reconcile(child_key)
        parent = api.objects[key]
        child = api.objects[child_key]
        metrics = instance_metrics(parent, [child])
        assert metrics["subgraphs"][0]["current"] is True
        assert metrics["execution"]["observedNodes"] == 1
        assert metrics["subgraphs"][0]["topology"]["nodeCount"] == 0
        stale = copy.deepcopy(child)
        stale["metadata"]["generation"] += 1
        metrics = instance_metrics(parent, [stale])
        assert metrics["subgraphs"][0]["current"] is False
        assert metrics["subgraphs"][0]["topology"] is None
        assert metrics["execution"]["readyNodes"] == 0
        await controller.reconcile(key)
        assert api.objects[key]["status"]["completed"]

    asyncio.run(scenario())


def test_invalid_topology_metrics_do_not_mask_validation():
    """
    Keep inventory available even when a graph revision contains an admission cycle.
    """

    async def scenario():
        spec = diamond()
        spec["nodes"][0]["requires"] = [{"node": "d"}]
        api = FakeAPI(resource("Graph", "invalid", spec))
        key = "Graph", "test", "invalid"
        with pytest.raises(ValueError, match="admission cycle"):
            await Controller(api).reconcile(key)
        metrics = api.objects[key]["status"]["metrics"]
        assert metrics["topology"] is None
        assert "admission cycle" in metrics["topologyError"]
        # The handler publishes Invalid after the controller propagates the error.
        api.objects[key]["status"].update(phase="Invalid", message="admission cycle")
        calls = len(api.calls)
        with pytest.raises(ValueError):
            await Controller(api).reconcile(key)
        assert len(api.calls) == calls
        assert api.objects[key]["status"]["message"] == "admission cycle"

    asyncio.run(scenario())


def test_templates_remain_inert_and_status_is_version_fenced():
    """
    Only instances report execution and concurrent API changes reject stale metrics.
    """

    async def scenario():
        key = "Graph", "test", "definition"
        api = FakeAPI(resource("Graph", "definition", {"nodes": [], "templateOnly": True}))
        controller = Controller(api)
        await controller.reconcile(key)
        assert "status" not in api.objects[key]
        api.objects[key]["spec"]["templateOnly"] = False
        original_owned = api.owned

        async def mutate_during_inventory(namespace, uid):
            children = await original_owned(namespace, uid)
            api.objects[key]["metadata"]["resourceVersion"] = "2"
            return children

        api.owned = mutate_during_inventory
        with pytest.raises(ApiException) as error:
            await controller.report_metrics(key)
        assert error.value.status == 409
        assert "status" not in api.objects[key]

    asyncio.run(scenario())


def test_absent_nullable_metrics_do_not_cause_repeated_patches():
    """
    Kubernetes removes merge-patch null fields rather than retaining Python None.
    """

    async def scenario():
        key = "Graph", "test", "waiting"
        api = FakeAPI(
            resource("Graph", "waiting", {"nodes": [{"name": "bad", "kind": "Workload", "ref": "job", "requires": [{"node": "bad"}]}]})
        )
        controller = Controller(api)
        await controller.report_metrics(key)
        metrics = api.objects[key]["status"]["metrics"]
        del metrics["execution"]
        del metrics["observedTopology"]
        calls = len(api.calls)
        await controller.report_metrics(key)
        assert len(api.calls) == calls

    asyncio.run(scenario())


@pytest.mark.parametrize("kind,filename", [("Graph", "graphs"), ("PolyGraph", "polygraphs")])
def test_emitted_status_matches_crd_schema(kind, filename):
    """
    Validate wire metrics, nullable observations and schema rejection of bad gauges.
    """
    from pathlib import Path

    import jsonschema
    import yaml

    path = Path(__file__).resolve().parents[2] / "charts" / "polyad-crds" / "crds" / f"{filename}.yaml"
    crd = yaml.safe_load(path.read_text())
    schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]

    def json_schema(value):
        if isinstance(value, list):
            return [json_schema(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: json_schema(item) for key, item in value.items() if key != "nullable"}
        if value.get("nullable"):
            result["type"] = [result["type"], "null"]
        return result

    validator = jsonschema.Draft7Validator(json_schema(schema))
    spec = diamond()
    if kind == "PolyGraph":
        spec = {"nodes": [{"name": "child", "kind": "Graph", "ref": "template"}]}
    parent = resource(kind, "graph", spec)
    child = resource("Graph", "nested", {"nodes": []})
    # Includes null nested metrics and unknown phase before the child reports.
    status = {"phase": "Reconciling", "metrics": instance_metrics(parent, [child])}
    validator.validate(status)
    status["metrics"]["resources"]["total"] = -1
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(status)
