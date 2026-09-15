"""
Describe graph traffic independently of workload kind and admission dependencies.
"""

from __future__ import annotations

import re
from typing import Literal

from attrs import field, frozen


@frozen
class NetworkPort:
    """
    Select a transport port on the destination workload.

    Attributes:
        port (int): Destination port from 1 through 65535.
        protocol (Literal['TCP', 'UDP', 'SCTP']): Transport protocol.
    """

    port: int = field(metadata={"schema": {"minimum": 1, "maximum": 65535, "description": "Destination transport port."}})
    protocol: Literal["TCP", "UDP", "SCTP"] = "TCP"

    def __attrs_post_init__(self) -> None:
        """
        Validate the network contract before compilation.

        Returns:
            None: No return value.
        """
        if isinstance(self.port, bool) or not 1 <= self.port <= 65535 or self.protocol not in {"TCP", "UDP", "SCTP"}:
            raise ValueError("network ports require a valid protocol and a port from 1 through 65535")


@frozen
class NetworkPeer:
    """
    Select peers in an explicit namespace, optionally by graph or pod labels.

    Attributes:
        namespace (str | None): Peer namespace; omitted means this graph's namespace.
        graph (str | None): Named graph instance; omitted with node selects a node in this boundary.
        kind (Literal['Graph', 'PolyGraph', 'EphemeralGraph', 'Feedback', 'ReplicaGroup']): Kind of the referenced graph instance.
        node (str | None): Node and its descendants inside the selected graph.
        podLabels (dict[str, str]): Additional exact pod-label matches, combined with graph selection.
    """

    namespace: str | None = None
    graph: str | None = None
    kind: Literal["Graph", "PolyGraph", "EphemeralGraph", "Feedback", "ReplicaGroup"] = "Graph"
    node: str | None = None
    podLabels: dict[str, str] = field(factory=dict)

    def __attrs_post_init__(self) -> None:
        """
        Validate the network contract before compilation.

        Returns:
            None: No return value.
        """
        for value in (self.namespace, self.graph, self.node):
            if value is not None and not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", value):
                raise ValueError("network namespace, graph and node references must be DNS labels")
        if self.namespace is not None and self.node is not None and self.graph is None:
            raise ValueError("cross-namespace node references require an explicit graph")


@frozen
class TrafficRule:
    """
    Allow one peer on selected ports, with optional inbound mesh authorization.

    Attributes:
        peer (NetworkPeer): Peer selected by namespace, graph and pod labels.
        ports (tuple[NetworkPort, ...]): Destination ports; empty means every transport port.
        node (str | None): Local node subtree to which this rule applies; omitted selects every local node.
        principals (tuple[str, ...]): Exact Istio source identities such as cluster.local/ns/team/sa/client.
        methods (tuple[str, ...]): Allowed inbound HTTP methods; empty leaves methods unconstrained.
        paths (tuple[str, ...]): Exact inbound HTTP paths; empty leaves paths unconstrained.
    """

    peer: NetworkPeer
    ports: tuple[NetworkPort, ...] = ()
    node: str | None = None
    principals: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

    def __attrs_post_init__(self) -> None:
        """
        Validate the network contract before compilation.

        Returns:
            None: No return value.
        """
        if any(not re.fullmatch(r"[A-Z]+", method) for method in self.methods):
            raise ValueError("HTTP methods must be uppercase names")
        if any(not path.startswith("/") or "*" in path or "{" in path or "}" in path for path in self.paths):
            raise ValueError("HTTP authorization supports exact absolute paths")
        if any(not principal or "*" in principal for principal in self.principals):
            raise ValueError("mesh principals must be exact nonempty identities")
        if (self.principals or self.methods or self.paths) and any(port.protocol != "TCP" for port in self.ports):
            raise ValueError("mesh authorization requires TCP traffic")


@frozen
class NetworkAccess:
    """
    Isolate a graph subtree and intersect its exceptions with inherited constraints.

    Attributes:
        scope (Literal['Boundary', 'Subtree']): Apply to direct workloads or propagate to all descendant workloads.
        ingress (tuple[TrafficRule, ...]): Additional allowed inbound traffic.
        egress (tuple[TrafficRule, ...]): Additional allowed outbound traffic.
        isolateIngress (bool): Restrict incoming connections.
        isolateEgress (bool): Restrict outgoing connections.
        allowWithin (bool): Allow traffic between all workloads inside this boundary.
        allowDNS (bool): Allow TCP/UDP 53 to kube-system pods labeled k8s-app=kube-dns.
        mesh (bool): Require Istio injection, strict mutual TLS and inbound authorization.
    """

    scope: Literal["Boundary", "Subtree"] = "Subtree"
    ingress: tuple[TrafficRule, ...] = ()
    egress: tuple[TrafficRule, ...] = ()
    isolateIngress: bool = True
    isolateEgress: bool = True
    allowWithin: bool = True
    allowDNS: bool = True
    mesh: bool = False

    def __attrs_post_init__(self) -> None:
        """
        Validate the network contract before compilation.

        Returns:
            None: No return value.
        """
        if self.scope not in {"Boundary", "Subtree"}:
            raise ValueError("unknown network scope")
        if len(self.ingress) > 32 or len(self.egress) > 32:
            raise ValueError("each network direction supports at most 32 exceptions")
        if any(rule.principals or rule.methods or rule.paths for rule in self.egress):
            raise ValueError("Istio HTTP and source-identity authorization belongs on ingress at the destination")
        if not self.mesh and any(rule.principals or rule.methods or rule.paths for rule in self.ingress):
            raise ValueError("HTTP and identity constraints require network.mesh")
        if self.mesh and not self.isolateIngress:
            raise ValueError("mesh authorization requires isolated ingress")
