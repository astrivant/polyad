"""Exercise mixed graph composition and recursive, generation-fenced status propagation."""

from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock, patch

import pytest
from attrs import frozen

from polyad.compiler import asts
from polyad.graph import GraphNode, Node, PolyGraph
from polyad.graph.topology import converter, topology
from polyad.operator import handlers
from polyad.operator.controller import Controller, Pending
from polyad.operator.coordination import Coordinator
from tests.test_operator import FakeAPI, resource, template


def reference(name, kind, ref):
    """Create a typed-boundary reference in a serialized topology."""
    return {"name": name, "kind": kind, "ref": ref}


async def settle(api, passes=10):
    """Advance boundaries through finalizer persistence and child status propagation."""
    controller = Controller(api)
    for _ in range(passes):
        for key in list(api.objects):
            if key[0] not in asts.BOUNDARY_KINDS:
                continue
            try:
                await controller.reconcile(key)
            except Pending:
                pass


def test_python_composition_roundtrip_and_boundary_restriction():
    """Compose any supported boundary type and reject direct leaf work in PolyGraph."""
    graph = PolyGraph(nodes=tuple(GraphNode(name=kind.lower(), kind=kind, ref="template") for kind in asts.BOUNDARY_KINDS))
    document = converter.unstructure(graph)
    assert topology(document, "PolyGraph") == graph
    assert all(isinstance(node, GraphNode) for node in topology(document, "PolyGraph").nodes)
    with pytest.raises(ValueError, match="graph boundaries"):
        PolyGraph(nodes=(Node(name="leaf", kind="Workload", ref="job"),))
    with pytest.raises(ValueError, match="Workload not in literal"):
        topology({"nodes": [reference("leaf", "Workload", "job")]}, "PolyGraph")
    compiled = asts.PolyGraph(metadata=asts.ObjectMeta(name="root"), spec=document)
    assert asts.from_document(asts.to_document(compiled)) == compiled


def test_specialized_polygraph_roundtrip():
    """Decode concrete attrs fields through an explicit generic specialization."""

    @frozen(kw_only=True)
    class RegionalGraph(GraphNode):
        """Attach application metadata to a Python graph reference."""

        region: str

    graph = PolyGraph[RegionalGraph](nodes=(RegionalGraph(name="batch", kind="Graph", ref="batch", region="west"),))
    document = converter.unstructure(graph, unstructure_as=PolyGraph[RegionalGraph])
    restored = converter.structure(document, PolyGraph[RegionalGraph])
    assert restored == graph
    assert isinstance(restored.nodes[0], RegionalGraph)
    assert restored.nodes[0].region == "west"


def test_mixed_graph_types_roll_up_leaf_work_once():
    """Graph, spot, recurrence and nested PolyGraph counts all reach the same root."""

    async def scenario():
        root = resource(
            "PolyGraph",
            "root",
            {
                "mode": "persistent",
                "nodes": [
                    reference("service", "Graph", "service-template"),
                    reference("spot", "EphemeralGraph", "spot-template"),
                    reference("loop", "Feedback", "loop-template"),
                    reference("group", "PolyGraph", "group-template"),
                ],
            },
        )
        api = FakeAPI(
            root,
            resource(
                "Graph",
                "service-template",
                {"templateOnly": True, "mode": "persistent", "nodes": [reference("server", "Daemon", "server")]},
            ),
            resource(
                "EphemeralGraph",
                "spot-template",
                {
                    "templateOnly": True,
                    "placement": {"nodeSelector": {"capacity": "spot"}},
                    "nodes": [reference("worker", "Ephemeral", "worker")],
                },
            ),
            resource(
                "Feedback",
                "loop-template",
                {"templateOnly": True, "rounds": 1, "graph": {"nodes": [reference("worker", "Workload", "worker")]}},
            ),
            resource("PolyGraph", "group-template", {"templateOnly": True, "nodes": [reference("batch", "Graph", "batch-template")]}),
            resource("Graph", "batch-template", {"templateOnly": True, "nodes": [reference("worker", "Workload", "worker")]}),
            resource("Workload", "worker", {"template": template()}),
            resource("Ephemeral", "worker", {"template": template()}),
            resource("Daemon", "server", {"template": template(True)}),
        )
        await settle(api)
        key = "PolyGraph", "test", "root"
        rollup = api.objects[key]["status"]["metrics"]["rollup"]
        assert rollup["observationsComplete"] is True
        assert rollup["graphCount"] == 7
        assert rollup["leafNodes"] == rollup["observedLeafNodes"] == rollup["activeLeafNodes"] == 4
        assert rollup["resourceCount"] == 10  # Six child boundary CRs plus four leaves.
        assert rollup["nestingDepth"] == 3
        assert rollup["unobservedGraphs"] == 0
        assert len(api.children("Job")) == 3
        for job in api.children("Job"):
            job["status"] = {"active": 1}
        api.children("Deployment")[0]["status"] = {
            "observedGeneration": 1,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
        }
        await settle(api, 4)
        assert api.objects[key]["status"]["ready"] is True
        assert api.objects[key]["status"]["completed"] is False
        assert api.objects[key]["status"]["metrics"]["rollup"]["readyLeafNodes"] == 4
        # Stable summaries must not produce a status/watch feedback loop.
        calls = len(api.calls)
        await settle(api, 2)
        assert len(api.calls) == calls

    asyncio.run(scenario())


def deep_graphs():
    """Build a PolyGraph of a PolyGraph of a finite Graph."""
    return FakeAPI(
        resource("PolyGraph", "root", {"nodes": [reference("group", "PolyGraph", "group-template")]}),
        resource("PolyGraph", "group-template", {"templateOnly": True, "nodes": [reference("batch", "Graph", "batch-template")]}),
        resource("Graph", "batch-template", {"templateOnly": True, "nodes": [reference("job", "Workload", "worker")]}),
        resource("Workload", "worker", {"template": template()}),
    )


@pytest.mark.parametrize("condition,field,phase", [("Complete", "completed", "Completed"), ("Failed", "failed", "Failed")])
def test_deep_lifecycle_reaches_root(condition, field, phase):
    """Terminal leaf facts propagate through every intermediate abstraction."""

    async def scenario():
        api = deep_graphs()
        await settle(api)
        api.children("Job")[0]["status"] = {"conditions": [{"type": condition, "status": "True"}]}
        await settle(api, 4)
        status = api.objects[("PolyGraph", "test", "root")]["status"]
        assert status[field] is True
        assert status["phase"] == phase
        assert status["metrics"]["rollup"][f"{field}LeafNodes"] == 1
        assert status["metrics"]["rollup"]["graphsByPhase"][phase] == 3

    asyncio.run(scenario())


def test_missing_stale_and_terminating_subtrees_are_explicit():
    """Never present unavailable subtree totals as a complete root observation."""

    async def scenario():
        api = deep_graphs()
        controller = Controller(api)
        root = "PolyGraph", "test", "root"
        await controller.report_metrics(root)
        before = api.objects[root]["status"]["metrics"]["rollup"]
        assert before["observationsComplete"] is False
        assert before["unobservedGraphs"] == 1
        assert before["leafNodes"] == 0
        await settle(api)
        child = next(obj for obj in api.children("PolyGraph") if obj["metadata"].get("ownerReferences"))
        saved = copy.deepcopy(child)
        child["metadata"]["generation"] += 1
        await controller.report_metrics(root)
        rollup = api.objects[root]["status"]["metrics"]["rollup"]
        assert rollup["observationsComplete"] is False
        assert rollup["unobservedGraphs"] == 1
        assert rollup["leafNodes"] == 0
        assert rollup["graphCount"] == 2  # Root plus the known child; deeper contents are unknown.
        child.update(saved)
        child["metadata"]["deletionTimestamp"] = "now"
        await controller.report_metrics(root)
        rollup = api.objects[root]["status"]["metrics"]["rollup"]
        assert rollup["observationsComplete"] is False
        assert rollup["terminatingResources"] == 1

    asyncio.run(scenario())


def test_invalid_child_failure_propagates_and_owner_event_enqueues_parent():
    """Validation failure blocks the root and changes enqueue both child and parent."""

    async def scenario():
        api = deep_graphs()
        await settle(api)
        child = next(obj for obj in api.children("Graph") if obj["metadata"].get("ownerReferences"))
        child["status"].update(phase="Invalid", failed=False, ready=False, completed=False)
        # Simulate the handler's Invalid publication without executing that child again.
        group = next(obj for obj in api.children("PolyGraph") if obj["metadata"].get("ownerReferences"))
        controller = Controller(api)
        await controller.reconcile(("PolyGraph", "test", group["metadata"]["name"]))
        await controller.reconcile(("PolyGraph", "test", "root"))
        assert api.objects[("PolyGraph", "test", "root")]["status"]["failed"] is True
        with patch.object(handlers, "publish", new_callable=AsyncMock) as publish:
            await handlers.handle(namespace="test", name=child["metadata"]["name"], body=child)
        assert [call.args[0] for call in publish.await_args_list] == [
            ("Graph", "test", child["metadata"]["name"]),
            ("PolyGraph", "test", group["metadata"]["name"]),
        ]
        coordinator = Coordinator(api, "test", "replica")
        assert await coordinator.shard_for(("Graph", "test", child["metadata"]["name"])) == await coordinator.shard_for(
            ("PolyGraph", "test", "root")
        )

    asyncio.run(scenario())


def test_reused_graph_types_create_distinct_owned_instances():
    """Referencing one graph definition twice represents two executions in the root fold."""

    async def scenario():
        api = FakeAPI(
            resource("PolyGraph", "root", {"nodes": [reference("left", "Graph", "batch"), reference("right", "Graph", "batch")]}),
            resource("Graph", "batch", {"templateOnly": True, "nodes": [reference("worker", "Workload", "worker")]}),
            resource("Workload", "worker", {"template": template()}),
        )
        await settle(api)
        rollup = api.objects[("PolyGraph", "test", "root")]["status"]["metrics"]["rollup"]
        assert rollup["graphCount"] == 3
        assert rollup["leafNodes"] == rollup["activeLeafNodes"] == 2
        assert rollup["resourceCount"] == 4
        assert len({job["metadata"]["uid"] for job in api.children("Job")}) == 2

    asyncio.run(scenario())


def test_polygraph_placement_and_persistent_completion_contracts():
    """Root placement reaches leaves while persistent descendants cannot satisfy completion."""

    async def scenario():
        api = deep_graphs()
        key = "PolyGraph", "test", "root"
        api.objects[key]["spec"]["placement"] = {"nodeSelector": {"pool": "batch"}}
        await settle(api)
        assert api.children("Job")[0]["spec"]["template"]["spec"]["nodeSelector"] == {"pool": "batch"}
        api = FakeAPI(
            resource("PolyGraph", "root", {"nodes": [reference("service", "Graph", "service")]}),
            resource("Graph", "service", {"templateOnly": True, "mode": "persistent", "nodes": []}),
        )
        with pytest.raises(ValueError, match="persistent nested boundaries"):
            await Controller(api).reconcile(key)

    asyncio.run(scenario())


def test_example_completes_with_epoch_cleanup_and_root_totals():
    """The shipped mixed composition finishes with only retained executions counted."""
    from pathlib import Path

    import jsonschema
    import yaml

    async def scenario():
        root_dir = Path(__file__).resolve().parents[1]
        documents = list(yaml.safe_load_all((root_dir / "examples/polygraph.yaml").read_text()))
        schemas = {
            doc["spec"]["names"]["kind"]: doc["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
            for path in (root_dir / "charts/polyad/crds").glob("*.yaml")
            for doc in [yaml.safe_load(path.read_text())]
        }
        for doc in documents:
            jsonschema.Draft7Validator(schemas[doc["kind"]]).validate(doc)
        api = FakeAPI(*(resource(doc["kind"], doc["metadata"]["name"], doc["spec"]) for doc in documents))
        key = "PolyGraph", "test", "composed"
        for _ in range(35):
            for job in api.children("Job"):
                job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
            await settle(api, 1)
            # Emulate foreground garbage collection after dependent finalizers finish.
            for child_key, child in list(api.objects.items()):
                meta = child["metadata"]
                if meta.get("deletionTimestamp") and not meta.get("finalizers") and not await api.owned("test", meta["uid"]):
                    del api.objects[child_key]
        status = api.objects[key]["status"]
        assert status["completed"] is True
        rollup = status["metrics"]["rollup"]
        assert rollup["observationsComplete"] is True
        assert rollup["completedLeafNodes"] == 2
        assert rollup["graphCount"] == 5
        assert rollup["nestingDepth"] == 3
        assert rollup["resourceCount"] == 6
        # Template definitions have no execution status and are excluded from root totals.
        assert all("status" not in obj for obj in api.objects.values() if obj["spec"].get("templateOnly"))

    asyncio.run(scenario())
