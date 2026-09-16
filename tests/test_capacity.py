"""
Exercise capacity forecasting, durable handoff and autoscaler failure semantics.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta

import pytest
from kubernetes.client.exceptions import ApiException

from polyad.compiler.passes.capacity import frontier, placeholder, requests
from polyad.compiler.passes.schema import structural_schema
from polyad.graph import CapacityPlan, Dependency, Node, Topology
from polyad.operator.capacity import CONSUME
from polyad.operator.controller import Controller, Pending
from polyad_types import resources as asts
from polyad_types.codec import converter
from tests.test_operator import FakeAPI, resource, template


class CapacityAPI(FakeAPI):
    """
    Model API discovery, Pod dry runs, merge patches and asynchronous deletion.
    """

    available = True
    denied = False
    lose_kind = None

    async def request(self, method, kind, namespace, name="", body=None, **kwargs):
        """
        Keep admission dry runs inert and capacity status updates JSON-merge compatible.
        """
        if kind == "ProvisioningRequest" and method == "GET":
            if self.denied:
                raise ApiException(status=403)
            if not self.available:
                return None
        if method == "POST" and kind == "Pod" and ("dryRun", "All") in kwargs.get("query", []):
            result = asts.encode_body(body)
            result = copy.deepcopy(result)
            result["spec"].setdefault("priority", 0)
            return result
        if method == "POST" and kind == self.lose_kind:
            self.lose_kind = None
            self.fail_create_after_commit = True
        if method == "GET" and kind == "Pod" and not name:
            items = self.children("Pod")
            for key, selector in kwargs.get("query", []):
                if key == "labelSelector":
                    for term in selector.split(","):
                        label, value = term.split("=", 1)
                        items = [obj for obj in items if obj["metadata"].get("labels", {}).get(label) == value]
            return {"items": copy.deepcopy(items)}
        result = await super().request(method, kind, namespace, name, body, **kwargs)
        if method == "PATCH" and kwargs.get("status"):
            obj = self.objects[kind, namespace, name]
            capacity = obj.get("status", {}).get("capacity")
            if capacity:
                capacity["nodes"] = {key: value for key, value in capacity["nodes"].items() if value is not None}
        return result

    def collect(self):
        """
        Simulate completed foreground deletion only when the test explicitly allows it.
        """
        self.objects = {key: obj for key, obj in self.objects.items() if not obj["metadata"].get("deletionTimestamp")}


@pytest.fixture(autouse=True)
def capacity_settings(monkeypatch):
    """
    Enable opt-in capacity with an administrator-managed placeholder priority class.
    """
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", "true")
    monkeypatch.setenv("POLYAD_CAPACITY_PRIORITY_CLASS", "capacity")
    monkeypatch.setenv("POLYAD_CAPACITY_PRIORITY", "-5")


def scenario(backend="Auto", *, nodes=None, **policy):
    """
    Construct a graph and a workload with explicit scheduling demand.
    """
    pod = template()
    pod["metadata"] = {"labels": {"app": "real-service"}}
    pod["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "2", "memory": "1Gi"}}
    graph = resource(
        "Graph",
        "pipeline",
        {
            "capacity": {"backend": backend, **policy},
            "placement": {"nodeSelector": {"pool": "compute"}},
            "nodes": nodes or [{"name": "work", "kind": "Workload", "ref": "job"}],
        },
    )
    api = CapacityAPI(graph, resource("Workload", "job", {"template": pod}))
    return api, Controller(api), ("Graph", "test", "pipeline")


async def passes(controller, key, count=1):
    """
    Allow several refreshed reconciliation turns without running any workloads.
    """
    for _ in range(count):
        await controller.reconcile(key)


def test_public_capacity_roundtrip_and_validation():
    """
    Share validated Python objects and generated CRD property shapes.
    """
    graph = Topology(nodes=(), capacity=CapacityPlan(lookaheadStages=2))
    assert converter.structure(converter.unstructure(graph), Topology) == graph
    assert structural_schema(CapacityPlan)["properties"]["maxPods"]["maximum"] == 1024
    for options in (
        {"maxPods": 0},
        {"timeoutSeconds": 1},
        {"lookaheadStages": 33},
        {"backend": "magic"},
        {"provisioningClassName": "check-capacity.autoscaling.x-k8s.io"},
    ):
        with pytest.raises(ValueError):
            CapacityPlan(**options)


def test_effective_requests_include_init_sidecars_and_overhead():
    """
    Sequential init peaks and restartable init resources follow Pod scheduling arithmetic.
    """
    spec = {
        "containers": [{"resources": {"requests": {"cpu": "2", "memory": "1Gi"}}}],
        "initContainers": [
            {"restartPolicy": "Always", "resources": {"requests": {"cpu": "500m"}}},
            {"resources": {"requests": {"cpu": "4"}}},
        ],
        "overhead": {"cpu": "100m"},
    }
    result = requests(spec)
    assert result["cpu"] == "4.600"
    assert result["memory"] == "1073741824"
    assert requests({"containers": [{"resources": {"limits": {"nvidia.com/gpu": "1"}}}]}) == {"nvidia.com/gpu": "1"}


def test_forecast_advances_before_dependency_completion():
    """
    Existing upstream execution exposes the next uncreated dependency layer.
    """
    graph = Topology(
        nodes=(
            Node("a", "Workload", "job"),
            Node("b", "Workload", "job", requires=(Dependency("a"),)),
            Node("c", "Workload", "job", requires=(Dependency("b"),)),
        ),
        capacity=CapacityPlan(),
    )
    assert frontier(graph, set()) == ["a"]
    assert frontier(graph, {"a"}) == ["b"]
    assert frontier(graph, {"a", "b"}) == ["c"]


def test_provisioning_blocks_until_fresh_success_and_traces_consumption():
    """
    Capacity acknowledgement precedes admission and survives controller replacement.
    """

    async def run():
        api, controller, key = scenario("ProvisioningRequest")
        await passes(controller, key, 3)
        assert not api.children("Job")
        request = api.children("ProvisioningRequest")[0]
        pod_template = api.children("PodTemplate")[0]
        assert pod_template["template"]["spec"]["nodeSelector"] == {"pool": "compute"}
        assert "spec" not in pod_template
        assert request["apiVersion"] == "autoscaling.x-k8s.io/v1"
        assert request["spec"]["podSets"][0]["podTemplateRef"]["name"] == pod_template["metadata"]["name"]
        request["status"] = {"conditions": [{"type": "Provisioned", "status": "True", "observedGeneration": 0}]}
        await passes(controller, key)
        assert not api.children("Job")
        request["status"]["conditions"][0]["observedGeneration"] = 1
        await passes(Controller(api), key)
        job = api.children("Job")[0]
        assert job["spec"]["template"]["metadata"]["annotations"][CONSUME] == request["metadata"]["name"]
        await passes(controller, key)
        assert not request["metadata"].get("deletionTimestamp")
        assert api.objects[key]["status"]["capacity"]["nodes"]["work"]["phase"] == "Consumed"

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["Failed", "BookingExpired", "CapacityRevoked"])
def test_provider_failure_never_falls_back(failure):
    """
    A failed or revoked request blocks execution even with an old success condition.
    """

    async def run():
        api, controller, key = scenario()
        await passes(controller, key, 2)
        request = api.children("ProvisioningRequest")[0]
        request["status"] = {
            "conditions": [
                {"type": "Provisioned", "status": "True"},
                {"type": failure, "status": "True", "message": "provider unavailable"},
            ]
        }
        await passes(controller, key, 2)
        assert not api.children("Job") and not api.children("Pod")
        assert api.objects[key]["status"]["capacity"]["nodes"]["work"]["phase"] == "Failed"
        assert api.objects[key]["status"]["metrics"]["rollup"]["capacityFailedPlans"] == 1

    asyncio.run(run())


def test_fallback_placeholders_and_ordered_release():
    """
    Missing API selects isolated placeholders; deletion must finish before real admission.
    """

    async def run():
        api, controller, key = scenario()
        api.available = False
        await passes(controller, key, 4)
        pod = api.children("Pod")[0]
        assert pod["spec"]["containers"][0]["image"] == "registry.k8s.io/pause:3.10"
        assert pod["spec"]["nodeSelector"] == {"pool": "compute"}
        assert pod["spec"]["priorityClassName"] == "capacity"
        assert not pod["spec"]["automountServiceAccountToken"]
        assert "app" not in pod["metadata"]["labels"]
        assert api.children("NetworkPolicy")[0]["spec"]["egress"] == []
        assert api.objects[key]["status"]["metrics"]["execution"]["observedNodes"] == 0
        pod["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
        await passes(controller, key)
        assert not api.children("Job")
        assert pod["metadata"].get("deletionTimestamp")
        assert not api.children("NetworkPolicy")[0]["metadata"].get("deletionTimestamp")
        api.collect()
        await passes(Controller(api), key)
        assert not api.children("Job")  # NetworkPolicy cleanup is also observed.
        api.collect()
        await passes(controller, key)
        assert len(api.children("Job")) == 1
        assert not api.children("Pod")

    asyncio.run(run())


def test_discovery_forbidden_is_not_fallback():
    """
    Authorization errors retain their API failure semantics.
    """

    async def run():
        api, controller, key = scenario()
        api.denied = True
        with pytest.raises(ApiException) as caught:
            await passes(controller, key)
        assert caught.value.status == 403
        assert not api.children("Pod")

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["PodTemplate", "ProvisioningRequest"])
def test_lost_creation_ack_does_not_duplicate_capacity(kind):
    """
    Stable plan identity recovers a committed request after a transport timeout.
    """

    async def run():
        api, controller, key = scenario()
        api.lose_kind = kind
        with pytest.raises(ApiException):
            await passes(controller, key, 2)
        await passes(Controller(api), key, 3)
        assert len(api.children(kind)) == 1
        assert len(api.children("ProvisioningRequest")) == 1
        assert api.objects[key]["status"]["capacity"]["nodes"]["work"]["resources"]["cpu"] == "2"

    asyncio.run(run())


def test_timeout_is_durable_and_requires_explicit_retry():
    """
    Expired forecasts release resources and cannot restart on every rescan.
    """

    async def run():
        api, controller, key = scenario()
        await passes(controller, key, 2)
        state = api.objects[key]["status"]["capacity"]["nodes"]["work"]
        state["startedAt"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await passes(controller, key)
        assert api.objects[key]["status"]["capacity"]["nodes"]["work"]["phase"] == "Expired"
        for _ in range(4):
            api.collect()
            await passes(Controller(api), key)
        assert not api.children("ProvisioningRequest") and not api.children("Job")
        api.objects[key]["spec"]["capacity"]["retryToken"] = "retry-1"
        api.objects[key]["metadata"]["generation"] += 1
        await passes(controller, key, 2)
        assert len(api.children("ProvisioningRequest")) == 1

    asyncio.run(run())


def test_generation_change_drains_old_forecast_before_creating_new_one():
    """
    Obsolete capacity remains a cleanup barrier across replica handoff.
    """

    async def run():
        api, controller, key = scenario()
        await passes(controller, key, 2)
        old = api.children("ProvisioningRequest")[0]
        api.objects[key]["metadata"]["generation"] += 1
        api.objects[key]["spec"]["placement"]["nodeSelector"]["pool"] = "other"
        await passes(Controller(api), key)
        assert old["metadata"].get("deletionTimestamp")
        assert len(api.children("ProvisioningRequest")) == 1
        for _ in range(4):
            api.collect()
            await passes(controller, key)
        new = api.children("ProvisioningRequest")[0]
        assert new["metadata"]["name"] != old["metadata"]["name"]
        assert api.children("PodTemplate")[0]["template"]["spec"]["nodeSelector"] == {"pool": "other"}

    asyncio.run(run())


def test_prewarms_downstream_without_bypassing_dependencies():
    """
    Next-stage capacity is requested while its upstream Job is still running.
    """

    async def run():
        api, controller, key = scenario(
            nodes=[
                {"name": "a", "kind": "Workload", "ref": "job"},
                {"name": "b", "kind": "Workload", "ref": "job", "requires": [{"node": "a"}]},
            ]
        )
        await passes(controller, key, 2)
        api.children("ProvisioningRequest")[0]["status"] = {"conditions": [{"type": "Provisioned", "status": "True"}]}
        await passes(controller, key, 3)
        assert len(api.children("Job")) == 1
        assert len(api.children("ProvisioningRequest")) == 2
        for request in api.children("ProvisioningRequest"):
            request["status"] = {"conditions": [{"type": "Provisioned", "status": "True"}]}
        await passes(controller, key)
        assert len(api.children("Job")) == 1
        api.children("Job")[0]["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        await passes(controller, key)
        assert len(api.children("Job")) == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "unsupported",
    [
        {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]},
        {"affinity": {"podAffinity": {}}},
        {"topologySpreadConstraints": [{}]},
        {"resourceClaims": [{}]},
        {"schedulerName": "custom"},
    ],
)
def test_placeholder_rejects_unrepresentable_scheduling(unsupported):
    """
    Never advertise a false storage, affinity or custom-scheduler reservation.
    """
    spec = {"containers": [{"resources": {"requests": {"cpu": "1"}}}], **unsupported}
    with pytest.raises(ValueError, match="ProvisioningRequest"):
        placeholder(spec, image="pause", priority_class="capacity", priority=-5)


def test_capacity_requires_operator_opt_in(monkeypatch):
    """
    A graph cannot silently use additional resource privileges on a disabled operator.
    """
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", "false")

    async def run():
        api, controller, key = scenario()
        with pytest.raises(ValueError, match="capacity.enabled"):
            await passes(controller, key)
        assert not api.children("PodTemplate")

    asyncio.run(run())


@pytest.mark.parametrize("action", ["suspend", "delete", "invalidate"])
def test_cancellation_releases_helpers_without_admitting_work(action):
    """
    Finalizers and validation failures cannot leave prewarming capacity alive indefinitely.
    """

    async def run():
        api, controller, key = scenario("Placeholders")
        await passes(controller, key, 3)
        graph = api.objects[key]
        if action == "suspend":
            graph["spec"]["suspend"] = True
            graph["metadata"]["generation"] += 1
        elif action == "delete":
            graph["metadata"]["deletionTimestamp"] = "now"
        else:
            graph["spec"]["nodes"][0]["requires"] = [{"node": "unknown"}]
            graph["metadata"]["generation"] += 1
        for _ in range(4):
            try:
                await passes(Controller(api), key)
            except (Pending, ValueError):
                pass
            api.collect()
            if key not in api.objects:
                break
        assert not api.children("Pod") and not api.children("PodTemplate")
        assert not api.children("Job")
        if key in api.objects:
            assert api.objects[key]["status"]["capacity"]["nodes"]["work"]["phase"] == "Cancelled"

    asyncio.run(run())


def test_capacity_policy_inherits_into_nested_graph_instances():
    """
    A PolyGraph propagates defaults while execution boundaries own their forecasts.
    """

    async def run():
        api, _, _ = scenario()
        parent = resource(
            "PolyGraph", "root", {"capacity": {"lookaheadStages": 2}, "nodes": [{"name": "child", "kind": "Graph", "ref": "pipeline"}]}
        )
        api.objects["Graph", "test", "pipeline"]["spec"].pop("capacity")
        api.objects["Graph", "test", "pipeline"]["spec"]["templateOnly"] = True
        api.objects["PolyGraph", "test", "root"] = parent
        await passes(Controller(api), ("PolyGraph", "test", "root"))
        child = next(graph for graph in api.children("Graph") if graph["metadata"]["name"] != "pipeline")
        assert child["spec"]["capacity"]["lookaheadStages"] == 2
        with pytest.raises(Pending, match="finalizer"):
            await passes(Controller(api), ("Graph", "test", child["metadata"]["name"]))
        await passes(Controller(api), ("Graph", "test", child["metadata"]["name"]), 2)
        assert len(api.children("ProvisioningRequest")) == 1
        await passes(Controller(api), ("PolyGraph", "test", "root"))
        assert api.objects["PolyGraph", "test", "root"]["status"]["metrics"]["rollup"]["capacityRequestedPods"] == 1

    asyncio.run(run())


def test_gate_wait_expires_capacity_without_starting_work():
    """
    An observed capacity grant never bypasses a Boolean gate.
    """

    async def run():
        api, controller, key = scenario(nodes=[{"name": "work", "kind": "Workload", "ref": "job", "gate": "wait"}])
        api.objects["Gate", "test", "wait"] = resource("Gate", "wait", {"delaySeconds": 300})
        await passes(controller, key, 2)
        api.children("ProvisioningRequest")[0]["status"] = {"conditions": [{"type": "Provisioned", "status": "True"}]}
        await passes(controller, key, 2)
        assert not api.children("Job")
        assert api.objects[key]["status"]["capacity"]["nodes"]["work"]["phase"] == "Ready"

    asyncio.run(run())


def test_forecast_pod_budget_limits_parallel_requests():
    """
    A graph cannot create more placeholder demand than its bounded budget.
    """

    async def run():
        api, controller, key = scenario(
            "Placeholders", maxPods=1, nodes=[{"name": name, "kind": "Workload", "ref": "job"} for name in ("a", "b", "c")]
        )
        await passes(controller, key, 4)
        assert len(api.children("Pod")) == 1
        assert sum(record["pods"] for record in api.objects[key]["status"]["capacity"]["nodes"].values()) == 1

    asyncio.run(run())


def test_forecast_rejects_same_name_template_owned_elsewhere():
    """
    Ownership fences apply to PodTemplates as well as actual workloads.
    """

    async def run():
        api, controller, key = scenario()
        await passes(controller, key)
        api.children("PodTemplate")[0]["metadata"]["ownerReferences"][0]["uid"] = "other-owner"
        with pytest.raises(ValueError, match="different ownership"):
            await passes(controller, key)
        assert not api.children("ProvisioningRequest") and not api.children("Job")

    asyncio.run(run())


def test_consumed_plan_does_not_keep_writing_elapsed_status():
    """
    Completed handoff freezes its elapsed time and leaves idle graph status stable.
    """

    async def run():
        api, controller, key = scenario()
        await passes(controller, key, 2)
        api.children("ProvisioningRequest")[0]["status"] = {"conditions": [{"type": "Provisioned", "status": "True"}]}
        await passes(controller, key, 2)
        api.objects = {key: obj for key, obj in api.objects.items() if obj["kind"] not in {"PodTemplate", "ProvisioningRequest"}}
        await passes(controller, key)
        record = api.objects[key]["status"]["capacity"]["nodes"]["work"]
        record["startedAt"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        before = len(api.calls)
        await passes(Controller(api), key)
        assert len(api.calls) == before

    asyncio.run(run())


def test_expired_helpers_retain_budget_until_deletion_is_observed():
    """
    Slow cleanup cannot temporarily double the configured forecast Pod budget.
    """

    async def run():
        api, controller, key = scenario(
            "Placeholders", maxPods=1, nodes=[{"name": name, "kind": "Workload", "ref": "job"} for name in ("a", "b")]
        )
        await passes(controller, key, 3)
        record = api.objects[key]["status"]["capacity"]["nodes"]["a"]
        record["startedAt"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        await passes(controller, key, 3)
        assert set(api.objects[key]["status"]["capacity"]["nodes"]) == {"a"}
        assert len(api.children("PodTemplate")) == 1

    asyncio.run(run())
