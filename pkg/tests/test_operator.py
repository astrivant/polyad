"""
Exercise graph lifecycle against an API with acknowledgement and deletion delays.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

import pytest
from kubernetes.client.exceptions import ApiException

from polyad.graph import Node, Placement, Topology
from polyad.operator.adapters import ResourceAPI
from polyad.operator.adapters.kubernetes import GROUP, VERSION
from polyad.operator.coordination.queue import RefreshQueue
from polyad.operator.reconciliation.controller import FINALIZER, Controller, Pending, observed
from polyad.operator.runtime import OperatorThread
from polyad_types.codec import converter
from polyad_types.resources import encode_body
from polyad_types.topology import topology

if TYPE_CHECKING:
    from typing import Any


def resource(kind, name, spec=None):
    """
    Create a minimal stored resource with a durable identity.
    """
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "test",
            "uid": f"uid-{name}",
            "generation": 1,
            "resourceVersion": "1",
            "finalizers": [FINALIZER],
            "creationTimestamp": "2026-01-01T00:00:00Z",
        },
        "spec": spec or {},
    }


class FakeAPI(ResourceAPI):
    """
    Retain deleting resources until tests simulate the garbage collector.
    """

    def __init__(self, *objects):
        """
        Store deep copies so callers cannot mutate the server without a request.
        """
        self.objects = {(o["kind"], o["metadata"]["namespace"], o["metadata"]["name"]): copy.deepcopy(o) for o in objects}
        self.calls = []
        self.fail_create_after_commit = False

    async def get(self, kind, namespace, name):
        """
        Return a fresh snapshot.
        """
        return copy.deepcopy(self.objects.get((kind, namespace, name)))

    async def request(self, method, kind, namespace, name="", body=None, **kwargs):
        """
        Enforce optimistic concurrency and optionally lose creation acknowledgement.
        """
        if method == "GET":
            if name:
                return await self.get(kind, namespace, name)
            items = [copy.deepcopy(obj) for (k, ns, _), obj in self.objects.items() if k == kind and ns == namespace]
            for key, selector in kwargs.get("query", []):
                if key == "labelSelector":
                    label, value = selector.split("=", 1)
                    items = [obj for obj in items if obj["metadata"].get("labels", {}).get(label) == value]
            return {"items": items}
        body = encode_body(body)
        self.calls.append((method, kind, name))
        key = (kind, namespace, name or (body or {}).get("metadata", {}).get("name", ""))
        if method == "POST":
            if key in self.objects:
                raise ApiException(status=409)
            self.objects[key] = copy.deepcopy(body)
            self.objects[key]["metadata"].update(uid=str(uuid5(NAMESPACE_URL, "/".join(key))), resourceVersion="1", generation=1)
            if self.fail_create_after_commit:
                self.fail_create_after_commit = False
                raise ApiException(status=504)
            return copy.deepcopy(self.objects[key])
        obj = self.objects[key]
        if body["metadata"]["resourceVersion"] != obj["metadata"]["resourceVersion"]:
            raise ApiException(status=409)
        if method == "PUT":
            obj.update(copy.deepcopy(body))
            obj["metadata"]["generation"] = obj["metadata"].get("generation", 0) + 1
        elif "status" in body:
            obj.setdefault("status", {}).update(body["status"])
        else:
            obj["metadata"].update(copy.deepcopy(body["metadata"]))
        obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
        return copy.deepcopy(obj)

    async def owned(self, namespace, uid):
        """
        List even terminating children until deletion is observed.
        """
        return [
            copy.deepcopy(o)
            for o in self.objects.values()
            if o["metadata"].get("namespace") == namespace
            and any(owner["uid"] == uid for owner in o["metadata"].get("ownerReferences", []))
        ]

    async def delete(self, obj):
        """
        A delete response only requests deletion; it does not acknowledge cleanup.
        """
        meta = obj["metadata"]
        self.objects[(obj["kind"], meta["namespace"], meta["name"])]["metadata"]["deletionTimestamp"] = "now"
        self.calls.append(("DELETE", obj["kind"], meta["name"]))

    def children(self, kind):
        """
        Return mutable stored children to simulate Kubernetes observations.
        """
        return [obj for obj in self.objects.values() if obj["kind"] == kind]


def template(daemon=False):
    """
    Build an execution definition with application health probes.
    """
    container: dict[str, Any] = {"name": "main", "image": "busybox:1.37"}
    if daemon:
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            container[probe] = {"exec": {"command": ["true"]}}
    return {"spec": {"containers": [container]}}


def test_topology_separates_cyclic_flow_from_admission():
    """
    Persistent cycles are valid only when their admission predicates can progress.
    """
    spec = {
        "mode": "persistent",
        "nodes": [
            {"name": "a", "kind": "Daemon", "ref": "server"},
            {"name": "b", "kind": "Workload", "ref": "job", "requires": [{"node": "a", "condition": "ready"}]},
        ],
        "connections": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
    }
    assert topology(spec).mode == "persistent"
    spec["nodes"][1]["requires"][0]["condition"] = "completed"
    with pytest.raises(ValueError, match="daemons do not complete"):
        topology(spec)
    spec["nodes"][1]["requires"][0]["condition"] = "ready"
    spec["nodes"][0]["requires"] = [{"node": "b", "condition": "started"}]
    with pytest.raises(ValueError, match="admission cycle"):
        topology(spec)


def test_spot_workload_python_types_roundtrip():
    """
    Represent spot work with ordinary workload nodes and explicit placement.
    """
    node = Node(name="worker", kind="Workload", ref="spot")
    graph = Topology(nodes=(node,), placement=Placement({"capacity": "spot"}))
    data = converter.unstructure(graph)
    assert data["nodes"][0]["kind"] == "Workload"
    assert converter.structure(data, Topology).placement == graph.placement


def test_readiness_and_creation_timeout():
    """
    A lost create response never duplicates a daemon or admits a dependent early.
    """

    async def scenario():
        graph = resource(
            "Graph",
            "pipeline",
            {
                "mode": "persistent",
                "nodes": [
                    {"name": "server", "kind": "Daemon", "ref": "server"},
                    {"name": "consumer", "kind": "Workload", "ref": "job", "requires": [{"node": "server", "condition": "ready"}]},
                ],
            },
        )
        api = FakeAPI(
            graph, resource("Daemon", "server", {"template": template(True)}), resource("Workload", "job", {"template": template()})
        )
        controller = Controller(api)
        key = ("Graph", "test", "pipeline")
        api.fail_create_after_commit = True
        with pytest.raises(ApiException):
            await controller.reconcile(key)
        await controller.reconcile(key)
        assert len(api.children("Deployment")) == 1
        assert not api.children("Job")
        deployment = api.children("Deployment")[0]
        deployment["status"] = {"observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}
        await controller.reconcile(key)
        assert len(api.children("Job")) == 1
        assert not api.objects[key]["status"]["completed"]

    asyncio.run(scenario())


def test_replacement_and_finalizers_wait_for_absence():
    """
    Deleting children retain graph ownership and block replacement admission.
    """

    async def scenario():
        key = ("Graph", "test", "pipeline")
        api = FakeAPI(
            resource("Graph", "pipeline", {"nodes": [{"name": "job", "kind": "Workload", "ref": "job"}]}),
            resource("Workload", "job", {"template": template()}),
        )
        controller = Controller(api)
        await controller.reconcile(key)
        api.children("Job")[0]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        await controller.reconcile(key)
        assert api.objects[key]["status"]["completed"]
        api.objects[("Workload", "test", "job")]["spec"]["template"]["spec"]["containers"][0]["image"] = "busybox:1.36"
        with pytest.raises(Pending):
            await controller.reconcile(key)
        with pytest.raises(Pending):
            await controller.reconcile(key)
        assert len(api.children("Job")) == 1
        assert not api.objects[key]["status"]["ready"]
        assert not api.objects[key]["status"]["completed"]
        job = api.children("Job")[0]
        del api.objects[("Job", "test", job["metadata"]["name"])]
        await controller.reconcile(key)
        assert api.children("Job")[0]["spec"]["template"]["spec"]["containers"][0]["image"] == "busybox:1.36"
        api.objects[key]["metadata"]["deletionTimestamp"] = "now"
        with pytest.raises(Pending):
            await controller.reconcile(key)
        assert FINALIZER in api.objects[key]["metadata"]["finalizers"]
        job = api.children("Job")[0]
        del api.objects[("Job", "test", job["metadata"]["name"])]
        await controller.reconcile(key)

    asyncio.run(scenario())


def test_spot_placement_and_interruption():
    """
    Spot placement propagates and a replacement Pod is not reported as job completion.
    """

    async def scenario():
        graph = resource(
            "Graph",
            "spot",
            {"placement": {"nodeSelector": {"capacity": "spot"}}, "nodes": [{"name": "job", "kind": "Workload", "ref": "job"}]},
        )
        api = FakeAPI(graph, resource("Workload", "job", {"template": template()}))
        controller = Controller(api)
        key = ("Graph", "test", "spot")
        await controller.reconcile(key)
        job = api.children("Job")[0]
        assert job["spec"]["template"]["spec"]["nodeSelector"] == {"capacity": "spot"}
        job["status"] = {"failed": 1, "active": 1}
        await controller.reconcile(key)
        assert not api.objects[key]["status"]["completed"]
        job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        await controller.reconcile(key)
        assert api.objects[key]["status"]["completed"]

    asyncio.run(scenario())


def test_queue_serializes_coalesces_and_refreshes_during_work():
    """
    Enqueues during an in-flight pass are processed again after older queued keys.
    """

    async def scenario():
        calls = []
        entered, release = asyncio.Event(), asyncio.Event()

        async def reconcile(key):
            calls.append(key)
            if len(calls) == 1:
                entered.set()
                await release.wait()

        queue = RefreshQueue(reconcile)
        a, b = ("Graph", "ns", "a"), ("Graph", "ns", "b")
        queue.start()
        first = asyncio.create_task(queue.submit(a))
        await entered.wait()
        rest = [asyncio.create_task(queue.submit(key)) for key in (b, a, a)]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, *rest)
        assert calls == [a, b, a]
        await queue.stop()

    asyncio.run(scenario())


def test_operator_runs_off_main_thread_and_stops():
    """
    The process owner can cooperatively stop the embedded Kopf thread.
    """
    threads = []

    async def operator(*, stop_flag, ready_flag):
        threads.append(threading.current_thread())
        ready_flag.set()
        while not stop_flag.is_set():
            await asyncio.sleep(0.01)

    runtime = OperatorThread(operator)
    runtime.start()
    assert runtime.ready_flag.wait(2)
    runtime.stop()
    runtime.join()
    assert threads[0] is not threading.main_thread()
    assert not runtime.thread.is_alive()


def test_stale_deployment_observation_is_not_ready():
    """
    A previous generation's availability cannot release downstream admission.
    """
    deployment = resource("Deployment", "server", {"replicas": 1})
    deployment["metadata"]["generation"] = 2
    deployment["status"] = {"observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}
    assert not observed(deployment)["ready"]


def test_rewrite_receipt_survives_status_conflict():
    """
    A committed spec change is not repeated if the rewrite's status patch conflicts.
    """

    async def scenario():
        graph = resource("Graph", "target", {"nodes": [], "suspend": True})
        rewrite = resource("Rewrite", "change", {"graph": "target", "expectedGeneration": 1, "topology": {"nodes": []}})
        api = FakeAPI(graph, rewrite)
        controller = Controller(api)
        stale = copy.deepcopy(rewrite)
        api.objects[("Rewrite", "test", "change")]["metadata"]["resourceVersion"] = "2"
        with pytest.raises(ApiException):
            await controller.rewrite(stale)
        assert api.objects[("Graph", "test", "target")]["metadata"]["generation"] == 2
        assert "suspend" not in api.objects[("Graph", "test", "target")]["spec"]
        await controller.reconcile(("Rewrite", "test", "change"))
        assert api.objects[("Rewrite", "test", "change")]["status"]["applied"]
        assert len([call for call in api.calls if call[0] == "PUT"]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("uid", "replacement"), ("resourceVersion", "2"), ("deletionTimestamp", "2026-09-15T00:00:00Z")])
def test_rewrite_refreshes_preconditions_before_dispatch(field, value):
    """
    A target changed between planning and dispatch must return to the refresh queue.
    """

    async def scenario():
        graph = resource("Graph", "target", {"nodes": [], "suspend": True})
        rewrite = resource("Rewrite", "change", {"graph": "target", "expectedGeneration": 1, "topology": {"nodes": []}})

        class ChangingAPI(FakeAPI):
            async def get(self, kind, namespace, name):
                result = await super().get(kind, namespace, name)
                if kind == "Graph":
                    self.objects[(kind, namespace, name)]["metadata"][field] = value
                return result

        api = ChangingAPI(graph, rewrite)
        with pytest.raises(Pending, match="changed before dispatch"):
            await Controller(api).rewrite(rewrite)
        assert not any(call[0] == "PUT" for call in api.calls)
        assert api.objects[("Graph", "test", "target")]["spec"]["suspend"]

    asyncio.run(scenario())


def test_resources_and_gates_resolve_node_names():
    """
    A resource's readiness opens a gate and resolves its name in the consuming pod.
    """

    async def scenario():
        pod = template()
        pod["spec"]["volumes"] = [{"name": "config", "configMap": {"name": "${nodes.config.name}"}}]
        api = FakeAPI(
            resource(
                "Graph",
                "pipeline",
                {
                    "nodes": [
                        {"name": "config", "kind": "Resource", "ref": "config"},
                        {"name": "job", "kind": "Workload", "ref": "job", "gate": "gate"},
                    ]
                },
            ),
            resource("Resource", "config", {"manifest": {"apiVersion": "v1", "kind": "ConfigMap", "data": {"key": "value"}}}),
            resource("Gate", "gate", {"expression": {"operator": "signal", "name": "config.ready"}}),
            resource("Workload", "job", {"template": pod}),
        )
        controller = Controller(api)
        key = ("Graph", "test", "pipeline")
        await controller.reconcile(key)
        assert len(api.children("ConfigMap")) == 1
        assert not api.children("Job")
        await controller.reconcile(key)
        assert (
            api.children("Job")[0]["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"]
            == api.children("ConfigMap")[0]["metadata"]["name"]
        )

    asyncio.run(scenario())


def test_nested_graph_inherits_spot_placement():
    """
    Templates remain inert while instantiated nested boundaries inherit spot placement.
    """

    async def scenario():
        api = FakeAPI(
            resource(
                "Graph",
                "outer",
                {"placement": {"nodeSelector": {"capacity": "spot"}}, "nodes": [{"name": "inner", "kind": "Graph", "ref": "definition"}]},
            ),
            resource("Graph", "definition", {"templateOnly": True, "nodes": []}),
        )
        controller = Controller(api)
        await controller.reconcile(("Graph", "test", "definition"))
        assert len(api.objects) == 2
        await controller.reconcile(("Graph", "test", "outer"))
        nested = [obj for obj in api.children("Graph") if obj["metadata"].get("ownerReferences")][0]
        assert nested["spec"]["placement"]["nodeSelector"] == {"capacity": "spot"}
        assert nested["spec"]["templateOnly"] is False

    asyncio.run(scenario())


def test_api_list_items_can_omit_kind():
    """
    Normalize Kubernetes list items, whose TypeMeta is commonly absent on the wire.
    """
    from polyad.operator.adapters.kubernetes import API

    async def scenario():
        api = API.__new__(API)

        async def request(method, kind, namespace, **kwargs):
            if kind == "Job":
                return {"items": [{"metadata": {"name": "job", "ownerReferences": [{"uid": "parent"}]}}]}
            return {"items": []}

        api.request = request
        children = await api.owned("test", "parent")
        assert children[0]["kind"] == "Job"

    asyncio.run(scenario())


def test_graph_wide_placement_preserves_execution_kind_and_child_constraints():
    """
    General graph placement narrows nested graphs without changing their lifecycle.
    """

    async def scenario():
        api = FakeAPI(
            resource(
                "Graph",
                "outer",
                {
                    "placement": {"nodeSelector": {"pool": "batch"}, "tolerations": [{"key": "dedicated", "operator": "Exists"}]},
                    "nodes": [{"name": "inner", "kind": "Graph", "ref": "definition"}],
                },
            ),
            resource(
                "Graph",
                "definition",
                {
                    "templateOnly": True,
                    "placement": {"nodeSelector": {"zone": "east"}},
                    "nodes": [{"name": "job", "kind": "Workload", "ref": "worker"}],
                },
            ),
            resource("Workload", "worker", {"template": template()}),
        )
        controller = Controller(api)
        await controller.reconcile(("Graph", "test", "outer"))
        inner = [g for g in api.children("Graph") if g["metadata"].get("ownerReferences")][0]
        assert inner["kind"] == "Graph"
        assert inner["spec"]["placement"]["nodeSelector"] == {"pool": "batch", "zone": "east"}
        inner["metadata"]["finalizers"] = [FINALIZER]
        await controller.reconcile(("Graph", "test", inner["metadata"]["name"]))
        pod = api.children("Job")[0]["spec"]["template"]["spec"]
        assert pod["nodeSelector"] == {"pool": "batch", "zone": "east"}
        assert pod["tolerations"] == [{"key": "dedicated", "operator": "Exists"}]

    asyncio.run(scenario())


def test_placement_intersects_affinity_alternatives_and_does_not_mutate_inputs():
    """
    AND scopes together while retaining OR within each scope's node selector terms.
    """
    from polyad.operator.reconciliation.placement import REQUIRED, merge_placement, place_pod

    def term(key, value):
        return {"matchExpressions": [{"key": key, "operator": "In", "values": [value]}]}

    parent = {"nodeAffinity": {REQUIRED: {"nodeSelectorTerms": [term("pool", "a"), term("pool", "b")]}}}
    child = {"nodeAffinity": {REQUIRED: {"nodeSelectorTerms": [term("zone", "east"), term("zone", "west")]}}}
    before = copy.deepcopy(parent)
    terms = merge_placement(parent, child)["nodeAffinity"][REQUIRED]["nodeSelectorTerms"]
    assert len(terms) == 4
    assert all({e["key"] for e in term["matchExpressions"]} == {"pool", "zone"} for term in terms)
    assert parent == before
    pod = {"affinity": {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}}}
    place_pod(pod, parent)
    assert "podAntiAffinity" in pod["affinity"]
    empty = {"nodeAffinity": {REQUIRED: {"nodeSelectorTerms": [{}]}}}
    assert merge_placement(parent, empty)["nodeAffinity"][REQUIRED]["nodeSelectorTerms"] == []
    with pytest.raises(ValueError, match="conflicting placement label"):
        merge_placement({"nodeSelector": {"pool": "batch"}}, {"nodeSelector": {"pool": "other"}})
    with pytest.raises(ValueError, match="nodeName bypasses"):
        place_pod({"nodeName": "escape"}, parent)


def test_nested_graph_merges_general_placement():
    """
    Nested graphs combine parent and child placement without another boundary type.
    """

    async def scenario():
        api = FakeAPI(
            resource(
                "Graph",
                "outer",
                {"placement": {"nodeSelector": {"pool": "batch"}}, "nodes": [{"name": "loop", "kind": "Graph", "ref": "loop-template"}]},
            ),
            resource(
                "Graph",
                "loop-template",
                {"templateOnly": True, "placement": {"nodeSelector": {"zone": "east"}}, "nodes": []},
            ),
        )
        await Controller(api).reconcile(("Graph", "test", "outer"))
        child = [g for g in api.children("Graph") if g["metadata"].get("ownerReferences")][0]
        assert child["spec"]["placement"]["nodeSelector"] == {"pool": "batch", "zone": "east"}

    asyncio.run(scenario())
