"""
Describe composition, activation and temporary connection API requests.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from attrs import field, frozen

from polyad_types import resources as asts
from polyad_types.api.discovery import ServiceEndpoint
from polyad_types.networking.access import NetworkPort
from polyad_types.resources.registry import COMPOSABLE_KINDS
from polyad_types.serialization import converter

__all__ = (
    "ActivationRequest",
    "COMPOSITION_KINDS",
    "CompositionItem",
    "CompositionRequest",
    "ConnectionRequest",
    "ConnectionResponse",
    "MAX_TTL",
    "ServiceConnectionRequest",
    "identity",
)


COMPOSITION_KINDS = COMPOSABLE_KINDS
MAX_TTL = 86400


@frozen
class ConnectionResponse:
    """
    Respond to an immutable connection proposal as an authenticated endpoint.

    Attributes:
        uid (str): Receipt incarnation obtained from the connection event.
        decision (Literal['Approve', 'Reject']): Explicit service consent or refusal.
    """

    uid: str = field(metadata={"schema": {"minLength": 1, "maxLength": 128}})
    decision: Literal["Approve", "Reject"]

    def __attrs_post_init__(self) -> None:
        """
        Reject missing receipt fences and unrecognized decisions.

        Returns:
            None: The service identity comes exclusively from verified authentication.
        """
        if not isinstance(self.uid, str) or not 1 <= len(self.uid) <= 128 or self.decision not in {"Approve", "Reject"}:
            raise ValueError("connection responses require a receipt UID and Approve or Reject decision")


def identity(value: str) -> str:
    """
    Validate a portable ID usable in node references and audit paths.

    Args:
        value (str): Client-generated identifier, commonly a UUID or a short slug.

    Returns:
        str: Validated identifier.
    """
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", value):
        raise ValueError("IDs must be lowercase DNS labels of at most 63 characters")
    return value


@frozen
class CompositionItem:
    """
    Supply one reusable definition in a request-local ID namespace.

    Attributes:
        id (str): Request-local definition ID.
        kind (str): Supported graph or workload definition kind.
        spec (dict[str, Any]): Definition configuration, using ID references for graph nodes.
    """

    id: str
    kind: str
    spec: dict[str, Any]

    def __attrs_post_init__(self) -> None:
        """
        Reject unsafe identifiers and kinds outside the composition surface.

        Returns:
            None: No return value.
        """
        identity(self.id)
        if self.kind not in COMPOSITION_KINDS:
            raise ValueError(f"composition cannot create kind {self.kind}")


@frozen
class CompositionRequest:
    """
    Describe an immutable composition with reusable definitions and one executable root.

    Attributes:
        requestId (str): Idempotency identity across replica handoff and HTTP retries.
        rootId (str): ID of the executable root boundary.
        objects (tuple[CompositionItem, ...]): Reusable graph and workload definitions.
    """

    requestId: str
    rootId: str
    objects: tuple[CompositionItem, ...]

    def __attrs_post_init__(self) -> None:
        """
        Bound request size and require unique IDs with an executable graph root.

        Returns:
            None: No return value.
        """
        identity(self.requestId)
        by_id = {item.id: item for item in self.objects}
        if not 1 <= len(self.objects) <= 128 or len(by_id) != len(self.objects):
            raise ValueError("composition requires 1 to 128 definitions with unique IDs")
        if self.rootId not in by_id or by_id[self.rootId].kind not in asts.BOUNDARY_KINDS:
            raise ValueError("composition rootId must identify a graph boundary")

    def digest(self) -> str:
        """
        Hash the immutable request independently of object ordering.

        Returns:
            str: Canonical content digest used to reject conflicting ID reuse.
        """
        document = converter.unstructure(self)
        document["objects"].sort(key=lambda item: item["id"])
        return hashlib.sha256(json.dumps(document, sort_keys=True, allow_nan=False).encode()).hexdigest()


@frozen
class ActivationRequest:
    """
    Request one execution of an activation-controlled graph vertex.

    Attributes:
        requestId (str): Idempotency identity retained for the receipt lifetime.
        graph (str): Executable graph instance name, not its reusable definition.
        graphUid (str): Kubernetes UID fencing graph deletion and recreation.
        node (str): Vertex name inside the selected graph instance.
        kind (Literal['Graph', 'PolyGraph', 'ReplicaGroup']): Target graph kind.
    """

    requestId: str
    graph: str
    graphUid: str
    node: str
    kind: Literal["Graph", "PolyGraph", "ReplicaGroup"] = "Graph"

    def __attrs_post_init__(self) -> None:
        """
        Require portable identities and a graph incarnation.

        Returns:
            None: Invalid identities raise before API access.
        """
        for value in (self.requestId, self.graph, self.node):
            identity(value)
        if not self.graphUid or len(self.graphUid) > 128 or self.kind not in {"Graph", "PolyGraph", "ReplicaGroup"}:
            raise ValueError("activation requires a valid graph kind and UID")


@frozen
class ConnectionRequest:
    """
    Request an immutable, expiring connection within a persisted graph boundary.

    Attributes:
        requestId (str): Client idempotency key retained with the receipt.
        namespace (str): Namespace of the target graph and receipt.
        kind (Literal['Graph', 'PolyGraph', 'ReplicaGroup']): Target boundary kind.
        graph (str): Persisted instance name, not a reusable template name.
        graphUid (str): Expected graph incarnation.
        source (str): Sending logical node or replica ordinal.
        target (str): Receiving logical node or replica ordinal.
        ttlSeconds (int): Lifetime measured from Kubernetes receipt creation.
        ports (tuple[NetworkPort, ...]): Destination grants, empty for topology only.
        bidirectional (bool): Whether to add the reverse connection with the same ports.
        peers (dict[str, ServiceEndpoint]): Exact service participants for an atlas-coordinated connection.
    """

    requestId: str = field(metadata={"schema": {"minLength": 1, "maxLength": 128, "pattern": "^[A-Za-z0-9_.:-]+$"}})
    namespace: str = field(metadata={"schema": {"minLength": 1, "maxLength": 63, "pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"}})
    kind: Literal["Graph", "PolyGraph", "ReplicaGroup"]
    graph: str = field(metadata={"schema": {"minLength": 1, "maxLength": 253, "pattern": "^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"}})
    graphUid: str = field(metadata={"schema": {"minLength": 1, "maxLength": 128}})
    source: str = field(metadata={"schema": {"minLength": 1, "maxLength": 63, "pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"}})
    target: str = field(metadata={"schema": {"minLength": 1, "maxLength": 63, "pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"}})
    ttlSeconds: int = field(metadata={"schema": {"minimum": 1, "maximum": MAX_TTL}})
    ports: tuple[NetworkPort, ...] = field(default=(), metadata={"schema": {"maxItems": 32}})
    bidirectional: bool = False
    peers: dict[str, ServiceEndpoint] = field(factory=dict)

    def __attrs_post_init__(self) -> None:
        """
        Reject identities, durations and endpoint pairs that cannot be admitted.

        Returns:
            None: Invalid requests raise before persistence.
        """
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.requestId):
            raise ValueError("requestId must contain 1 through 128 letters, digits, dots, underscores, colons or hyphens")
        for name in (self.namespace, self.source, self.target):
            if len(name) > 63 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", name):
                raise ValueError("namespace and node names must be DNS labels")
        if len(self.graph) > 253 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", self.graph):
            raise ValueError("graph must be a Kubernetes resource name")
        if self.kind not in {"Graph", "PolyGraph", "ReplicaGroup"} or not 1 <= len(self.graphUid) <= 128:
            raise ValueError("a supported graph kind and graphUid are required")
        if type(self.ttlSeconds) is not int or not 1 <= self.ttlSeconds <= MAX_TTL:
            raise ValueError("ttlSeconds must be an integer from 1 through 86400")
        if self.source == self.target or len(self.ports) > 32 or type(self.bidirectional) is not bool:
            raise ValueError("connections require distinct endpoints, at most 32 ports and a boolean bidirectional flag")
        if self.peers and set(self.peers) != {"source", "target"}:
            raise ValueError("atlas connections require exactly source and target peers")


@frozen
class ServiceConnectionRequest:
    """
    Negotiate an edge between discovered services through their common graph boundary.

    Attributes:
        requestId (str): Stable idempotency key for this proposal.
        source (ServiceEndpoint): Sending service's exact graph and node.
        target (ServiceEndpoint): Receiving service's exact graph and node.
        ttlSeconds (int): Lifetime including time spent awaiting consent.
        ports (tuple[NetworkPort, ...]): Explicit destination TCP ports for remote transport.
        bidirectional (bool): Also negotiate the reverse direction.
    """

    requestId: str
    source: ServiceEndpoint
    target: ServiceEndpoint
    ttlSeconds: int
    ports: tuple[NetworkPort, ...] = ()
    bidirectional: bool = False

    def __attrs_post_init__(self) -> None:
        """
        Reject malformed or unbounded negotiation before contacting cluster APIs.

        Returns:
            None: Invalid service requests raise ValueError.
        """
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.requestId):
            raise ValueError("invalid requestId")
        if type(self.ttlSeconds) is not int or not 1 <= self.ttlSeconds <= MAX_TTL:
            raise ValueError("ttlSeconds must be an integer from 1 through 86400")
        if self.source == self.target or len(self.ports) > 32 or type(self.bidirectional) is not bool:
            raise ValueError("service connections require distinct peers, at most 32 ports and boolean bidirectional")
        if self.source.cluster != self.target.cluster and (not self.ports or any(port.protocol != "TCP" for port in self.ports)):
            raise ValueError("cross-cluster service connections require explicit TCP ports")
