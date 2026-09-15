"""
Describe admission separately from persistent data-flow connections.
"""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from attrs import field, frozen
from cattrs import Converter
from cattrs.errors import CattrsError

from polyad.graph.network import NetworkAccess, NetworkPort


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
        kind (Literal['Workload', 'Daemon', 'Ephemeral', 'Resource', 'Graph', 'EphemeralGraph', 'Feedback', 'PolyGraph']):
            Kubernetes resource kind.
        ref (str): Name of the reusable execution definition.
        requires (tuple[Dependency, ...]): Admission dependencies; all must be satisfied.
        gate (str | None): Optional name of a Boolean admission rule.
        slots (int): Resource slots reserved within the scheduling boundary.
        id (str | None): Optional composition node identifier retained for audit traces.
    """

    name: str
    kind: Literal["Workload", "Daemon", "Ephemeral", "Resource", "Graph", "EphemeralGraph", "Feedback", "PolyGraph"]
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
        network (NetworkAccess | None): Optional traffic restrictions inherited by descendant workloads.
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
    network: NetworkAccess | None = field(default=None, kw_only=True)

    def __attrs_post_init__(self) -> None:
        """
        Reject deadlocked admission and impossible completion dependencies.

        Returns:
            None: No return value.
        """
        names = {node.name for node in self.nodes}
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


converter = Converter(forbid_extra_keys=True, detailed_validation=False)
converter.register_structure_hook_func(lambda kind: kind is object, lambda value, _: value)


def topology(spec: dict[str, object], kind: str = "Graph") -> Topology:
    """
    Decode a strict, serializable graph definition using cattrs.

    Args:
        spec (dict[str, object]): Desired resource configuration.
        kind (str): Graph kind; PolyGraph restricts nodes to graph boundaries.

    Returns:
        Topology: Validated graph topology.
    """
    try:
        return converter.structure(spec, PolyGraph[GraphNode] if kind == "PolyGraph" else Topology)
    except CattrsError as error:
        raise ValueError(str(error)) from error


@frozen
class Ephemeral(Node):
    """
    Reference restartable finite execution on interruptible Kubernetes capacity.

    Attributes:
        kind (Literal['Ephemeral']): Kubernetes resource kind.
    """

    kind: Literal["Ephemeral"] = field(default="Ephemeral", init=False)


@frozen(kw_only=True)
class EphemeralGraph(Topology):
    """
    Apply spot placement to descendant execution while keeping durable graph intent.

    Attributes:
        placement (Placement): Scheduling constraints inherited by descendant execution.
    """

    placement: Placement


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
        kind (Literal['Graph', 'EphemeralGraph', 'Feedback', 'PolyGraph']): Referenced boundary kind.
    """

    kind: Literal["Graph", "EphemeralGraph", "Feedback", "PolyGraph"]


NodeT = TypeVar("NodeT", bound=GraphNode, default=GraphNode, covariant=True)


@frozen(kw_only=True)
class PolyGraph(Topology, Generic[NodeT]):
    """
    Compose graph boundaries under one lifecycle and placement contract.

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
        if any(node.kind not in {"Graph", "EphemeralGraph", "Feedback", "PolyGraph"} for node in self.nodes):
            raise ValueError("PolyGraph nodes must reference graph boundaries")
