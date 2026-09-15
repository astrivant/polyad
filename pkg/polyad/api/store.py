"""
Store immutable API receipts and resolve audit IDs through refreshed Kubernetes reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kubernetes.client.exceptions import ApiException

from polyad.api.app import Conflict
from polyad.compiler import asts
from polyad.compiler.composition import COMPOSITION_KINDS, read_receipt, receipt_spec, request_name

if TYPE_CHECKING:
    from typing import Any

    from polyad.compiler.composition import CompositionRequest
    from polyad.operator.api import API


class CompositionStore:
    """
    Use Kubernetes as the durable source of accepted composition requests.
    """

    def __init__(self, api: API, namespace: str) -> None:
        """
        Bind one API adapter and the operator's namespace.

        Args:
            api (API): Adapter serializing receipt writes; graph writes use the leased worker.
            namespace (str): Fixed namespace for every request from this API instance.
        """
        self.api, self.namespace = api, namespace

    async def submit(self, request: CompositionRequest) -> dict[str, Any]:
        """
        Create an immutable receipt, recovering matching duplicates and lost acknowledgements.

        Args:
            request (CompositionRequest): Validated request with a client-chosen idempotency ID.

        Returns:
            dict[str, Any]: Persisted receipt identity and current observation status.
        """
        name = request_name(request.requestId)
        existing = await self.api.get("Composition", self.namespace, name)
        if existing is None:
            desired = asts.Composition(
                metadata=asts.ObjectMeta(
                    name=name,
                    namespace=self.namespace,
                    annotations={f"{asts.GROUP}/request-id": request.requestId, f"{asts.GROUP}/request-hash": request.digest()},
                    labels={f"{asts.GROUP}/request": name[12:]},
                ),
                spec=receipt_spec(request),
            )
            try:
                existing = await self.api.request("POST", "Composition", self.namespace, body=desired)
            except ApiException as error:
                if error.status != 409:
                    raise
                existing = await self.api.get("Composition", self.namespace, name)
                if existing is None:
                    raise
        if existing["metadata"].get("deletionTimestamp") or read_receipt(existing["spec"]).digest() != request.digest():
            raise Conflict("requestId already identifies different or deleting intent")
        return {
            "requestId": request.requestId,
            "kind": "Composition",
            "name": name,
            "namespace": self.namespace,
            "uid": existing["metadata"]["uid"],
            "status": existing.get("status", {"phase": "Pending"}),
        }

    async def lookup(self, request_id: str, audit: bool = False) -> dict[str, Any] | None:
        """
        Resolve a receipt and optionally list generated definitions, graph instances and pods.

        Args:
            request_id (str): Client request identity.
            audit (bool): Whether to include manifest references carrying this receipt's UID.

        Returns:
            dict[str, Any] | None: Fresh receipt status and bounded resource references, or None.
        """
        name = request_name(request_id)
        receipt = await self.api.get("Composition", self.namespace, name)
        if receipt is None:
            return None
        result = {
            "requestId": request_id,
            "name": name,
            "namespace": self.namespace,
            "uid": receipt["metadata"]["uid"],
            "status": receipt.get("status", {"phase": "Pending"}),
        }
        if not audit:
            return result
        resources = []
        truncated = False
        for kind in sorted(COMPOSITION_KINDS | {"Job", "Deployment", "Pod", "Service", "ConfigMap", "PersistentVolumeClaim"}):
            response = await self.api.request(
                "GET", kind, self.namespace, query=[("labelSelector", f"{asts.GROUP}/request={name[12:]}"), ("limit", "200")]
            )
            truncated |= bool(response.get("metadata", {}).get("continue"))
            for item in response.get("items", []):
                meta = item["metadata"]
                annotations = meta.get("annotations", {})
                if annotations.get(f"{asts.GROUP}/composition-uid") != receipt["metadata"]["uid"]:
                    continue
                resources.append(
                    {
                        "kind": kind,
                        "name": meta["name"],
                        "uid": meta["uid"],
                        "namespace": self.namespace,
                        "generation": meta.get("generation"),
                        "owners": meta.get("ownerReferences", []),
                        "trace": {
                            key.removeprefix(asts.GROUP + "/"): value
                            for key, value in annotations.items()
                            if key.startswith(asts.GROUP + "/")
                        },
                    }
                )
        return {**result, "resources": resources, "truncated": truncated}
