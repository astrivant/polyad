"""
Describe bounded replication of reusable workload and graph definitions.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from attrs import field, frozen

from polyad.graph.network import NetworkPort

if TYPE_CHECKING:
    from collections.abc import Iterable


@frozen
class ReplicaConnection:
    """
    Connect two stable ordinals when both are present in a custom replica topology.

    Attributes:
        source (str): Sending ordinal, such as replica-0.
        target (str): Receiving ordinal, such as replica-1.
        ports (tuple[NetworkPort, ...]): Optional destination transport grants.
    """

    source: str = field(metadata={"schema": {"pattern": "^replica-(0|[1-9][0-9]{0,2})$", "maxLength": 11}})
    target: str = field(metadata={"schema": {"pattern": "^replica-(0|[1-9][0-9]{0,2})$", "maxLength": 11}})
    ports: tuple[NetworkPort, ...] = ()

    def __attrs_post_init__(self) -> None:
        """
        Reject invalid ordinal names and self connections.

        Returns:
            None: Invalid endpoints raise before projection.
        """
        if any(not re.fullmatch(r"replica-(0|[1-9][0-9]{0,2})", name) for name in (self.source, self.target)):
            raise ValueError("custom connections require canonical replica-N endpoints")
        if self.source == self.target:
            raise ValueError("replica connections cannot connect an ordinal to itself")


@frozen
class ReplicaConnectivity:
    """
    Select data-flow edges between copies without introducing admission dependencies.

    Attributes:
        mode (Literal['Independent', 'Chain', 'Ring', 'Star', 'FullMesh', 'Custom']): Connection pattern over active ordinals.
        bidirectional (bool): Add a reverse connection with the same ports for each edge.
        ports (tuple[NetworkPort, ...]): Destination grants for built-in patterns; Custom uses each edge's ports.
        edges (tuple[ReplicaConnection, ...]): Custom edges, active only when both endpoints exist.
    """

    mode: Literal["Independent", "Chain", "Ring", "Star", "FullMesh", "Custom"] = field(
        default="Independent", metadata={"schema": {"default": "Independent"}}
    )
    bidirectional: bool = field(default=False, metadata={"schema": {"default": False}})
    ports: tuple[NetworkPort, ...] = ()
    edges: tuple[ReplicaConnection, ...] = field(default=(), metadata={"schema": {"maxItems": 4096}})

    def __attrs_post_init__(self) -> None:
        """
        Reject ambiguous or ignored connection options.

        Returns:
            None: Invalid configurations raise before projection.
        """
        if self.mode not in {"Independent", "Chain", "Ring", "Star", "FullMesh", "Custom"}:
            raise ValueError("unknown replica connectivity mode")
        if self.mode != "Custom" and self.edges:
            raise ValueError("connectivity.edges requires Custom mode")
        if self.mode == "Custom" and self.ports:
            raise ValueError("Custom mode uses ports on individual edges")
        if self.mode == "Independent" and (self.ports or self.bidirectional):
            raise ValueError("Independent mode does not accept ports or bidirectional connections")
        if len(self.edges) > 4096 or len({(edge.source, edge.target) for edge in self.edges}) != len(self.edges):
            raise ValueError("Custom mode accepts at most 4096 edges with unique source/target pairs")

    def connections(self, names: tuple[str, ...]) -> list[dict[str, Any]]:
        """
        Generate deterministic connections for the ordered set of projected copies.

        Args:
            names (tuple[str, ...]): Active ordinal names in numeric order.

        Returns:
            list[dict[str, Any]]: Data-flow edges, including explicit transport grants.
        """
        from polyad.graph.topology import converter

        pairs: list[tuple[str, str]] = []
        if self.mode in {"Chain", "Ring"}:
            pairs = list(zip(names[:-1], names[1:], strict=True))
            if self.mode == "Ring" and len(names) > 1:
                pairs.append((names[-1], names[0]))
        elif self.mode == "Star" and names:
            pairs = [(names[0], name) for name in names[1:]]
        elif self.mode == "FullMesh":
            pairs = [(source, target) for source in names for target in names if source != target]
        edges = (
            [edge for edge in self.edges if edge.source in names and edge.target in names]
            if self.mode == "Custom"
            else [ReplicaConnection(source, target, self.ports) for source, target in pairs]
        )
        if self.bidirectional:
            edges += [ReplicaConnection(edge.target, edge.source, edge.ports) for edge in edges]
        # Reverse edges with different ports retain both grants; identical edges
        # (including already symmetric rings and meshes) need only one declaration.
        return [converter.unstructure(edge) for edge in dict.fromkeys(edges)]


@frozen
class ReplicaTemplate:
    """
    Select the reusable definition instantiated at each stable replica ordinal.

    Attributes:
        kind (Literal['Workload', 'Daemon', 'Ephemeral', 'Resource', 'Graph', 'PolyGraph', 'ReplicaGroup']):
            Executable definition or resource abstraction.
        ref (str): Namespaced definition name.
    """

    kind: Literal["Workload", "Daemon", "Ephemeral", "Resource", "Graph", "PolyGraph", "ReplicaGroup"]
    ref: str = field(metadata={"schema": {"minLength": 1, "maxLength": 63, "pattern": "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"}})


@frozen
class ReplicaSource:
    """
    Bind a generated instance to its reusable group's scaling intent.

    Attributes:
        name (str): Reusable ReplicaGroup name.
        uid (str): Incarnation fence for the reusable group.
    """

    name: str = field(metadata={"schema": {"minLength": 1, "maxLength": 63}})
    uid: str = field(metadata={"schema": {"minLength": 1, "maxLength": 128}})


@frozen
class Replication:
    """
    Bound copies and their connections while preserving stable ordinals and graph admission.

    Attributes:
        template (ReplicaTemplate): Definition replicated by this group.
        replicas (int): Requested number of copies, exposed through the scale subresource.
        minReplicas (int): Inclusive lower bound, including zero.
        maxReplicas (int): Inclusive upper bound, at most 256 copies per group.
        templateOnly (bool): Reusable group controlling all inheriting instances.
        replicaSource (ReplicaSource | None): Compiler-provided reference to shared replica intent.
        inheritReplicas (bool): Whether a generated instance follows its reusable group.
        suspend (bool): Drain execution until resumed.
        placement (dict[str, Any] | None): Placement inherited by all copies.
        rules (tuple[str, ...]): Structural restrictions for every replicated graph.
        network (dict[str, Any] | None): Traffic restrictions across this group.
        capacity (dict[str, Any] | None): Advance capacity policy for contained work.
        shutdownPolicy (str | None): Graceful termination policy.
        activation (dict[str, Any] | None): Optional pulse policy when referenced by another graph.
        connectivity (ReplicaConnectivity): Data-flow pattern between the copies in this boundary.
    """

    template: ReplicaTemplate
    replicas: int = field(default=1, metadata={"schema": {"minimum": 0, "maximum": 256}})
    minReplicas: int = field(default=0, metadata={"schema": {"minimum": 0, "maximum": 256}})
    maxReplicas: int = field(default=32, metadata={"schema": {"minimum": 1, "maximum": 256}})
    templateOnly: bool = False
    replicaSource: ReplicaSource | None = None
    inheritReplicas: bool = True
    suspend: bool = False
    placement: dict[str, Any] | None = None
    rules: tuple[str, ...] = ()
    network: dict[str, Any] | None = None
    capacity: dict[str, Any] | None = None
    shutdownPolicy: str | None = None
    activation: dict[str, Any] | None = None
    connectivity: ReplicaConnectivity = field(factory=ReplicaConnectivity, kw_only=True)

    def __attrs_post_init__(self) -> None:
        """
        Reject unbounded or contradictory replica counts.

        Returns:
            None: Invalid counts raise before admission.
        """
        if any(type(value) is not int for value in (self.replicas, self.minReplicas, self.maxReplicas)):
            raise ValueError("replica bounds must be integers")
        if not 0 <= self.minReplicas <= self.replicas <= self.maxReplicas <= 256 or self.maxReplicas < 1:
            raise ValueError("replicas must lie within minReplicas and maxReplicas, capped at 256")
        if any(int(name[8:]) >= self.maxReplicas for edge in self.connectivity.edges for name in (edge.source, edge.target)):
            raise ValueError("custom connection ordinals must be below maxReplicas")


def replica_topology(spec: dict[str, Any], *, retained: Iterable[str] = ()) -> dict[str, Any]:
    """
    Project replication into ordinary graph vertices for scheduling and mathematical rules.

    Args:
        spec (dict[str, Any]): ReplicaGroup specification, including its effective count.
        retained (Iterable[str]): Live retiring ordinals retained when checking a sibling's constraints.

    Returns:
        dict[str, Any]: Persistent scheduling topology with one vertex per stable ordinal.
    """
    from polyad.graph.topology import converter

    policy = converter.structure(spec, Replication)
    names = tuple(sorted({*(f"replica-{index}" for index in range(policy.replicas)), *retained}, key=lambda name: int(name[8:])))
    result = {
        key: spec[key]
        for key in ("suspend", "placement", "rules", "network", "capacity", "shutdownPolicy", "templateOnly")
        if key in spec and spec[key] is not None
    }
    return {
        **result,
        "mode": "persistent",
        "slots": max(1, policy.maxReplicas),
        "nodes": [{"name": name, "kind": policy.template.kind, "ref": policy.template.ref} for name in names],
        "connections": policy.connectivity.connections(names),
    }
