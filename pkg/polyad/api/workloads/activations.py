"""
Persist pulse receipts without performing workload mutations on HTTP threads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kubernetes.client.exceptions import ApiException

from polyad.api.http.errors import Conflict, Unavailable
from polyad.compiler.activation import activation_name
from polyad_types import resources as asts
from polyad_types.graphs.activation import ActivationPolicy
from polyad_types.serialization import converter

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad_types.api.requests import ActivationRequest


class ActivationStore:
    """
    Accept immutable namespace-scoped requests and retain idempotency after completion.
    """

    def __init__(self, api: API, namespace: str) -> None:
        """
        Bind the receipt adapter and namespace.

        Args:
            api (API): Serialized Kubernetes adapter.
            namespace (str): Namespace served by this API.
        """
        self.api, self.namespace = api, namespace

    async def submit(self, request: ActivationRequest) -> dict[str, Any]:
        """
        Record one pulse, recovering duplicate submissions and uncertain acknowledgements.

        Args:
            request (ActivationRequest): Graph incarnation and downstream vertex identity.

        Returns:
            dict[str, Any]: Durable receipt; admission is asynchronous.
        """
        name = activation_name(request.requestId)

        # Persist the submitted intent independently of later changes to the caller's model.
        intent = converter.unstructure(request)
        existing = await self.api.get("Activation", self.namespace, name)
        if existing is None:
            graph = await self.api.get(request.kind, self.namespace, request.graph)
            if graph is None or graph["metadata"]["uid"] != request.graphUid or graph["metadata"].get("deletionTimestamp"):
                raise Conflict("activation target graph incarnation is absent or deleting")
            if graph["spec"].get("templateOnly") or graph["spec"].get("suspend") or graph.get("status", {}).get("phase") == "Stopped":
                raise Conflict("activation target must be an executable, unsuspended graph")
            if request.kind != "ReplicaGroup" and graph["spec"].get("mode", "finite") != "persistent":
                raise ValueError("activation-controlled nodes require a persistent containing graph")
            if request.kind == "ReplicaGroup":
                from polyad_types.graphs.replication import replica_topology

                spec = dict(graph["spec"])
                source_ref = spec.get("replicaSource")
                if source_ref and spec.get("inheritReplicas", True):
                    source = await self.api.get("ReplicaGroup", self.namespace, source_ref["name"])
                    if not source or source["metadata"]["uid"] != source_ref["uid"] or source["metadata"].get("deletionTimestamp"):
                        raise Conflict("replica source incarnation is unavailable")
                    spec["replicas"] = source["spec"].get("replicas", 1)
                graph = {**graph, "spec": replica_topology(spec)}
            node = next((node for node in graph["spec"]["nodes"] if node["name"] == request.node), None)
            if node is None or node["kind"] == "Resource":
                raise ValueError("activation target must be a workload or graph vertex")
            definition = await self.api.get(node["kind"], self.namespace, node["ref"])
            if definition is None or definition["metadata"].get("deletionTimestamp") or "activation" not in definition["spec"]:
                raise ValueError("downstream definition must declare an activation policy")

            # Validate the opt-in activation contract before binding a receipt to this definition revision.
            converter.structure(definition["spec"]["activation"], ActivationPolicy)
            meta = graph["metadata"]
            desired = asts.Activation(
                metadata=asts.ObjectMeta(
                    name=name,
                    namespace=self.namespace,
                    labels={f"{asts.GROUP}/owner": meta["uid"], f"{asts.GROUP}/activation-node": request.node},
                    ownerReferences=(
                        asts.OwnerReference(
                            apiVersion=graph["apiVersion"],
                            kind=graph["kind"],
                            name=meta["name"],
                            uid=meta["uid"],
                            controller=True,
                            blockOwnerDeletion=True,
                        ),
                    ),
                ),
                spec={
                    **intent,
                    "graphGeneration": meta.get("generation", 1),
                    "definitionUid": definition["metadata"]["uid"],
                    "definitionGeneration": definition["metadata"].get("generation", 1),
                },
            )
            try:
                existing = await self.api.request("POST", "Activation", self.namespace, body=desired)
            except ApiException as error:
                if error.status != 409:
                    raise
                existing = await self.api.get("Activation", self.namespace, name)
                if existing is None:
                    raise
        if existing["metadata"].get("deletionTimestamp") or any(existing["spec"].get(key) != value for key, value in intent.items()):
            raise Conflict("requestId already identifies another activation or a deleting receipt")
        return self.receipt(existing)

    def receipt(self, value: dict[str, Any]) -> dict[str, Any]:
        """
        Expose receipt identity and lifecycle without application secrets.

        Args:
            value (dict[str, Any]): Stored Activation resource.

        Returns:
            dict[str, Any]: API response envelope.
        """
        return {
            "requestId": value["spec"]["requestId"],
            "kind": "Activation",
            "name": value["metadata"]["name"],
            "namespace": self.namespace,
            "uid": value["metadata"]["uid"],
            "target": {key: value["spec"][key] for key in ("kind", "graph", "graphUid", "node")},
            "status": value.get("status", {"phase": "Pending"}),
        }

    async def lookup(self, request_id: str) -> dict[str, Any] | None:
        """
        Read a pulse's latest admission decision and execution identity.

        Args:
            request_id (str): Stable client request ID.

        Returns:
            dict[str, Any] | None: Current receipt or absence.
        """
        value = await self.api.get("Activation", self.namespace, activation_name(request_id))
        return self.receipt(value) if value else None

    async def stop(self, request_id: str) -> dict[str, Any] | None:
        """
        Persist an idempotent stop signal while retaining the original request identity.

        Args:
            request_id (str): Receipt whose running or queued execution should stop.

        Returns:
            dict[str, Any] | None: Receipt after the signal, or absence.
        """
        for _ in range(5):
            value = await self.api.get("Activation", self.namespace, activation_name(request_id))
            if value is None:
                return None
            try:
                await self.api.request(
                    "PATCH",
                    "Activation",
                    self.namespace,
                    value["metadata"]["name"],
                    {
                        "metadata": {
                            "resourceVersion": value["metadata"]["resourceVersion"],
                            "annotations": {f"{asts.GROUP}/stop-requested": "true"},
                        },
                    },
                )
                return {**self.receipt(value), "stopRequested": True}
            except ApiException as error:
                if error.status != 409:
                    raise
        raise Unavailable("activation changed repeatedly; retry the same stop request")
