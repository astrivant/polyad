"""
Identify discoverable graph services and administrator-defined access ceilings.
"""

from __future__ import annotations

import re
from enum import StrEnum

from attrs import field, frozen

__all__ = (
    "AccessMode",
    "AtlasAccess",
    "ServiceAccess",
    "ServiceEndpoint",
)


class AccessMode(StrEnum):
    """
    Order service access from disabled through the complete registered atlas.

    Attributes:
        DISABLED: Reject the capability.
        SAME_GRAPH: Permit only the exact home boundary.
        GRAPH_TREE: Permit related graph branches in the home cluster.
        CLUSTER: Permit the home cluster.
        ATLAS: Permit registered clusters subject to explicit authorization.
    """

    DISABLED = "Disabled"
    SAME_GRAPH = "SameGraph"
    GRAPH_TREE = "GraphTree"
    CLUSTER = "Cluster"
    ATLAS = "Atlas"


@frozen
class ServiceAccess:
    """
    Limit discovery and connection negotiation independently.

    Attributes:
        discovery (AccessMode): Maximum discoverable scope relative to a service's home graph.
        connections (AccessMode): Maximum negotiable scope; consent and GraphRules remain required.
        parent (str): Parent operator's registered cluster; empty selects the atlas root.
    """

    discovery: AccessMode = AccessMode.GRAPH_TREE
    connections: AccessMode = AccessMode.SAME_GRAPH
    parent: str = ""


@frozen
class ServiceEndpoint:
    """
    Address one logical service with its exact graph incarnation and execution cluster.

    Attributes:
        cluster (str): Registered cluster hosting this endpoint; empty selects local execution.
        namespace (str): Namespace of the graph instance.
        kind (str): Graph, PolyGraph or ReplicaGroup boundary kind.
        graph (str): Persisted graph instance name.
        graphUid (str): Graph UID fencing deletion and replacement.
        node (str): Logical node or replica ordinal within that boundary.
    """

    cluster: str
    namespace: str
    kind: str
    graph: str
    graphUid: str
    node: str

    def __attrs_post_init__(self) -> None:
        """
        Reject ambiguous graph addresses before discovery or negotiation.

        Returns:
            None: Invalid identities raise ValueError.
        """
        for value in (self.namespace, self.node, self.cluster or "local"):
            if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", value):
                raise ValueError("endpoint cluster, namespace and node must be DNS labels")
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", self.graph):
            raise ValueError("endpoint graph must be a Kubernetes resource name")
        if self.kind not in {"Graph", "PolyGraph", "ReplicaGroup"} or not 1 <= len(self.graphUid) <= 128:
            raise ValueError("endpoint requires a graph kind and UID")


@frozen
class AtlasAccess:
    """
    Intersect operator access ceilings along each configured parent chain.

    Attributes:
        discovery (AccessMode): Root operator's discovery ceiling.
        connections (AccessMode): Root operator's negotiation ceiling.
        clusters (dict[str, ServiceAccess]): Registered child operators and their narrower ceilings.
        trustDomains (dict[str, str]): Explicit Istio trust domains for cross-cluster service identities.
    """

    discovery: AccessMode = AccessMode.GRAPH_TREE
    connections: AccessMode = AccessMode.SAME_GRAPH
    clusters: dict[str, ServiceAccess] = field(factory=dict)
    trustDomains: dict[str, str] = field(factory=dict)

    def __attrs_post_init__(self) -> None:
        """
        Reject cyclic or unresolved parent chains and invalid mode values.

        Returns:
            None: Access cannot fall back to a broader parent when configuration is invalid.
        """
        modes = [
            self.discovery,
            self.connections,
            *(mode for child in self.clusters.values() for mode in (child.discovery, child.connections)),
        ]
        for mode in modes:
            if mode not in set(AccessMode):
                raise ValueError("unsupported service access mode")
        if len(self.clusters) > 32 or len(self.trustDomains) > 33:
            raise ValueError("service access supports at most 32 child clusters")
        for cluster in self.clusters.keys() | self.trustDomains.keys():
            if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", cluster):
                raise ValueError("service access cluster names must be DNS labels")
            self.effective(cluster, "discovery")
        for domain in self.trustDomains.values():
            if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", domain):
                raise ValueError("mesh trust domains must be DNS names")

    def effective(self, cluster: str, capability: str) -> AccessMode:
        """
        Find the most restrictive ceiling from a child to the root operator.

        Args:
            cluster (str): Service's home cluster.
            capability (str): Discovery or connections.

        Returns:
            AccessMode: Intersection of every administrator-defined ceiling on the path.
        """
        if capability not in {"discovery", "connections"}:
            raise ValueError("unknown service access capability")

        # Modes are ordered from narrowest to broadest; every ancestor may only reduce access.
        order = list(AccessMode)
        ceiling = order.index(getattr(self, capability))
        seen = set()

        # Follow explicit parents, rejecting cycles instead of silently granting root privileges.
        while cluster in self.clusters:
            if cluster in seen:
                raise ValueError("operator access parent chain contains a cycle")
            seen.add(cluster)
            child = self.clusters[cluster]
            ceiling = min(ceiling, order.index(getattr(child, capability)))
            cluster = child.parent
            if cluster and cluster not in self.clusters:
                raise ValueError("operator access parent is not configured")
        return order[ceiling]
