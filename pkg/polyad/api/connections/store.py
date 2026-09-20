"""
Authenticate service accounts and persist immutable connection receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from functools import cached_property
from typing import TYPE_CHECKING, Any, Literal

from attrs import field, frozen
from kubernetes.client.exceptions import ApiException

from polyad.exceptions.api import Conflict, Forbidden, Unauthorized, Unavailable
from polyad.graph.temporary import deadline
from polyad.operator.coordination.pulses import PulsePolicy
from polyad_types import resources as asts
from polyad_types.api.requests import MAX_TTL
from polyad_types.graphs.topology import topology
from polyad_types.serialization import converter

if TYPE_CHECKING:
    from polyad.operator.adapters.kubernetes import API
    from polyad.operator.clusters.federation import Federation
    from polyad_types.api.requests import ConnectionRequest, ConnectionResponse, ServiceConnectionRequest

__all__ = (
    "AUDIENCE",
    "Caller",
    "ConnectionSettings",
    "ConnectionStore",
    "FINALIZER",
    "receipt_name",
)


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
        pulse_cooldown (float): Shared cooldown window for new proposals and positive responses; zero disables it.
        pulse_burst (int): New pulses admitted per graph or responding endpoint during one window.
    """

    operator_namespace: str
    scope: Literal["Cluster", "OperatorNamespace", "Namespace"] = "Cluster"
    namespace: str = ""
    max_ttl: int = 3600
    retention: int = 3600
    pulse_cooldown: float = 0
    pulse_burst: int = 1

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
        PulsePolicy(self.pulse_cooldown, self.pulse_burst)

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
            pulse_cooldown=float(os.environ.get("POLYAD_CONNECTION_PULSE_COOLDOWN_SECONDS", "0")),
            pulse_burst=int(os.environ.get("POLYAD_CONNECTION_PULSE_BURST", "1")),
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
        cluster (str): Token issuer's registered cluster; empty is the local cluster.
    """

    username: str
    uid: str
    namespace: str
    groups: tuple[str, ...] = ()
    extra: dict[str, Any] = field(factory=dict)
    cluster: str = ""


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
    identity = f"{caller.cluster}\0{caller.uid}" if caller.cluster else caller.uid
    return "connection-" + hashlib.sha256(f"{identity}\0{request_id}".encode()).hexdigest()[:40]


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

    @cached_property
    def federation(self) -> Federation:
        """
        Load registered destinations only when cross-cluster negotiation is used.

        Returns:
            Federation: Dedicated intake transports with no caller-supplied kubeconfigs.
        """
        from polyad.operator.clusters.federation import Federation

        return Federation(self.api)

    def resolve(self, cluster: str) -> tuple[API, str]:
        """
        Reject cluster requests this operator cannot fulfill instead of forwarding them.

        Args:
            cluster (str): Explicit registered cluster identity.

        Returns:
            tuple[API, str]: Local or registered remote transport and namespace.
        """
        if not cluster or cluster == self.federation.name:
            return self.api, self.settings.operator_namespace
        if cluster not in self.federation.clusters:
            raise Unavailable("this operator cannot fulfill requests for the selected cluster")
        return self.federation.target(cluster)

    async def authenticate(self, token: str, cluster: str = "") -> Caller:
        """
        Verify a projected service-account token using its dedicated audience.

        Args:
            token (str): Bearer credential; never persisted or logged.
            cluster (str): Token issuer's registered cluster; verified using that cluster's TokenReview API.

        Returns:
            Caller: Kubernetes-verified identity within the configured scope.
        """
        from polyad.auth.policy import public_demo

        if cluster and cluster != os.environ.get("POLYAD_CLUSTER_NAME", ""):
            remote, namespace = self.resolve(cluster)
            identity = await ConnectionStore(remote, self.settings).authenticate(token)
            if identity.namespace != namespace:
                raise Forbidden("caller is outside this child operator's registered namespace")
            return Caller(identity.username, identity.uid, identity.namespace, identity.groups, identity.extra, cluster)

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

    async def authorize(self, caller: Caller, namespace: str, kind: str, graph: str, *, verb: str = "connect") -> None:
        """
        Require the requested verb on the exact target graph without granting CRD writes.

        Args:
            caller (Caller): Verified service account.
            namespace (str): Target namespace.
            kind (str): Target boundary kind.
            graph (str): Target graph name.
            verb (str): Separate connect or approve permission on this exact graph.

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
                        "verb": verb,
                    },
                },
            },
        )
        if review.get("status", {}).get("allowed") is not True:
            raise Forbidden(f"caller requires the {verb} verb on the target graph")

    async def submit(self, request: ConnectionRequest, caller: Caller) -> dict[str, Any]:
        """
        Persist one immutable connection request with graph and caller identity fences.

        Args:
            request (ConnectionRequest): Validated connection intent.
            caller (Caller): Verified service account.

        Returns:
            dict[str, Any]: Durable receipt; network admission remains asynchronous.
        """
        from polyad.events.access import configuration
        from polyad_types.api.discovery import AccessMode

        if configuration().effective(caller.cluster or os.environ.get("POLYAD_CLUSTER_NAME", ""), "connections") == AccessMode.DISABLED:
            raise Forbidden("connection requests are disabled by this operator's access mode")
        if request.peers:
            from polyad.api.connections.cross import authorize_request

            await authorize_request(self, request, caller)
        else:
            if caller.cluster:
                raise Forbidden("a remote participant must use an atlas service connection request")
            await self.authorize(caller, request.namespace, request.kind, request.graph)
        if request.ttlSeconds > self.settings.max_ttl:
            raise ValueError("ttlSeconds exceeds the configured maximum")
        name = receipt_name(request.requestId, caller)
        requester = {"username": caller.username, "uid": caller.uid, **({"cluster": caller.cluster} if caller.cluster else {})}
        intent = {**converter.unstructure(request), "requester": requester}
        existing = await self.api.get("TemporaryConnection", request.namespace, name)
        if existing is None:
            graph = await self.api.get(request.kind, request.namespace, request.graph)
            if graph is None or graph["metadata"]["uid"] != request.graphUid or graph["metadata"].get("deletionTimestamp"):
                raise Conflict("target graph incarnation is absent or deleting")
            if graph["spec"].get("templateOnly") or graph["spec"].get("suspend") or graph.get("status", {}).get("phase") == "Stopped":
                raise Conflict("temporary connections require an executable, unsuspended graph instance")
            spec = graph["spec"]
            if request.kind == "ReplicaGroup":
                from polyad.operator.reconciliation.replication import effective_spec

                spec, _ = await effective_spec(self.api, graph)
            nodes = {node.name for node in topology(spec, request.kind).nodes}
            if request.source not in nodes or request.target not in nodes:
                raise ValueError("temporary connection endpoints must exist in the target boundary")
            await self.pulse(f"request:{request.namespace}:{request.graphUid}")
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
        from polyad.api.connections.consent import TERMINAL, endpoint

        if existing.get("status", {}).get("phase") not in TERMINAL and datetime.now(UTC) < deadline(existing):
            try:
                node = await endpoint(self.api, caller, existing, resolve=self.resolve)
            except Forbidden:
                node = None  # A third-party requester cannot consent for either service.
            if node is not None:
                existing = await self.record(existing, caller, node, "Approve", verb="connect", implicit=True)
        return self.receipt(existing)

    async def record(
        self, value: dict[str, Any], caller: Caller, node: str, decision: str, *, verb: str, implicit: bool = False
    ) -> dict[str, Any]:
        """
        Persist one endpoint's consent with optimistic concurrency and no TTL extension.

        Args:
            value (dict[str, Any]): Current receipt with its original UID fence.
            caller (Caller): Verified endpoint representative.
            node (str): Endpoint resolved from current Pod ownership.
            decision (str): Approve or Reject.
            verb (str): Permission establishing this consent.
            implicit (bool): Requester consent must never replace a later explicit response.

        Returns:
            dict[str, Any]: Updated or idempotently acknowledged receipt.
        """
        from polyad.api.connections.consent import CONSENTS, TERMINAL, decisions, endpoint

        name, namespace, uid = (value["metadata"][key] for key in ("name", "namespace", "uid"))
        extra = {key: caller.extra[key] for key in ("authentication.kubernetes.io/pod-name", "authentication.kubernetes.io/pod-uid")}
        charged = False
        for _ in range(5):
            if value["metadata"]["uid"] != uid or value["metadata"].get("deletionTimestamp") or datetime.now(UTC) >= deadline(value):
                raise Conflict("connection proposal was replaced, deleted or expired")
            if (
                value.get("status", {}).get("phase") in TERMINAL
                or value["metadata"].get("annotations", {}).get(f"{asts.GROUP}/revoke-requested") == "true"
            ):
                raise Conflict("connection is terminal or being revoked")
            current = decisions(value)
            if node in current:
                previous = current[node]
                if (implicit and previous["verb"] != "connect") or (
                    previous["decision"] == decision
                    and previous["uid"] == caller.uid
                    and previous["extra"] == extra
                    and previous.get("cluster", "") == caller.cluster
                ):
                    return value
                if previous["decision"] == "Reject":
                    raise Conflict("a rejected proposal requires a new requestId")
            await self.authorize_receipt(caller, value, verb=verb)
            if await endpoint(self.api, caller, value, resolve=self.resolve) != node:
                raise Forbidden("connection participant changed before recording consent")

            # Keep only verified Pod claims, never a bearer token or arbitrary authentication extras.
            current[node] = {
                "receiptUid": uid,
                "decision": decision,
                "verb": verb,
                "username": caller.username,
                "uid": caller.uid,
                "cluster": caller.cluster,
                "namespace": caller.namespace,
                "groups": list(caller.groups),
                "extra": extra,
                "observedAt": datetime.now(UTC).isoformat(),
            }
            if not implicit and decision == "Approve" and not charged:
                await self.pulse(f"response:{namespace}:{value['spec']['graphUid']}:{node}")
                charged = True
            try:
                updated: dict[str, Any] = await self.api.request(
                    "PATCH",
                    "TemporaryConnection",
                    namespace,
                    name,
                    {"metadata": {"resourceVersion": value["metadata"]["resourceVersion"], "annotations": {CONSENTS: json.dumps(current)}}},
                )
                return updated
            except ApiException as error:
                if error.status != 409:
                    raise
                refreshed = await self.api.get("TemporaryConnection", namespace, name)
                if refreshed is None:
                    raise Conflict("connection proposal no longer exists") from error
                value = refreshed
        raise Unavailable("connection changed repeatedly; retry the same response")

    async def pulse(self, identity: str) -> None:
        """
        Apply the optional shared negotiation budget without slowing revocation or expiry.

        Args:
            identity (str): Graph-wide proposal lane or graph-endpoint response lane.

        Returns:
            None: Replayed receipts and unchanged decisions bypass new-pulse accounting.
        """
        from polyad.auth.policy import public_demo

        if self.settings.pulse_cooldown == 0 or public_demo():
            return
        from polyad.cache import Cache, cache_url

        cache = Cache(cache_url(), self.settings.operator_namespace)
        try:
            await PulsePolicy(self.settings.pulse_cooldown, self.settings.pulse_burst).admit(cache.client, f"connections:{identity}")
        finally:
            await cache.close()

    async def respond(self, namespace: str, name: str, response: ConnectionResponse, caller: Caller) -> dict[str, Any] | None:
        """
        Accept consent only from a permitted Pod belonging to the proposed endpoint.

        Args:
            namespace (str): Receipt namespace from the event.
            name (str): Server-assigned receipt name from the event, not the requester's private ID.
            response (ConnectionResponse): Receipt UID and explicit decision.
            caller (Caller): TokenReview-verified responding service.

        Returns:
            dict[str, Any] | None: Public receipt after recording consent, or absence.
        """
        from polyad.api.connections.consent import endpoint

        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace) or not re.fullmatch(r"connection-[a-f0-9]{40}", name):
            raise ValueError("invalid connection proposal identity")
        if not self.settings.allows(caller.namespace) or not self.settings.allows(namespace):
            raise Forbidden("caller and target must be inside the configured connections scope")
        value = await self.api.get("TemporaryConnection", namespace, name)
        if value is None:
            return None
        if value["metadata"]["uid"] != response.uid:
            raise Conflict("connection proposal UID changed")
        if response.decision == "Approve" and value["spec"].get("peers"):
            from polyad.api.connections.cross import connection_request
            from polyad_types.api.requests import ConnectionRequest, ServiceConnectionRequest

            intent = converter.structure({key: item for key, item in value["spec"].items() if key != "requester"}, ConnectionRequest)
            refreshed = await connection_request(
                self,
                ServiceConnectionRequest(
                    intent.requestId, intent.peers["source"], intent.peers["target"], intent.ttlSeconds, intent.ports, intent.bidirectional
                ),
            )
            if refreshed != intent:
                raise Conflict("proposal ancestry changed before consent")
        await self.authorize_receipt(caller, value, verb="approve")
        node = await endpoint(self.api, caller, value, resolve=self.resolve)
        return self.receipt(await self.record(value, caller, node, response.decision, verb="approve"))

    @staticmethod
    def receipt(value: dict[str, Any]) -> dict[str, Any]:
        """
        Expose a connection's immutable deadline and observed admission state.

        Args:
            value (dict[str, Any]): Stored TemporaryConnection resource.

        Returns:
            dict[str, Any]: Public response without credentials or workload templates.
        """
        from polyad.api.connections.consent import decisions

        return {
            "requestId": value["spec"]["requestId"],
            "name": value["metadata"]["name"],
            "namespace": value["metadata"]["namespace"],
            "uid": value["metadata"]["uid"],
            "expiresAt": deadline(value).isoformat(),
            "target": {key: value["spec"][key] for key in ("kind", "graph", "graphUid", "source", "target", "ports", "bidirectional")},
            "status": value.get("status", {"phase": "Pending"}),
            "consent": {node: entry["decision"] for node, entry in decisions(value).items()},
            "peers": value["spec"].get("peers", {}),
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
            if value["spec"]["requester"] != {
                "username": caller.username,
                "uid": caller.uid,
                **({"cluster": caller.cluster} if caller.cluster else {}),
            }:
                raise Forbidden("connection belongs to a different service-account incarnation")
            await self.authorize_receipt(caller, value)
        return value

    async def authorize_receipt(self, caller: Caller, value: dict[str, Any], *, verb: str = "connect") -> None:
        """
        Check permissions in the participant's verified home cluster.

        Args:
            caller (Caller): Verified projected-token identity.
            value (dict[str, Any]): Immutable proposal with exact endpoint references.
            verb (str): Connect or approve permission.

        Returns:
            None: Remote names cannot impersonate same-named local service accounts.
        """
        from polyad.api.connections.consent import endpoint

        peers = value["spec"].get("peers", {})
        if not peers:
            if caller.cluster:
                raise Forbidden("remote identity cannot authorize a local connection receipt")
            await self.authorize(caller, value["metadata"]["namespace"], value["spec"]["kind"], value["spec"]["graph"], verb=verb)
            return
        node = await endpoint(self.api, caller, value, resolve=self.resolve)
        side = "source" if node == value["spec"]["source"] else "target"
        peer = peers[side]
        remote, _ = self.resolve(peer["cluster"])
        await ConnectionStore(remote, self.settings).authorize(caller, peer["namespace"], peer["kind"], peer["graph"], verb=verb)

    async def connect_services(self, request: ServiceConnectionRequest, caller: Caller) -> dict[str, Any]:
        """
        Resolve a discovered peer pair to a common boundary this operator owns.

        Args:
            request (ServiceConnectionRequest): Exact discovered service identities.
            caller (Caller): Verified source service identity.

        Returns:
            dict[str, Any]: Durable proposal, pending the target's explicit consent.
        """
        from polyad.api.connections.cross import connection_request

        return await self.submit(await connection_request(self, request), caller)

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
