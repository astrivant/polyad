"""
Reconcile advance capacity through the graph's leased, ordered API adapter.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from attrs import evolve

from polyad.compiler import asts
from polyad.compiler.passes.capacity import frontier, placeholder, requests
from polyad.graph.topology import converter

if TYPE_CHECKING:
    from typing import Any

    from polyad.graph.capacity import CapacityPlan
    from polyad.graph.topology import Topology
    from polyad.operator.controller import Controller

CAPACITY_LABEL = f"{asts.GROUP}/capacity"
CONSUME = "autoscaling.x-k8s.io/consume-provisioning-request"
CLASS = "autoscaling.x-k8s.io/provisioning-class-name"
DEFAULT_CLASS = "best-effort-atomic-scale-up.autoscaling.x-k8s.io"
TERMINAL = {"Failed", "Expired", "Cancelled"}


class CapacityManager:
    """
    Persist request identity and handoff decisions before dispatching mutations.
    """

    def __init__(self, controller: Controller, obj: dict[str, Any]) -> None:
        """
        Bind a refreshed graph and its ownership-fenced API adapter.

        Args:
            controller (Controller): Current graph-family reconciliation controller.
            obj (dict[str, Any]): Persisted graph updated in place after capacity status writes.
        """
        self.controller = controller
        self.api = controller.api
        self.obj = obj
        self.namespace = obj["metadata"]["namespace"]
        self.state = asts.converter.structure(obj.get("status", {}).get("capacity") or {}, asts.CapacityStatus)
        self.ready: set[str] = set()
        self.plan: CapacityPlan | None = None
        self.inventory: list[dict[str, Any]] | None = None
        self.selected_backend: str | None = None

    async def save(self, *, allow_deleting: bool = False) -> None:
        """
        Persist capacity state with resource-version fencing and refresh before more writes.

        Args:
            allow_deleting (bool): Permit cancellation status while the graph drains.

        Returns:
            None: No return value.
        """
        # Merge patches need explicit nulls for removed map entries.
        document = asts.to_document(self.state)
        previous = self.obj.get("status", {}).get("capacity") or {}
        for removed in previous.get("nodes", {}).keys() - self.state.nodes.keys():
            document["nodes"][removed] = None
        await self.controller.status(self.obj, {"capacity": document})
        latest = await self.api.get(self.obj["kind"], self.namespace, self.obj["metadata"]["name"])
        if (
            latest is None
            or (latest["metadata"].get("deletionTimestamp") and not allow_deleting)
            or latest["metadata"].get("uid") != self.obj["metadata"].get("uid")
            or latest["metadata"].get("generation") != self.obj["metadata"].get("generation")
        ):
            from polyad.operator.controller import Pending

            raise Pending("capacity intent changed; refresh before admission")
        self.obj.clear()
        self.obj.update(latest)

    async def cancel(self, message: str) -> None:
        """
        Stop forecasts and release owned helpers during invalidation, suspension or deletion.

        Args:
            message (str): Cancellation explanation retained in graph status.

        Returns:
            None: No return value.
        """
        if not self.state.nodes:
            return
        self.state = evolve(
            self.state,
            observedGeneration=self.obj["metadata"]["generation"],
            nodes={name: evolve(record, phase="Cancelled", readyPods=0, message=message) for name, record in self.state.nodes.items()},
        )
        await self.save(allow_deleting=True)
        await self.cleanup()

    async def children(self, revision: str | None = None) -> list[dict[str, Any]]:
        """
        Refresh only helper resources owned by this graph.

        Args:
            revision (str | None): Optional capacity revision to select.

        Returns:
            list[dict[str, Any]]: Owned capacity resources, including terminating objects.
        """
        if self.inventory is None:
            self.inventory = await self.api.owned(self.namespace, self.obj["metadata"]["uid"])
        return [
            child
            for child in self.inventory
            if CAPACITY_LABEL in child["metadata"].get("labels", {})
            and (revision is None or child["metadata"]["labels"][CAPACITY_LABEL] == revision)
        ]

    async def cleanup(self, revision: str | None = None) -> bool:
        """
        Delete owned requests before templates and wait for observed disappearance.

        Args:
            revision (str | None): Optional capacity revision to release.

        Returns:
            bool: Whether refreshed inventory is already empty.
        """
        children = await self.children(revision)
        first = [child for child in children if child["kind"] == "ProvisioningRequest"]
        work = [child for child in children if child["kind"] != "NetworkPolicy"]
        for child in first or work or children:
            if not child["metadata"].get("deletionTimestamp"):
                await self.api.delete(child)
        return not children

    def helper(self, node: str, revision: str, suffix: str, kind: str, spec: dict[str, Any]) -> asts.Resource:
        """
        Compile collision-resistant helper identity with auditable graph ownership.

        Args:
            node (str): Workload node represented by the capacity request.
            revision (str): Durable generation and intent hash.
            suffix (str): Distinguish templates, requests and individual placeholders.
            kind (str): Native Kubernetes or autoscaler resource kind.
            spec (dict[str, Any]): Desired resource spec.

        Returns:
            asts.Resource: Owned helper with a dedicated capacity membership label.
        """
        child = self.controller.child(self.obj, f"capacity-{revision}-{suffix}", kind, spec)
        return evolve(
            child,
            metadata=evolve(child.metadata, labels={**(child.metadata.labels or {}), CAPACITY_LABEL: revision, f"{asts.GROUP}/node": node}),
        )

    async def backend(self, plan: CapacityPlan) -> str:
        """
        Use API availability for fallback without hiding authorization or provider failures.

        Args:
            plan (CapacityPlan): Desired backend selection policy.

        Returns:
            str: ProvisioningRequest or Placeholders.
        """
        if plan.backend == "Placeholders":
            return "Placeholders"
        if self.selected_backend is not None:
            return self.selected_backend
        result = await self.api.request("GET", "ProvisioningRequest", self.namespace, query=[("limit", "1")])
        if result is not None:
            self.selected_backend = "ProvisioningRequest"
            return self.selected_backend
        if plan.backend == "ProvisioningRequest":
            raise ValueError("ProvisioningRequest autoscaling.x-k8s.io/v1 API is not installed")
        self.selected_backend = "Placeholders"
        return self.selected_backend

    async def prepare(
        self, graph: Topology, desired: dict[str, asts.Resource], states: dict[str, dict[str, bool]], *, excluded: set[str] | None = None
    ) -> None:
        """
        Forecast upcoming execution and refresh readiness without bypassing admission gates.

        Args:
            graph (Topology): Validated graph and optional capacity policy.
            desired (dict[str, asts.Resource]): Compiled workloads including inherited placement.
            states (dict[str, dict[str, bool]]): Freshly observed existing execution nodes.
            excluded (set[str] | None): Dormant pulse vertices that must not reserve capacity.

        Returns:
            None: No return value.
        """
        self.plan = graph.capacity
        if graph.capacity is None:
            if self.state.nodes or (os.environ.get("POLYAD_CAPACITY_ENABLED", "false").lower() == "true" and await self.children()):
                if not await self.cleanup():
                    from polyad.operator.controller import Pending

                    raise Pending("releasing disabled capacity plan")
                self.state = asts.CapacityStatus(observedGeneration=self.obj["metadata"]["generation"])
                await self.save()
            self.ready.update(desired)
            return
        if os.environ.get("POLYAD_CAPACITY_ENABLED", "false").lower() != "true":
            raise ValueError("graph capacity requires capacity.enabled on the operator Helm chart")
        plan = graph.capacity
        generation = self.obj["metadata"]["generation"]
        policy = converter.unstructure(plan)
        policy["operator"] = {
            key: os.environ.get(key, "")
            for key in ("POLYAD_CAPACITY_CLASS", "POLYAD_CAPACITY_IMAGE", "POLYAD_CAPACITY_PRIORITY_CLASS", "POLYAD_CAPACITY_PRIORITY")
        }
        revisions = {
            name: hashlib.sha256(json.dumps([generation, asts.to_document(resource), policy], sort_keys=True).encode()).hexdigest()[:20]
            for name, resource in desired.items()
            if resource.resource_type.kind in {"Job", "Deployment"} and name not in (excluded or set())
        }
        for name, record in list(self.state.nodes.items()):
            if revisions.get(name) != record.revision or (record.phase == "Consumed" and name not in states):
                if not await self.cleanup(record.revision):
                    return
                del self.state.nodes[name]
        self.state = evolve(self.state, observedGeneration=generation)
        selected = frontier(graph, set(states))
        counts = {name: asts.to_document(desired[name])["spec"].get("replicas", 1) for name in revisions}
        # Existing plans remain stable as the frontier advances; never silently underforecast a replica group.
        allocated = 0
        for name, record in self.state.nodes.items():
            if name not in states and (record.phase not in TERMINAL or await self.children(record.revision)):
                allocated += record.pods
        operator_max = int(os.environ.get("POLYAD_CAPACITY_MAX_PODS", "1024"))
        maximum = min(plan.maxPods, operator_max)
        for name in selected:
            if name not in revisions or name in self.state.nodes or counts[name] == 0:
                continue
            if counts[name] > maximum:
                raise ValueError(f"capacity for {name} exceeds maxPods; increase the limit or reduce replicas")
            if allocated + counts[name] > maximum:
                continue
            backend = await self.backend(plan)
            self.state.nodes[name] = asts.CapacityNodeStatus(
                revision=revisions[name], backend=backend, startedAt=datetime.now(UTC).isoformat(), pods=counts[name]
            )
            allocated += counts[name]
        await self.save()  # Plan identity and timeout survive lost creation responses and replica handoff.
        for name, record in list(self.state.nodes.items()):
            elapsed = (datetime.now(UTC) - datetime.fromisoformat(record.startedAt)).total_seconds()
            if record.phase not in TERMINAL | {"Consumed"}:
                record = evolve(record, waitSeconds=max(0, int(elapsed)))
            self.state.nodes[name] = record
            if name in states:
                self.state.nodes[name] = evolve(record, phase="Consumed", readyPods=0)
                if not await self.children(record.revision):
                    continue
                if (
                    record.backend == "Placeholders"
                    or states[name]["completed"]
                    or elapsed >= plan.timeoutSeconds
                    or await self.scheduled(desired[name], record.pods)
                ):
                    await self.cleanup(record.revision)
                continue
            if record.phase in TERMINAL:
                await self.cleanup(record.revision)
                continue
            if elapsed >= plan.timeoutSeconds:
                self.state.nodes[name] = evolve(
                    record, phase="Expired", readyPods=0, message="capacity request expired; change capacity.retryToken to retry"
                )
                await self.save()
                await self.cleanup(record.revision)
                continue
            if record.phase == "Releasing":
                if await self.cleanup(record.revision):
                    self.ready.add(name)
                continue
            # Every turn re-observes the backing resource; saved Ready is never sufficient.
            await self.observe(name, record, desired[name])
        self.ready.update(name for name in desired if name not in revisions or counts[name] == 0)
        await self.save()

    async def scheduled(self, desired: asts.Resource, count: int) -> bool:
        """
        Retain provisioning requests until workload Pods have consumed their capacity.

        Args:
            desired (asts.Resource): Desired Job or Deployment.
            count (int): Number of Pods expected to consume the request.

        Returns:
            bool: Whether the expected Pods have been scheduled.
        """
        document = asts.to_document(desired)
        labels = (
            {"batch.kubernetes.io/job-name": desired.metadata.name}
            if document["kind"] == "Job"
            else document["spec"]["selector"]["matchLabels"]
        )
        result = await self.api.request(
            "GET", "Pod", self.namespace, query=[("labelSelector", ",".join(f"{key}={value}" for key, value in labels.items()))]
        )
        return (
            sum(
                bool(pod.get("spec", {}).get("nodeName")) and not pod["metadata"].get("deletionTimestamp")
                for pod in (result or {}).get("items", [])
            )
            >= count
        )

    async def observe(self, name: str, record: asts.CapacityNodeStatus, desired: asts.Resource) -> None:
        """
        Refresh immutable scheduling templates before provisioning or placeholder creation.

        Args:
            name (str): Logical workload node.
            record (asts.CapacityNodeStatus): Durable identity and chosen backend.
            desired (asts.Resource): Compiled workload resource.

        Returns:
            None: No return value.
        """
        assert self.plan is not None
        template = asts.to_document(desired)["spec"]["template"]
        identity = self.helper(name, record.revision, "template", "Pod", {})
        stored = await self.api.get("PodTemplate", self.namespace, identity.metadata.name or "")
        if stored is None:
            probe = copy.deepcopy(template)
            probe.update(apiVersion="v1", kind="Pod")
            probe.setdefault("metadata", {}).update(name=identity.metadata.name, namespace=self.namespace)
            admitted = await self.api.request("POST", "Pod", self.namespace, body=probe, query=[("dryRun", "All")])
            spec = admitted["spec"]
            if spec.get("nodeName") or spec.get("schedulingGates"):
                raise ValueError("capacity forecasts require Pods without nodeName or schedulingGates")
            demand = requests(spec)
            if not demand:
                raise ValueError("capacity planning requires nonzero resource requests")
            if template.get("metadata", {}).get("annotations", {}).get("sidecar.istio.io/inject") == "true" and not any(
                container.get("name") == "istio-proxy" and container.get("restartPolicy") == "Always"
                for container in spec.get("initContainers", [])
            ):
                raise ValueError("capacity forecast requires successful Istio native sidecar injection")
            if record.backend == "Placeholders":
                placeholder(
                    spec,
                    image=os.environ.get("POLYAD_CAPACITY_IMAGE", "registry.k8s.io/pause:3.10"),
                    priority_class=os.environ.get("POLYAD_CAPACITY_PRIORITY_CLASS", ""),
                    priority=int(os.environ.get("POLYAD_CAPACITY_PRIORITY", "-5")),
                )
            body = asts.PodTemplateResource(
                metadata=identity.metadata,
                template=asts.PodTemplate(spec=spec, metadata=asts.converter.structure(template.get("metadata", {}), asts.ObjectMeta)),
            )
            await self.controller.ensure(body)
            self.state.nodes[name] = evolve(
                record, phase="Provisioning", resources={key: str(Decimal(value) * record.pods) for key, value in demand.items()}
            )
            return
        if stored["metadata"].get("ownerReferences") != asts.to_document(identity)["metadata"].get("ownerReferences"):
            raise ValueError("refusing to adopt a capacity template with different ownership")
        if stored["metadata"].get("deletionTimestamp"):
            return
        record = evolve(
            record, resources={key: str(Decimal(value) * record.pods) for key, value in requests(stored["template"]["spec"]).items()}
        )
        if record.backend == "ProvisioningRequest":
            await self.provision(name, record, stored)
        else:
            await self.reserve(name, record, stored)

    async def provision(self, name: str, record: asts.CapacityNodeStatus, template: dict[str, Any]) -> None:
        """
        Observe provisioned status and fail closed on revoked or expired bookings.

        Args:
            name (str): Logical workload node.
            record (asts.CapacityNodeStatus): Durable identity and timeout.
            template (dict[str, Any]): Persisted native PodTemplate.

        Returns:
            None: No return value.
        """
        assert self.plan is not None
        provisioning_class = self.plan.provisioningClassName or os.environ.get("POLYAD_CAPACITY_CLASS", DEFAULT_CLASS)
        if provisioning_class == "check-capacity.autoscaling.x-k8s.io":
            raise ValueError("capacity plans require a provisioning class that scales out")
        spec = asts.ProvisioningRequestSpec(
            provisioningClassName=provisioning_class,
            podSets=(asts.PodSet(podTemplateRef=asts.PodTemplateReference(name=template["metadata"]["name"]), count=record.pods),),
            parameters={"ValidUntilSeconds": str(self.plan.timeoutSeconds), **self.plan.parameters},
        )
        desired = self.helper(name, record.revision, "request", "ProvisioningRequest", asts.to_document(spec))
        current = await self.controller.ensure(desired)
        record = evolve(record, requestName=desired.metadata.name or "", phase="Provisioning", readyPods=0)
        if current is not None:
            conditions = {
                condition["type"]: condition
                for condition in current.get("status", {}).get("conditions", [])
                if condition.get("status") == "True"
                and condition.get("observedGeneration", current["metadata"].get("generation", 1))
                == current["metadata"].get("generation", 1)
            }
            failure = next((conditions[key] for key in ("Failed", "BookingExpired", "CapacityRevoked") if key in conditions), None)
            if failure:
                record = evolve(record, phase="Failed", message=failure.get("message") or failure["type"])
            elif "Provisioned" in conditions:
                record = evolve(record, phase="Ready", readyPods=record.pods, message="")
                self.ready.add(name)
        self.state.nodes[name] = record

    async def reserve(self, name: str, record: asts.CapacityNodeStatus, template: dict[str, Any]) -> None:
        """
        Run isolated inert Pods to expose advance demand to a node autoscaler.

        Args:
            name (str): Logical workload node.
            record (asts.CapacityNodeStatus): Durable request identity.
            template (dict[str, Any]): Persisted workload scheduling template.

        Returns:
            None: No return value.
        """
        priority_class = os.environ.get("POLYAD_CAPACITY_PRIORITY_CLASS", "")
        if not priority_class:
            raise ValueError("placeholder capacity requires an administrator-configured PriorityClass")
        spec = placeholder(
            template["template"]["spec"],
            image=os.environ.get("POLYAD_CAPACITY_IMAGE", "registry.k8s.io/pause:3.10"),
            priority_class=priority_class,
            priority=int(os.environ.get("POLYAD_CAPACITY_PRIORITY", "-5")),
        )
        # Deny all Pod traffic. Placeholders have no application labels or service credentials.
        policy = self.helper(
            name,
            record.revision,
            "network",
            "NetworkPolicy",
            {
                "podSelector": {"matchLabels": {CAPACITY_LABEL: record.revision}},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        )
        if await self.controller.ensure(policy) is None:
            return
        ready = 0
        for index in range(record.pods):
            desired = self.helper(name, record.revision, str(index), "Pod", spec)
            desired = evolve(
                desired,
                metadata=evolve(
                    desired.metadata,
                    annotations={
                        **(desired.metadata.annotations or {}),
                        "sidecar.istio.io/inject": "false",
                        "cluster-autoscaler.kubernetes.io/safe-to-evict": "true",
                    },
                ),
            )
            current = await self.controller.ensure(desired)
            if current is not None:
                if current.get("status", {}).get("phase") in {"Failed", "Succeeded"}:
                    await self.api.delete(current)
                    continue
                ready += int(any(c["type"] == "Ready" and c["status"] == "True" for c in current.get("status", {}).get("conditions", [])))
        self.state.nodes[name] = evolve(record, phase="Ready" if ready == record.pods else "Provisioning", readyPods=ready)
        if ready == record.pods:
            self.ready.add(name)

    async def admit(self, name: str, desired: asts.Resource) -> asts.Resource | None:
        """
        Hand capacity to a dependency-eligible workload without claiming atomic scheduling.

        Args:
            name (str): Eligible graph node.
            desired (asts.Resource): Compiled resource to admit.

        Returns:
            asts.Resource | None: Resource ready to create, or None while capacity is unavailable.
        """
        if name not in self.ready:
            return None
        record = self.state.nodes.get(name)
        if record is None:
            return desired
        if record.backend == "Placeholders":
            if record.phase != "Releasing":
                self.state.nodes[name] = evolve(record, phase="Releasing", readyPods=0)
                await self.save()
            if not await self.cleanup(record.revision):
                return None
            return desired
        assert self.plan is not None
        body = asts.to_document(desired)
        annotations = {
            CONSUME: record.requestName,
            CLASS: self.plan.provisioningClassName or os.environ.get("POLYAD_CAPACITY_CLASS", DEFAULT_CLASS),
        }
        body["metadata"].setdefault("annotations", {}).update(annotations)
        body["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {}).update(annotations)
        return asts.from_document(body)
