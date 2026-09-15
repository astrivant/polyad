"""Describe admission separately from persistent data-flow connections."""

from typing import Literal

from attrs import field, frozen
from cattrs import Converter


@frozen
class Dependency:
    """Require an observed lifecycle condition before admitting a node."""

    node: str
    condition: Literal["started", "ready", "completed"] = "completed"


@frozen
class Node:
    """Reference a reusable Kubernetes execution definition within a boundary."""

    name: str
    kind: Literal["Workload", "Daemon", "Ephemeral", "Resource", "Graph", "EphemeralGraph", "Feedback"]
    ref: str
    requires: tuple[Dependency, ...] = ()
    gate: str | None = None
    slots: int = 1


@frozen
class Connection:
    """Declare a data-flow edge, which may cycle without blocking admission."""

    source: str
    target: str


@frozen
class Placement:
    """Select a labeled resource slice and tolerate its taints for an entire graph."""

    nodeSelector: dict[str, str] = field(factory=dict)
    tolerations: tuple[dict[str, object], ...] = ()
    nodeAffinity: dict[str, object] = field(factory=dict)

    def __attrs_post_init__(self) -> None:
        """Require at least one placement constraint or toleration."""
        if not (self.nodeSelector or self.nodeAffinity or self.tolerations):
            raise ValueError("placement needs a selector, affinity or toleration")


@frozen
class Topology:
    """Define a finite or persistent graph with an acyclic admission relation."""

    nodes: tuple[Node, ...]
    connections: tuple[Connection, ...] = ()
    mode: Literal["finite", "persistent"] = "finite"
    slots: int = 64
    shutdownPolicy: str | None = None
    suspend: bool = False
    templateOnly: bool = False
    placement: Placement | None = field(default=None, kw_only=True)

    def __attrs_post_init__(self) -> None:
        """Reject deadlocked admission and impossible completion dependencies."""
        names = {node.name for node in self.nodes}
        if len(names) != len(self.nodes) or self.slots < 1:
            raise ValueError("nodes must be unique and capacity positive")
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


def topology(spec: dict[str, object]) -> Topology:
    """Decode a strict, serializable graph definition using cattrs."""
    return converter.structure(spec, Topology)


@frozen
class Ephemeral(Node):
    """Reference restartable finite execution on interruptible Kubernetes capacity."""

    kind: Literal["Ephemeral"] = field(default="Ephemeral", init=False)


@frozen(kw_only=True)
class EphemeralGraph(Topology):
    """Apply spot placement to descendant execution while keeping durable graph intent."""

    placement: Placement


@frozen
class Daemon(Node):
    """Reference a persistent capability whose lifecycle is readiness and explicit shutdown."""

    kind: Literal["Daemon"] = field(default="Daemon", init=False)
