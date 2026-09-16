"""
Describe admission separately from persistent data-flow connections.
"""

from __future__ import annotations

import math
import re
from typing import Generic, Literal

from attrs import field, frozen
from cattrs.errors import CattrsError
from typing_extensions import TypeVar

from polyad_types.activation import ActivationPolicy
from polyad_types.capacity import CapacityPlan
from polyad_types.codec import converter
from polyad_types.network import NetworkAccess, NetworkPort
from polyad_types.rules import Cheeger


@frozen
class Dependency:
    """
    Require an observed lifecycle condition before admitting a node.

    Attributes:
        node (str): Name of the node within its graph boundary.
        condition (Literal['started', 'ready', 'completed']): Observed lifecycle predicate required for admission.
    """

    node: str
    condition: Literal["started", "ready", "completed"] = "completed"


@frozen
class Node:
    """
    Reference a reusable Kubernetes execution definition within a boundary.

    Attributes:
        name (str): Resource name within its namespace.
        kind (Literal['Workload', 'Daemon', 'Resource', 'Graph', 'PolyGraph', 'ReplicaGroup']):
            Kubernetes resource kind.
        ref (str): Name of the reusable execution definition.
        requires (tuple[Dependency, ...]): Admission dependencies; all must be satisfied.
        gate (str | None): Optional name of a Boolean admission rule.
        slots (int): Resource slots reserved within the scheduling boundary.
        id (str | None): Optional composition node identifier retained for audit traces.
    """

    name: str
    kind: Literal["Workload", "Daemon", "Resource", "Graph", "PolyGraph", "ReplicaGroup"]
    ref: str
    requires: tuple[Dependency, ...] = ()
    gate: str | None = None
    slots: int = 1
    id: str | None = field(default=None, kw_only=True)


@frozen
class Connection:
    """
    Declare a data-flow edge, which may cycle without blocking admission.

    Attributes:
        source (str): Node emitting data on this connection.
        target (str): Node receiving data on this connection.
        ports (tuple[NetworkPort, ...]): Optional transport grants when graph networking is enabled.
    """

    source: str
    target: str
    ports: tuple[NetworkPort, ...] = ()


@frozen
class ThroughputTier:
    """
    Associate measured application demand with an independently calibrated expansion target.

    Attributes:
        offeredPerSecond (float): Inclusive demand threshold in the application's declared unit.
        cheeger (Cheeger): Target range on the connections relation; never overrides GraphRules.
    """

    offeredPerSecond: float = field(metadata={"schema": {"minimum": 0}})
    cheeger: Cheeger

    def __attrs_post_init__(self) -> None:
        """
        Require a finite demand threshold and at least one target bound.

        Returns:
            None: No return value.
        """
        if isinstance(self.offeredPerSecond, bool) or not math.isfinite(self.offeredPerSecond) or self.offeredPerSecond < 0:
            raise ValueError("throughput demand thresholds must be finite and nonnegative")
        if self.cheeger.minimum is None and self.cheeger.maximum is None:
            raise ValueError("throughput tiers require a Cheeger target")


@frozen
class ThroughputLayout:
    """
    Approve one complete set of boundary-local data-flow connections.

    Attributes:
        name (str): Stable administrator-selected layout name.
        connections (tuple[Connection, ...]): Complete replacement connections; nodes and admission stay unchanged.
    """

    name: str = field(metadata={"schema": {"minLength": 1, "maxLength": 63}})
    connections: tuple[Connection, ...] = field(metadata={"schema": {"maxItems": 380}})


@frozen
class ThroughputPolicy:
    """
    Observe or adapt topology using empirical throughput targets within hard structural rules.

    Attributes:
        unit (str): Application work unit shared by offered and completed rates, such as records.
        tiers (tuple[ThroughputTier, ...]): Strictly increasing demand thresholds; highest matching tier wins.
        layouts (tuple[ThroughputLayout, ...]): Approved alternatives, tried in declaration order.
        mode (Literal['Observe', 'Adapt']): Observe reports recommendations; Adapt may replace connections.
        sampleMaxAgeSeconds (int): Maximum age and gap between usable samples.
        sustainedSeconds (int): Continuous shortfall duration required before selecting a layout.
        minSamples (int): Distinct reports required during the sustained shortfall.
        shortfallRatio (float): Completed/offered ratio below which demanded throughput is unmet.
        cooldownSeconds (int): Minimum time between successful topology changes.
        maxChangesPerHour (int): Maximum successful changes in a rolling hour.
    """

    unit: str = field(metadata={"schema": {"minLength": 1, "maxLength": 64}})
    tiers: tuple[ThroughputTier, ...] = field(metadata={"schema": {"minItems": 1, "maxItems": 16}})
    layouts: tuple[ThroughputLayout, ...] = field(default=(), metadata={"schema": {"maxItems": 8}})
    mode: Literal["Observe", "Adapt"] = "Observe"
    sampleMaxAgeSeconds: int = field(default=60, metadata={"schema": {"minimum": 1, "maximum": 3600}})
    sustainedSeconds: int = field(default=60, metadata={"schema": {"minimum": 1, "maximum": 86400}})
    minSamples: int = field(default=3, metadata={"schema": {"minimum": 2, "maximum": 1000}})
    shortfallRatio: float = field(default=0.9, metadata={"schema": {"minimum": 0, "exclusiveMinimum": True, "maximum": 1}})
    cooldownSeconds: int = field(default=300, metadata={"schema": {"minimum": 1, "maximum": 86400}})
    maxChangesPerHour: int = field(default=2, metadata={"schema": {"minimum": 1, "maximum": 60}})

    def __attrs_post_init__(self) -> None:
        """
        Bound the feedback controller and reject ambiguous demand tiers.

        Returns:
            None: No return value.
        """
        if self.mode not in {"Observe", "Adapt"} or not self.unit.strip() or len(self.unit) > 64:
            raise ValueError("throughput requires a unit and Observe or Adapt mode")
        thresholds = [tier.offeredPerSecond for tier in self.tiers]
        if not 1 <= len(thresholds) <= 16 or thresholds != sorted(set(thresholds)):
            raise ValueError("throughput tiers require 1–16 strictly increasing demand thresholds")
        names = [layout.name for layout in self.layouts]
        if len(names) > 8 or len(set(names)) != len(names) or any(not name or len(name) > 63 for name in names):
            raise ValueError("throughput layouts require unique names and at most eight alternatives")
        if any(len(layout.connections) > 380 for layout in self.layouts):
            raise ValueError("throughput layouts support at most 380 connections")
        if self.mode == "Adapt" and not self.layouts:
            raise ValueError("Adapt requires administrator-approved layouts")
        for value, minimum, maximum in (
            (self.sampleMaxAgeSeconds, 1, 3600),
            (self.sustainedSeconds, 1, 86400),
            (self.minSamples, 2, 1000),
            (self.cooldownSeconds, 1, 86400),
            (self.maxChangesPerHour, 1, 60),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("throughput timing and change budgets must be bounded positive integers")
        if isinstance(self.shortfallRatio, bool) or not 0 < self.shortfallRatio <= 1:
            raise ValueError("shortfallRatio must be greater than zero and at most one")


@frozen
class Placement:
    """
    Select a labeled resource slice and tolerate its taints for an entire graph.

    Attributes:
        nodeSelector (dict[str, str]): Required Kubernetes node labels.
        tolerations (tuple[dict[str, object], ...]): Taints that descendant pods may tolerate.
        nodeAffinity (dict[str, object]): Required and preferred Kubernetes node affinity rules.
        enforce (bool): Whether descendants must retain this placement rather than override it.
    """

    nodeSelector: dict[str, str] = field(factory=dict)
    tolerations: tuple[dict[str, object], ...] = ()
    nodeAffinity: dict[str, object] = field(factory=dict)
    enforce: bool = True

    def __attrs_post_init__(self) -> None:
        """
        Require at least one placement constraint or toleration.

        Returns:
            None: No return value.
        """
        if not (self.nodeSelector or self.nodeAffinity or self.tolerations):
            raise ValueError("placement needs a selector, affinity or toleration")


@frozen
class Topology:
    """
    Define a finite or persistent graph with an acyclic admission relation.

    Attributes:
        nodes (tuple[Node, ...]): Execution nodes owned by this graph boundary.
        connections (tuple[Connection, ...]): Data-flow edges, including permitted cycles.
        mode (Literal['finite', 'persistent']): Whether the graph has finite completion or persistent operation.
        slots (int): Resource slots reserved within the scheduling boundary.
        shutdownPolicy (str | None): Optional name of the graph termination policy.
        suspend (bool): Whether execution is drained and admission paused.
        templateOnly (bool): Whether this definition is instantiated only by a parent graph.
        placement (Placement | None): Scheduling constraints inherited by descendant execution.
        rules (tuple[str, ...]): Additional structural rules inherited by nested boundaries.
        capacity (CapacityPlan | None): Optional advance capacity policy inherited by nested graph instances.
        network (NetworkAccess | None): Optional traffic restrictions inherited by descendant workloads.
        activation (ActivationPolicy | None): Optional pulse policy when this graph is referenced as a downstream node.
        throughput (ThroughputPolicy | None): Optional application feedback with separate Cheeger targets and approved layouts.
    """

    nodes: tuple[Node, ...]
    connections: tuple[Connection, ...] = ()
    mode: Literal["finite", "persistent"] = "finite"
    slots: int = 64
    shutdownPolicy: str | None = None
    suspend: bool = False
    templateOnly: bool = False
    placement: Placement | None = field(default=None, kw_only=True)
    rules: tuple[str, ...] = field(default=(), kw_only=True)
    capacity: CapacityPlan | None = field(default=None, kw_only=True)
    network: NetworkAccess | None = field(default=None, kw_only=True)
    activation: ActivationPolicy | None = field(default=None, kw_only=True)
    throughput: ThroughputPolicy | None = field(default=None, kw_only=True)

    def __attrs_post_init__(self) -> None:
        """
        Reject deadlocked admission and impossible completion dependencies.

        Returns:
            None: No return value.
        """
        names = {node.name for node in self.nodes}
        if self.throughput is not None:
            if len(names) > 20:
                raise ValueError("throughput Cheeger feedback supports at most 20 vertices per boundary")
            for layout in self.throughput.layouts:
                if any(edge.source not in names or edge.target not in names for edge in layout.connections):
                    raise ValueError("throughput layout connection endpoint is absent")
        if len(names) != len(self.nodes) or self.slots < 1:
            raise ValueError("nodes must be unique and capacity positive")
        if self.network:
            for rule in (*self.network.ingress, *self.network.egress):
                if rule.node is not None and rule.node not in names:
                    raise ValueError("network rule refers to an absent local node")
                if rule.peer.node and not rule.peer.graph and rule.peer.node not in names:
                    raise ValueError("network peer refers to an absent local node")
        by_name = {node.name: node for node in self.nodes}
        for node in self.nodes:
            if node.slots < 1 or node.slots > self.slots:
                raise ValueError("node reservation must fit graph capacity")
            for edge in node.requires:
                if edge.node not in names:
                    raise ValueError(f"unknown dependency: {edge.node}")
                if edge.condition == "completed" and by_name[edge.node].kind in {"Daemon", "Resource"}:
                    raise ValueError("daemons do not complete, and resources expose readiness; depend on ready or started")
        if self.mode == "finite" and any(node.kind == "Daemon" for node in self.nodes):
            raise ValueError("a graph containing daemons must be persistent")
        resolved: set[str] = set()
        while len(resolved) < len(names):
            ready = {n.name for n in self.nodes if all(e.node in resolved for e in n.requires)} - resolved
            if not ready:
                raise ValueError("admission cycle: use connections for cyclic data flow")
            resolved.update(ready)
        if any(edge.source not in names or edge.target not in names for edge in self.connections):
            raise ValueError("connection endpoint is absent")


def topology(spec: dict[str, object], kind: str = "Graph") -> Topology:
    """
    Decode a strict, serializable graph definition using cattrs.

    Args:
        spec (dict[str, object]): Desired resource configuration.
        kind (str): Graph kind; PolyGraph restricts nodes to graph boundaries.

    Returns:
        Topology: Validated graph topology.
    """
    if kind not in {"Graph", "PolyGraph", "ReplicaGroup"}:
        raise ValueError(f"unsupported graph kind: {kind}")
    try:
        if kind == "ReplicaGroup" and "template" in spec:
            from polyad_types.replication import replica_topology

            spec = replica_topology(spec)
        return converter.structure(spec, PolyGraph[GraphNode] if kind == "PolyGraph" else Topology)
    except CattrsError as error:
        raise ValueError(str(error)) from error


@frozen
class Daemon(Node):
    """
    Reference a persistent capability whose lifecycle is readiness and explicit shutdown.

    Attributes:
        kind (Literal['Daemon']): Kubernetes resource kind.
    """

    kind: Literal["Daemon"] = field(default="Daemon", init=False)


@frozen(kw_only=True)
class GraphNode(Node):
    """
    Reference a reusable graph boundary as a node in another graph.

    Attributes:
        kind (Literal['Graph', 'PolyGraph', 'ReplicaGroup']): Referenced boundary kind.
        cluster (str | None): Registered destination cluster; omitted keeps the child in this cluster.
    """

    kind: Literal["Graph", "PolyGraph", "ReplicaGroup"]
    cluster: str | None = field(
        default=None,
        metadata={"schema": {"maxLength": 63, "pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"}},
    )

    def __attrs_post_init__(self) -> None:
        """
        Restrict remote placement to managed Graph and PolyGraph boundaries.

        Returns:
            None: No return value.
        """
        if self.cluster is not None:
            if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", self.cluster):
                raise ValueError("cluster references must be DNS labels")
            if self.kind == "ReplicaGroup":
                raise ValueError("place a Graph or PolyGraph containing the ReplicaGroup in the remote cluster")


NodeT = TypeVar("NodeT", bound=GraphNode, default=GraphNode, covariant=True)


@frozen(kw_only=True)
class PolyGraph(Topology, Generic[NodeT]):
    """
    Compose local or remotely placed graph boundaries under one lifecycle.

    Attributes:
        nodes (tuple[NodeT, ...]): Nested graph references, preserving their concrete type for consumers.
    """

    nodes: tuple[NodeT, ...]

    def __attrs_post_init__(self) -> None:
        """
        Validate admission and restrict composition to graph boundary references.

        Returns:
            None: No return value.
        """
        Topology.__attrs_post_init__(self)
        if any(node.kind not in {"Graph", "PolyGraph", "ReplicaGroup"} for node in self.nodes):
            raise ValueError("PolyGraph nodes must reference graph boundaries")
