"""
Authenticate service accounts and persist immutable connection receipts.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import TYPE_CHECKING, Any, Literal

from attrs import field, frozen
from kubernetes.client.exceptions import ApiException

from polyad.api.errors import Conflict, Forbidden, Unauthorized, Unavailable
from polyad.graph.temporary import deadline
from polyad_types import resources as asts
from polyad_types.codec import converter
from polyad_types.requests import MAX_TTL
from polyad_types.topology import topology

if TYPE_CHECKING:
    from polyad.operator.api import API
    from polyad_types.requests import ConnectionRequest

FINALIZER = f"{asts.GROUP}/temporary-connection"
AUDIENCE = "polyad-connections"


@frozen
class ConnectionSettings:
    """
    Bound endpoint visibility and connection lifetimes.

    Attributes:
        operator_namespace (str): Namespace hosting this operator.
        scope (Literal['Cluster', 'OperatorNamespace', 'Namespace']): Allowed caller and target scope.
        namespace (str): Explicit namespace when scope is Namespace.
        max_ttl (int): Maximum request lifetime in seconds.
        retention (int): Terminal receipt retention in seconds.
    """

    operator_namespace: str
    scope: Literal["Cluster", "OperatorNamespace", "Namespace"] = "Cluster"
    namespace: str = ""
    max_ttl: int = 3600
    retention: int = 3600

    def __attrs_post_init__(self) -> None:
        """
        Reject ambiguous scope and unbounded duration settings.

        Returns:
            None: Invalid settings prevent listener startup.
        """
        if self.scope not in {"Cluster", "OperatorNamespace", "Namespace"}:
            raise ValueError("connections scope must be Cluster, OperatorNamespace or Namespace")
        if bool(self.namespace) != (self.scope == "Namespace"):
            raise ValueError("connections namespace is required only with Namespace scope")
        for value in (self.operator_namespace, self.namespace or self.operator_namespace):
            if len(value) > 63 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value):
                raise ValueError("connections namespaces must be DNS labels")
        if type(self.max_ttl) is not int or not 1 <= self.max_ttl <= MAX_TTL:
            raise ValueError("connections max TTL must be from 1 through 86400 seconds")
        if type(self.retention) is not int or not 0 <= self.retention <= 604800:
            raise ValueError("connections receipt retention must be from 0 through 604800 seconds")

    @classmethod
    def from_environment(cls, namespace: str) -> ConnectionSettings:
        """
        Load optional connection settings for one operator process.

        Args:
            namespace (str): Operator namespace.

        Returns:
            ConnectionSettings: Validated endpoint policy.
        """
        return cls(
            namespace,
            scope=os.environ.get("POLYAD_CONNECTIONS_SCOPE", "Cluster"),  # type: ignore[arg-type]
            namespace=os.environ.get("POLYAD_CONNECTIONS_NAMESPACE", ""),
            max_ttl=int(os.environ.get("POLYAD_CONNECTIONS_MAX_TTL", "3600")),
            retention=int(os.environ.get("POLYAD_CONNECTIONS_RETENTION", "3600")),
        )

    def allows(self, namespace: str) -> bool:
        """
        Match a caller or target namespace against the selected scope.

        Args:
            namespace (str): Namespace from verified identity or validated input.

        Returns:
            bool: Whether the namespace belongs to the configured scope.
        """
        return self.scope == "Cluster" or namespace == (self.operator_namespace if self.scope == "OperatorNamespace" else self.namespace)


@frozen
class Caller:
    """
    Retain only Kubernetes-verified service-account identity for authorization.

    Attributes:
        username (str): Verified system:serviceaccount identity.
        uid (str): Service-account incarnation from TokenReview.
        namespace (str): Namespace parsed from the verified username.
        groups (tuple[str, ...]): Verified groups for SubjectAccessReview.
        extra (dict[str, Any]): Verified additional authentication attributes.
    """

    username: str
    uid: str
    namespace: str
    groups: tuple[str, ...] = ()
    extra: dict[str, Any] = field(factory=dict)


def receipt_name(request_id: str, caller: Caller) -> str:
    """
    Isolate idempotency keys between service-account incarnations.

    Args:
        request_id (str): Client idempotency key.
        caller (Caller): Verified caller identity.

    Returns:
        str: Stable Kubernetes receipt name.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
        raise ValueError("invalid requestId")
    return "connection-" + hashlib.sha256(f"{caller.uid}\0{request_id}".encode()).hexdigest()[:40]


class ConnectionStore:
    """
    Accept durable intent while leaving graph mutations to the owning operator shard.
    """

    def __init__(self, api: API, settings: ConnectionSettings) -> None:
        """
        Bind Kubernetes transport and endpoint scope.

        Args:
            api (API): Dedicated intake adapter, separate from workload writes.
            settings (ConnectionSettings): Validated caller, target and TTL policy.
        """
        self.api, self.settings = api, settings

    async def authenticate(self, token: str) -> Caller:
        """
        Verify a projected service-account token using its dedicated audience.

        Args:
            token (str): Bearer credential; never persisted or logged.

        Returns:
            Caller: Kubernetes-verified identity within the configured scope.
        """
        from polyad.auth.policy import public_demo

        if public_demo():
            namespace = self.settings.namespace if self.settings.scope == "Namespace" else self.settings.operator_namespace
            return Caller(f"system:serviceaccount:{namespace}:polyad-demo", "polyad-demo", namespace)
        if not token or len(token) > 16384:
            raise Unauthorized("a projected service-account token is required")
        review = await self.api.request(
            "POST",
            "TokenReview",
            "",
            body={"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview", "spec": {"token": token, "audiences": [AUDIENCE]}},
        )
        status = review.get("status", {})
        user = status.get("user", {})
        match = re.fullmatch(r"system:serviceaccount:([a-z0-9-]+):([a-z0-9.-]+)", user.get("username", ""))
        if status.get("authenticated") is not True or AUDIENCE not in status.get("audiences", []) or not match or not user.get("uid"):
            raise Unauthorized("invalid service-account token or audience")
        caller = Caller(user["username"], user["uid"], match[1], tuple(user.get("groups", [])), user.get("extra", {}))
        if not self.settings.allows(caller.namespace):
            raise Forbidden("caller namespace is outside the configured connections scope")
        return caller

    async def authorize(self, caller: Caller, namespace: str, kind: str, graph: str) -> None:
        """
        Require the connect verb on the exact target graph without granting CRD writes.

        Args:
            caller (Caller): Verified service account.
            namespace (str): Target namespace.
            kind (str): Target boundary kind.
            graph (str): Target graph name.

        Returns:
            None: Scope or Kubernetes authorization failures raise Forbidden.
        """
        if not self.settings.allows(caller.namespace) or not self.settings.allows(namespace):
            raise Forbidden("caller and target must be inside the configured connections scope")
        from polyad.auth.policy import public_demo

        if public_demo():
            return
        review = await self.api.request(
            "POST",
            "SubjectAccessReview",
            "",
            body={
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SubjectAccessReview",
                "spec": {
                    "user": caller.username,
                    "uid": caller.uid,
                    "groups": list(caller.groups),
                    "extra": caller.extra,
                    "resourceAttributes": {
                        "namespace": namespace,
                        "group": asts.GROUP,
                        "resource": asts.RESOURCE_TYPES[kind].plural,
                        "name": graph,
                        "verb": "connect",
                    },
                },
            },
        )
        if review.get("status", {}).get("allowed") is not True:
            raise Forbidden("caller requires the connect verb on the target graph")

    async def submit(self, request: ConnectionRequest, caller: Caller) -> dict[str, Any]:
        """
        Persist one immutable connection request with graph and caller identity fences.

        Args:
            request (ConnectionRequest): Validated connection intent.
            caller (Caller): Verified service account.

        Returns:
            dict[str, Any]: Durable receipt; network admission remains asynchronous.
        """
        await self.authorize(caller, request.namespace, request.kind, request.graph)
        if request.ttlSeconds > self.settings.max_ttl:
            raise ValueError("ttlSeconds exceeds the configured maximum")
        name = receipt_name(request.requestId, caller)
        intent = {**converter.unstructure(request), "requester": {"username": caller.username, "uid": caller.uid}}
        existing = await self.api.get("TemporaryConnection", request.namespace, name)
        if existing is None:
            graph = await self.api.get(request.kind, request.namespace, request.graph)
            if graph is None or graph["metadata"]["uid"] != request.graphUid or graph["metadata"].get("deletionTimestamp"):
                raise Conflict("target graph incarnation is absent or deleting")
            if graph["spec"].get("templateOnly") or graph["spec"].get("suspend") or graph.get("status", {}).get("phase") == "Stopped":
                raise Conflict("temporary connections require an executable, unsuspended graph instance")
            spec = graph["spec"]
            if request.kind == "ReplicaGroup":
                from polyad.operator.replication import effective_spec

                spec, _ = await effective_spec(self.api, graph)
            nodes = {node.name for node in topology(spec, request.kind).nodes}
            if request.source not in nodes or request.target not in nodes:
                raise ValueError("temporary connection endpoints must exist in the target boundary")
            desired = asts.TemporaryConnection(
                metadata=asts.ObjectMeta(
                    name=name,
                    namespace=request.namespace,
                    labels={f"{asts.GROUP}/owner": request.graphUid},
                    finalizers=(FINALIZER,),
                    ownerReferences=(
                        asts.OwnerReference(
                            apiVersion=graph["apiVersion"],
                            kind=request.kind,
                            name=request.graph,
                            uid=request.graphUid,
                            controller=True,
                            blockOwnerDeletion=False,
                        ),
                    ),
                ),
                spec=intent,
            )
            try:
                existing = await self.api.request("POST", "TemporaryConnection", request.namespace, body=desired)
            except ApiException as error:
                if error.status != 409:
                    raise
                existing = await self.api.get("TemporaryConnection", request.namespace, name)
                if existing is None:
                    raise
        if existing["metadata"].get("deletionTimestamp") or existing["spec"] != intent:
            raise Conflict("requestId already identifies a different or deleting connection")
        return self.receipt(existing)

    @staticmethod
    def receipt(value: dict[str, Any]) -> dict[str, Any]:
        """
        Expose a connection's immutable deadline and observed admission state.

        Args:
            value (dict[str, Any]): Stored TemporaryConnection resource.

        Returns:
            dict[str, Any]: Public response without credentials or workload templates.
        """
        return {
            "requestId": value["spec"]["requestId"],
            "name": value["metadata"]["name"],
            "namespace": value["metadata"]["namespace"],
            "uid": value["metadata"]["uid"],
            "expiresAt": deadline(value).isoformat(),
            "target": {key: value["spec"][key] for key in ("kind", "graph", "graphUid", "source", "target", "ports", "bidirectional")},
            "status": value.get("status", {"phase": "Pending"}),
            "revokeRequested": value["metadata"].get("annotations", {}).get(f"{asts.GROUP}/revoke-requested") == "true",
        }

    async def read(self, namespace: str, request_id: str, caller: Caller) -> dict[str, Any] | None:
        """
        Read only this service account's receipts within the permitted namespace.

        Args:
            namespace (str): Receipt namespace.
            request_id (str): Client idempotency key.
            caller (Caller): Verified caller.

        Returns:
            dict[str, Any] | None: Owned receipt or absence after authorization.
        """
        if len(namespace) > 63 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace):
            raise ValueError("invalid namespace")
        if not self.settings.allows(caller.namespace) or not self.settings.allows(namespace):
            raise Forbidden("caller and target must be inside the configured connections scope")
        value = await self.api.get("TemporaryConnection", namespace, receipt_name(request_id, caller))
        if value is not None:
            if value["spec"]["requester"] != {"username": caller.username, "uid": caller.uid}:
                raise Forbidden("connection belongs to a different service-account incarnation")
            await self.authorize(caller, namespace, value["spec"]["kind"], value["spec"]["graph"])
        return value

    async def lookup(self, namespace: str, request_id: str, caller: Caller) -> dict[str, Any] | None:
        """
        Return the caller's observed connection state.

        Args:
            namespace (str): Receipt namespace.
            request_id (str): Client idempotency key.
            caller (Caller): Verified caller.

        Returns:
            dict[str, Any] | None: Public receipt or absence.
        """
        value = await self.read(namespace, request_id, caller)
        return self.receipt(value) if value else None

    async def revoke(self, namespace: str, request_id: str, caller: Caller) -> dict[str, Any] | None:
        """
        Request early removal without losing retry identity before cleanup finishes.

        Args:
            namespace (str): Receipt namespace.
            request_id (str): Client idempotency key.
            caller (Caller): Verified caller.

        Returns:
            dict[str, Any] | None: Receipt with a durable revocation request, or absence.
        """
        for _ in range(5):
            value = await self.read(namespace, request_id, caller)
            if value is None:
                return None
            try:
                await self.api.request(
                    "PATCH",
                    "TemporaryConnection",
                    namespace,
                    value["metadata"]["name"],
                    {
                        "metadata": {
                            "resourceVersion": value["metadata"]["resourceVersion"],
                            "annotations": {f"{asts.GROUP}/revoke-requested": "true"},
                        }
                    },
                )
                return {**self.receipt(value), "revokeRequested": True}
            except ApiException as error:
                if error.status != 409:
                    raise
        raise Unavailable("connection changed repeatedly; retry the same revocation")
