"""
Schedule pulse executions through their parent graph's leased reconciliation stream.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from attrs import evolve

from polyad.api.activations import ActivationStore
from polyad.compiler import asts
from polyad.compiler.activation import ActivationRequest, activation_name
from polyad.compiler.passes.children import child_name
from polyad.compiler.passes.identity import inject_environment
from polyad.graph.topology import Dependency
from polyad.operator.graph_status import observed

if TYPE_CHECKING:
    from typing import Any

    from polyad.graph.activation import ActivationPolicy
    from polyad.graph.topology import Topology
    from polyad.operator.controller import Controller

TERMINAL = {"Completed", "Failed", "Rejected", "Superseded", "Stopped"}
ACTIVE = {"Queued", "Running", "Ready", "Stopping"}
ANNOTATION = f"{asts.GROUP}/activation-uid"


class Activations:
    """
    Keep admission decisions durable before creating independently identified executions.
    """

    def __init__(self, controller: Controller, obj: dict[str, Any]) -> None:
        """
        Bind the graph family and guarded mutation adapter.

        Args:
            controller (Controller): Controller holding the graph family's shard.
            obj (dict[str, Any]): Fresh graph instance.
        """
        self.controller, self.obj = controller, obj
        self.records: dict[str, dict[str, Any]] = {}
        self.policies: dict[str, ActivationPolicy] = {}
        self.blocked: set[str] = set()
        self.summary: dict[str, Any] = {}

    async def save(self, receipt: dict[str, Any], phase: str, **values: Any) -> None:
        """
        Persist a receipt decision and refresh its concurrency token before further changes.

        Args:
            receipt (dict[str, Any]): Current Activation document updated in place.
            phase (str): New admission or execution phase.
            **values (Any): Additional observation fields.

        Returns:
            None: Receipt mutation is acknowledged before execution changes.
        """
        await self.controller.status(
            receipt,
            {
                "phase": phase,
                "ready": phase == "Ready",
                "completed": phase == "Completed",
                "failed": phase in {"Failed", "Rejected"},
                "observedGeneration": receipt["metadata"].get("generation", 1),
                **values,
            },
        )
        latest = await self.controller.api.get("Activation", receipt["metadata"]["namespace"], receipt["metadata"]["name"])
        if latest is None or latest["metadata"]["uid"] != receipt["metadata"]["uid"]:
            from polyad.operator.controller import Pending

            raise Pending("activation receipt changed; refresh before execution")
        receipt.clear()
        receipt.update(latest)

    async def prepare(
        self,
        graph: Topology,
        prototypes: dict[str, asts.Resource],
        policies: dict[str, ActivationPolicy],
        definitions: dict[str, dict[str, Any]],
        children: list[dict[str, Any]],
    ) -> tuple[Topology, dict[str, asts.Resource]]:
        """
        Expand selected pulses into bounded execution vertices without bypassing graph admission.

        Args:
            graph (Topology): Original logical topology.
            prototypes (dict[str, asts.Resource]): Fully compiled, placement-aware execution templates.
            policies (dict[str, ActivationPolicy]): Pulse policy per logical node.
            definitions (dict[str, dict[str, Any]]): Fresh reusable target definitions.
            children (list[dict[str, Any]]): Refreshed parent-owned inventory.

        Returns:
            tuple[Topology, dict[str, asts.Resource]]: Runtime topology and desired execution objects.
        """
        self.policies = policies
        if policies and graph.mode != "persistent":
            raise ValueError("activation-controlled nodes require a persistent containing graph")
        now = datetime.now(UTC)
        receipts = [child for child in children if child["kind"] == "Activation" and not child["metadata"].get("deletionTimestamp")]
        executions = {
            child["metadata"].get("annotations", {}).get(ANNOTATION): child for child in children if child["kind"] != "Activation"
        }
        for receipt in receipts:
            if receipt["spec"]["node"] not in policies and receipt.get("status", {}).get("phase") not in TERMINAL:
                await self.save(receipt, "Failed", message="activation target or policy was removed")
        selected: dict[str, list[dict[str, Any]]] = {}
        for node, policy in policies.items():
            records = sorted(
                (receipt for receipt in receipts if receipt["spec"]["node"] == node),
                key=lambda receipt: (receipt["metadata"].get("creationTimestamp", ""), receipt["metadata"]["name"]),
            )
            for receipt in records:
                state = receipt.get("status", {})
                if state.get("phase") in TERMINAL:
                    continue
                intent, meta = receipt["spec"], self.obj["metadata"]
                valid = (
                    receipt["metadata"]["name"] == activation_name(intent["requestId"])
                    and intent["graphUid"] == meta["uid"]
                    and intent["graph"] == meta["name"]
                    and intent["kind"] == self.obj["kind"]
                    and intent["graphGeneration"] == meta.get("generation", 1)
                    and intent["definitionUid"] == definitions[node]["metadata"]["uid"]
                    and intent["definitionGeneration"] == definitions[node]["metadata"].get("generation", 1)
                )
                execution = executions.get(receipt["metadata"]["uid"])
                if not valid:
                    await self.save(receipt, "Failed", message="graph or definition revision changed")
                elif receipt["metadata"].get("annotations", {}).get(f"{asts.GROUP}/stop-requested") == "true":
                    await self.save(receipt, "Stopping" if execution else "Stopped", message="explicit stop requested")
                elif execution:
                    observation = observed(execution)
                    phase = (
                        "Failed"
                        if observation["failed"]
                        else "Completed"
                        if observation["completed"]
                        else "Ready"
                        if observation["ready"]
                        else "Running"
                    )
                    await self.save(
                        receipt,
                        phase,
                        execution={
                            "kind": execution["kind"],
                            "name": execution["metadata"]["name"],
                            "uid": execution["metadata"]["uid"],
                        },
                    )
            active = [receipt for receipt in records if receipt.get("status", {}).get("phase") in ACTIVE]
            pending = [receipt for receipt in records if receipt.get("status", {}).get("phase", "Pending") == "Pending"]
            if policy.mode == "Coalesce" and len(pending) > 1:
                for receipt in pending[:-1]:
                    await self.save(
                        receipt,
                        "Superseded",
                        message="coalesced into the newest pending pulse",
                        supersededBy=pending[-1]["spec"]["requestId"],
                    )
                pending = pending[-1:]
            limit = policy.maxConcurrent if policy.mode == "Parallel" else 1
            if policy.mode == "Reject":
                keep = max(0, limit - len(active))
                for receipt in pending[keep:]:
                    await self.save(receipt, "Rejected", message="target is busy")
                pending = pending[:keep]
            for receipt in pending[policy.maxPending :]:
                await self.save(receipt, "Rejected", message="activation queue limit exceeded")
            pending = pending[: policy.maxPending]
            # A terminating execution still occupies concurrency until its finalizers finish.
            draining = sum(
                receipt.get("status", {}).get("phase") in TERMINAL
                and receipt["metadata"]["uid"] in executions
                and not observed(executions[receipt["metadata"]["uid"]])["completed"]
                for receipt in records
            )
            for receipt in pending[: max(0, limit - len(active) - draining)]:
                await self.save(receipt, "Queued", message="selected; waiting for dependencies, gates, slots and rate bounds")
                active.append(receipt)
            pending = [receipt for receipt in pending if receipt.get("status", {}).get("phase") == "Pending"]
            admitted = [receipt["status"]["admittedAt"] for receipt in records if receipt.get("status", {}).get("admittedAt")]
            last = max(admitted, default=self.obj["metadata"]["creationTimestamp"])
            elapsed = max(0.0, (now - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds())
            overdue = policy.maxIntervalSeconds is not None and elapsed > policy.maxIntervalSeconds
            self.summary[node] = {
                "mode": policy.mode,
                "pending": len(pending),
                "active": len(active),
                "overdue": overdue,
                "lastAdmissionTime": max(admitted) if admitted else None,
                "completed": sum(receipt.get("status", {}).get("phase") == "Completed" for receipt in records),
                "failed": sum(receipt.get("status", {}).get("phase") in {"Failed", "Rejected"} for receipt in records),
            }
            if overdue and policy.onDeadline == "Activate" and not pending and len(active) < limit:
                timer_id = "timer-" + hashlib.sha256(f"{self.obj['metadata']['uid']}/{node}/{last}".encode()).hexdigest()[:40]
                await ActivationStore(self.controller.api, self.obj["metadata"]["namespace"]).submit(
                    ActivationRequest(
                        requestId=timer_id,
                        graph=self.obj["metadata"]["name"],
                        graphUid=self.obj["metadata"]["uid"],
                        node=node,
                        kind=self.obj["kind"],
                    )
                )
            selected[node] = [receipt for receipt in active if receipt.get("status", {}).get("phase") != "Stopping"]
            # Keep the latest successful execution observable until the next pulse replaces it.
            if not active and not pending:
                completed = [
                    receipt
                    for receipt in records
                    if receipt.get("status", {}).get("phase") == "Completed" and receipt["metadata"]["uid"] in executions
                ]
                if completed:
                    selected[node] = completed[-1:]

        aliases = {
            node.name: [self.key(node.name, receipt) for receipt in selected.get(node.name, [])] or [node.name] for node in graph.nodes
        }
        desired: dict[str, asts.Resource] = {}
        nodes = []
        for vertex in graph.nodes:
            requires = tuple(Dependency(name, edge.condition) for edge in vertex.requires for name in aliases[edge.node])
            if vertex.name not in policies or not selected[vertex.name]:
                nodes.append(evolve(vertex, requires=requires))
                desired[vertex.name] = prototypes[vertex.name]
                if vertex.name in policies:
                    self.blocked.add(vertex.name)
                continue
            for receipt in selected[vertex.name]:
                key = self.key(vertex.name, receipt)
                nodes.append(evolve(vertex, name=key, requires=requires))
                self.records[key] = receipt
                desired[key] = self.execution(prototypes[vertex.name], vertex.name, key, receipt)
                if receipt.get("status", {}).get("phase") in TERMINAL:
                    self.blocked.add(key)
        if len(nodes) > 4096 or sum(len(aliases[edge.source]) * len(aliases[edge.target]) for edge in graph.connections) > 16384:
            raise ValueError("expanded activation topology exceeds 4096 vertices or 16384 connections")
        connections = tuple(
            evolve(edge, source=source, target=target)
            for edge in graph.connections
            for source in aliases[edge.source]
            for target in aliases[edge.target]
        )
        # Network guards are compiled from logical names before this scheduling
        # projection. All executions retain those labels; runtime aliases must
        # never become new network identities or widen the original contract.
        return evolve(graph, nodes=tuple(nodes), connections=connections, network=None), desired

    def key(self, node: str, receipt: dict[str, Any]) -> str:
        """
        Name one execution vertex independently of application completion or retries.

        Args:
            node (str): Logical node name.
            receipt (dict[str, Any]): Persisted activation identity.

        Returns:
            str: DNS-safe execution vertex.
        """
        return node[:28] + "-" + hashlib.sha256(receipt["metadata"]["uid"].encode()).hexdigest()[:24]

    def execution(self, prototype: asts.Resource, node: str, key: str, receipt: dict[str, Any]) -> asts.Resource:
        """
        Clone a compiled resource with disjoint selectors and an audited pulse identity.

        Args:
            prototype (asts.Resource): Placement, storage and network-aware resource.
            node (str): Original graph vertex.
            key (str): Unique runtime vertex.
            receipt (dict[str, Any]): Activation receipt.

        Returns:
            asts.Resource: Execution with stable ownership and per-pulse identity.
        """
        document = asts.to_document(prototype)
        uid = receipt["metadata"]["uid"]
        if document["kind"] in {"Deployment", "StatefulSet"}:
            instance = hashlib.sha256(uid.encode()).hexdigest()[:32]
            document["spec"]["replicas"] = self.policies[node].replicasPerActivation
            document["spec"]["selector"]["matchLabels"][f"{asts.GROUP}/instance"] = instance
            document["spec"]["template"]["metadata"]["labels"][f"{asts.GROUP}/instance"] = instance
        meta = document["metadata"]
        meta["name"] = child_name(asts.converter.structure(self.obj["metadata"], asts.ObjectMeta), key)
        meta.setdefault("labels", {})[f"{asts.GROUP}/runtime-node"] = key
        annotations = meta.setdefault("annotations", {})
        annotations.update(
            {ANNOTATION: uid, f"{asts.GROUP}/activation-id": receipt["spec"]["requestId"], f"{asts.GROUP}/logical-node": node}
        )
        annotations[f"{asts.GROUP}/desired-hash"] = hashlib.sha256(
            f"{annotations[f'{asts.GROUP}/desired-hash']}/{uid}/{self.policies[node].replicasPerActivation}".encode()
        ).hexdigest()
        if document["kind"] in {"Job", "Deployment", "StatefulSet"}:
            inject_environment(
                document["spec"]["template"],
                {
                    "POLYAD_RESOURCE_NAME": meta["name"],
                    "POLYAD_RUNTIME_NODE_NAME": key,
                    "POLYAD_ACTIVATION_ID": receipt["spec"]["requestId"],
                    "POLYAD_ACTIVATION_UID": uid,
                },
            )
            document["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {}).update(
                {
                    ANNOTATION: uid,
                    f"{asts.GROUP}/activation-id": receipt["spec"]["requestId"],
                    f"{asts.GROUP}/logical-node": node,
                }
            )
        return asts.from_document(document)

    async def admit(self, key: str) -> bool:
        """
        Fence start frequency with a durable timestamp before creating a workload.

        Args:
            key (str): Candidate runtime vertex.

        Returns:
            bool: Whether dispatch is permitted on this pass.
        """
        if key in self.blocked:
            return False
        receipt = self.records.get(key)
        if receipt is None:
            return True
        if receipt.get("status", {}).get("admittedAt"):
            return True
        node = receipt["spec"]["node"]
        last = self.summary[node]["lastAdmissionTime"]
        now = datetime.now(UTC)
        if last and (now - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() < self.policies[node].minIntervalSeconds:
            return False
        await self.save(receipt, "Running", admittedAt=now.isoformat(), message="execution admitted")
        self.summary[node]["lastAdmissionTime"] = now.isoformat()
        self.summary[node]["overdue"] = False
        return True
