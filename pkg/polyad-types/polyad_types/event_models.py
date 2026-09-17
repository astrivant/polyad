"""
Define the syntax trees of public event payloads and their transport envelopes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from attrs import field, frozen

from polyad_types.discovery import ServiceEndpoint
from polyad_types.network import NetworkPort

if TYPE_CHECKING:
    from typing import TypeAlias


@frozen(kw_only=True)
class EventIdentity:
    """
    Fence a public resource identity to its namespace, incarnation and optional cluster.

    Attributes:
        kind (str): Kubernetes resource kind.
        namespace (str): Resource namespace.
        name (str): Resource name.
        uid (str): Exact resource incarnation.
        cluster (str | None): Registered cluster; omitted for local observations.
    """

    kind: str
    namespace: str
    name: str
    uid: str
    cluster: str | None = None


@frozen(kw_only=True)
class ObservationIdentity(EventIdentity):
    """
    Record the Kubernetes revision associated with an observation.

    Attributes:
        apiVersion (str): Kubernetes API group/version.
        resourceVersion (str): Opaque Kubernetes version; not a topology revision.
        generation (int): Desired-state generation.
    """

    apiVersion: str
    resourceVersion: str
    generation: int = field(metadata={"schema": {"minimum": 0}})


@frozen(kw_only=True)
class EventOwner:
    """
    Describe the public subset of an observation's owner references.

    Attributes:
        kind (str): Owner kind.
        name (str): Owner name.
        uid (str): Exact owner incarnation.
    """

    kind: str
    name: str
    uid: str


@frozen(kw_only=True)
class EventStatus:
    """
    Expose lifecycle flags and extensible policy observations without workload specifications.

    Attributes:
        phase (str | None): Resource-specific lifecycle phase.
        ready (bool | None): Observed readiness.
        completed (bool | None): Observed completion.
        failed (bool | None): Observed failure.
        observedGeneration (int | None): Desired generation represented by this status.
        activations (dict[str, Any] | None): Resource-specific activation status JSON.
        throughput (dict[str, Any] | None): Soul searching observations, including traffic and Cheeger targets.
    """

    phase: str | None = None
    ready: bool | None = None
    completed: bool | None = None
    failed: bool | None = None
    observedGeneration: int | None = None
    activations: dict[str, Any] | None = None
    throughput: dict[str, Any] | None = None


@frozen(kw_only=True)
class GraphObservation(ObservationIdentity):
    """
    Describe a lifecycle observation carried by a graph event.

    Attributes:
        type (Literal['observation', 'deleting']): Ordinary observation or deletion in progress.
        owners (tuple[EventOwner, ...]): Public owner references.
        ancestry (tuple[EventIdentity, ...]): Verified graph ancestors used for visibility.
        audit (dict[str, str]): Public composition, request and node labels.
        status (EventStatus): Selected lifecycle and policy observations.
        resources (dict[str, Any]): Extensible resource-count metrics JSON.
    """

    type: Literal["observation", "deleting"]
    owners: tuple[EventOwner, ...]
    ancestry: tuple[EventIdentity, ...]
    audit: dict[str, str]
    status: EventStatus
    resources: dict[str, Any]


@frozen(kw_only=True)
class TopologyObservation(ObservationIdentity):
    """
    Notify subscribers to refresh a graph's neighbor snapshot.

    Attributes:
        type (Literal['topology']): Topology payload discriminator.
        ancestry (tuple[EventIdentity, ...]): Verified graph ancestors.
        revision (str): Structural revision, independent of resourceVersion.
        snapshot (str): Relative topology API path, including a cluster selector when needed.
        valid (bool): Whether the current topology can be represented.
        nodeCount (int): Number of logical nodes in the snapshot.
        connectionCount (int): Number of declared connections in the snapshot.
    """

    type: Literal["topology"]
    ancestry: tuple[EventIdentity, ...]
    revision: str
    snapshot: str
    valid: bool
    nodeCount: int = field(metadata={"schema": {"minimum": 0}})
    connectionCount: int = field(metadata={"schema": {"minimum": 0}})


@frozen(kw_only=True)
class ConnectionTarget:
    """
    Describe the exact boundary and logical endpoints of a temporary connection.

    Attributes:
        kind (Literal['Graph', 'PolyGraph', 'ReplicaGroup']): Boundary kind.
        graph (str): Boundary name.
        graphUid (str): Boundary incarnation.
        source (str): Source node.
        target (str): Target node.
        ports (tuple[NetworkPort, ...]): Granted destination ports and protocols.
        bidirectional (bool): Whether the proposal also grants reverse traffic.
    """

    kind: Literal["Graph", "PolyGraph", "ReplicaGroup"]
    graph: str
    graphUid: str
    source: str
    target: str
    ports: tuple[NetworkPort, ...]
    bidirectional: bool


@frozen(kw_only=True)
class ConnectionReceipt:
    """
    Carry the public consent receipt used by application connection hooks.

    Attributes:
        requestId (str): Stable proposal idempotency key.
        name (str): Receipt resource name.
        namespace (str): Receipt namespace.
        uid (str): Receipt incarnation required by approval requests.
        expiresAt (str): Immutable ISO-8601 deadline.
        target (ConnectionTarget): Exact requested edge.
        status (dict[str, Any]): Resource-specific admission and negotiation status JSON.
        consent (dict[str, Literal['Approve', 'Reject']]): Verified endpoint decisions.
        peers (dict[str, ServiceEndpoint]): Cluster-qualified source and target for atlas proposals.
        revokeRequested (bool): Whether revocation has been requested.
    """

    requestId: str
    name: str
    namespace: str
    uid: str
    expiresAt: str
    target: ConnectionTarget
    status: dict[str, Any]
    consent: dict[str, Literal["Approve", "Reject"]]
    peers: dict[str, ServiceEndpoint]
    revokeRequested: bool


@frozen(kw_only=True)
class ConnectionObservation:
    """
    Deliver local receipts or atlas participant projections through an authorized graph tree.

    Attributes:
        type (Literal['connection']): Consent event payload discriminator.
        graph (EventIdentity): Graph in whose stream this participant is visible.
        connection (ConnectionReceipt): Public receipt without credentials or workload templates.
        ancestry (tuple[EventIdentity, ...]): Verified ancestors of the visible graph.
        participant (Literal['source', 'target'] | None): Atlas projection side; omitted for local receipts.
        apiVersion (str | None): API version of a locally observed receipt.
        kind (Literal['TemporaryConnection'] | None): Kind of a locally observed receipt.
        namespace (str | None): Local receipt namespace.
        name (str | None): Local receipt name.
        uid (str | None): Local receipt UID.
        resourceVersion (str | None): Local receipt resource version.
        generation (int | None): Local receipt generation.
        cluster (str | None): Registered cluster of the local receipt.
        owners (tuple[EventOwner, ...] | None): Local receipt owners.
        audit (dict[str, str] | None): Public local receipt labels.
        status (EventStatus | None): Selected local lifecycle observations.
        resources (dict[str, Any] | None): Local resource-count observations.
    """

    type: Literal["connection"]
    graph: EventIdentity
    connection: ConnectionReceipt
    ancestry: tuple[EventIdentity, ...]
    participant: Literal["source", "target"] | None = None
    apiVersion: str | None = None
    kind: Literal["TemporaryConnection"] | None = None
    namespace: str | None = None
    name: str | None = None
    uid: str | None = None
    resourceVersion: str | None = None
    generation: int | None = None
    cluster: str | None = None
    owners: tuple[EventOwner, ...] | None = None
    audit: dict[str, str] | None = None
    status: EventStatus | None = None
    resources: dict[str, Any] | None = None


@frozen(kw_only=True)
class StreamControl:
    """
    Explain why the application must refresh or reconnect without acknowledging another event.

    Attributes:
        reason (str): Human-readable recovery instruction.
    """

    reason: str


@frozen(kw_only=True)
class Heartbeat:
    """
    Represent an empty WebSocket heartbeat payload with no application observation.
    """


@frozen(kw_only=True)
class GraphEvent:
    """
    Wrap a lifecycle observation in the transport-neutral event AST.

    Attributes:
        id (str): Replay cursor to commit after handling.
        data (GraphObservation): Lifecycle payload.
        event (Literal['graph']): Transport discriminator.
    """

    id: str
    data: GraphObservation
    event: Literal["graph"] = "graph"


@frozen(kw_only=True)
class TopologyEvent:
    """
    Wrap a structural change in the transport-neutral event AST.

    Attributes:
        id (str): Replay cursor to commit after handling.
        data (TopologyObservation): Structural change payload.
        event (Literal['topology']): Transport discriminator.
    """

    id: str
    data: TopologyObservation
    event: Literal["topology"] = "topology"


@frozen(kw_only=True)
class ConnectionEvent:
    """
    Wrap a consent notification in the transport-neutral event AST.

    Attributes:
        id (str): Replay cursor to commit after handling.
        data (ConnectionObservation): Local or atlas consent payload.
        event (Literal['connection']): Transport discriminator.
    """

    id: str
    data: ConnectionObservation
    event: Literal["connection"] = "connection"


@frozen(kw_only=True)
class ControlEvent:
    """
    End consumption without advancing the application checkpoint.

    Attributes:
        event (Literal['reset', 'unavailable']): Refresh-snapshot or reconnect instruction.
        data (StreamControl): Recovery reason.
        id (Literal['']): Controls never acknowledge a stream position.
    """

    event: Literal["reset", "unavailable"]
    data: StreamControl
    id: Literal[""] = ""


@frozen(kw_only=True)
class HeartbeatEvent:
    """
    Keep a WebSocket subscription alive without advancing its checkpoint.

    Attributes:
        data (Heartbeat): Empty heartbeat object.
        event (Literal['heartbeat']): Heartbeat discriminator.
        id (Literal['']): Heartbeats never acknowledge a stream position.
    """

    data: Heartbeat = field(factory=Heartbeat)
    event: Literal["heartbeat"] = "heartbeat"
    id: Literal[""] = ""


@frozen(kw_only=True)
class Copulse:
    """
    Request transport rediscovery while preserving the last application checkpoint.

    Attributes:
        reason (str): Membership change, administrative roll, stream age or replica drain.
        revision (str): Observed operator membership revision; never a graph revision.
        retryAfterSeconds (float): Bounded delay before rediscovery, spreading reconnect load.
    """

    reason: str
    revision: str
    retryAfterSeconds: float = field(metadata={"schema": {"minimum": 0, "maximum": 30}})


@frozen(kw_only=True)
class CopulseEvent:
    """
    Carry an operator connection roll without publishing reserved graph observations.

    Attributes:
        data (Copulse): Reconnection instruction without destinations or credentials.
        event (Literal['copulse']): Transport control discriminator.
        id (Literal['']): Reconnection never acknowledges an application event.
    """

    data: Copulse
    event: Literal["copulse"] = "copulse"
    id: Literal[""] = ""


EventAST: TypeAlias = GraphEvent | TopologyEvent | ConnectionEvent | ControlEvent | HeartbeatEvent | CopulseEvent
EVENT_MODELS: dict[str, type] = {
    "graph": GraphEvent,
    "topology": TopologyEvent,
    "connection": ConnectionEvent,
    "reset": ControlEvent,
    "unavailable": ControlEvent,
    "heartbeat": HeartbeatEvent,
    "copulse": CopulseEvent,
}
