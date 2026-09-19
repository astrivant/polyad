"""
Describe Polyad custom resource envelopes and their API identities.
"""

from __future__ import annotations

from typing import ClassVar

from attrs import frozen

from polyad_types.resources.base import SpecResource
from polyad_types.resources.common import GROUP, VERSION, ResourceType


@frozen(kw_only=True)
class Graph(SpecResource):
    """
    A cluster-local Polyad graph boundary validated by the graph library.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Graph",
        f"{GROUP}/{VERSION}",
        "graphs",
        boundary=True,
        description="Cluster-local scheduling boundary for dependent workload vertices.",
        graph_owned=True,
        reconciled=True,
        composable=True,
    )


@frozen(kw_only=True)
class PolyGraph(SpecResource):
    """
    A composition boundary for local graphs and optionally remote Graphs and PolyGraphs.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "PolyGraph",
        f"{GROUP}/{VERSION}",
        "polygraphs",
        boundary=True,
        description="Scheduling boundary composed of nested graph types.",
        graph_owned=True,
        reconciled=True,
        composable=True,
    )


@frozen(kw_only=True)
class Workload(SpecResource):
    """
    A reusable finite workload definition.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Workload", f"{GROUP}/{VERSION}", "workloads", description="Reusable finite workload definition.", definition=True, composable=True
    )


@frozen(kw_only=True)
class Daemon(SpecResource):
    """
    A reusable persistent workload definition.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Daemon", f"{GROUP}/{VERSION}", "daemons", description="Reusable persistent service definition.", definition=True, composable=True
    )


@frozen(kw_only=True)
class ResourceDefinition(SpecResource):
    """
    A Polyad Resource definition, distinct from the Kubernetes AST base class.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Resource",
        f"{GROUP}/{VERSION}",
        "resources",
        description="Reusable native resource manifest definition.",
        definition=True,
        composable=True,
    )


@frozen(kw_only=True)
class Gate(SpecResource):
    """
    A reusable Boolean admission expression.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Gate", f"{GROUP}/{VERSION}", "gates", description="Reusable Boolean admission condition.", definition=True, composable=True
    )


@frozen(kw_only=True)
class ShutdownPolicy(SpecResource):
    """
    A reusable runtime limit and termination contract.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "ShutdownPolicy",
        f"{GROUP}/{VERSION}",
        "shutdownpolicies",
        description="Reusable execution limit and termination contract.",
        definition=True,
        composable=True,
    )


@frozen(kw_only=True)
class Rewrite(SpecResource):
    """
    A generation-fenced graph revision request.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Rewrite", f"{GROUP}/{VERSION}", "rewrites", description="Generation-fenced graph revision request.", reconciled=True
    )


@frozen(kw_only=True)
class GraphRule(SpecResource):
    """
    An engineer-managed structural graph policy.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "GraphRule",
        f"{GROUP}/{VERSION}",
        "graphrules",
        description="Structural and network policy governing graph admission.",
        definition=True,
    )


@frozen(kw_only=True)
class Composition(SpecResource):
    """
    An immutable composition request and its generated resource audit inventory.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Composition",
        f"{GROUP}/{VERSION}",
        "compositions",
        description="Immutable composition request and generated identity receipt.",
        reconciled=True,
    )


@frozen(kw_only=True)
class ReplicaGroup(SpecResource):
    """
    A scalable family of stable copies of a workload or graph definition.

    Attributes:
        resource_type (ClassVar[ResourceType]): Scalable replication boundary identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "ReplicaGroup",
        f"{GROUP}/{VERSION}",
        "replicagroups",
        boundary=True,
        description="Bounded replication of workload and graph abstractions through the scale subresource.",
        graph_owned=True,
        reconciled=True,
        composable=True,
    )


@frozen(kw_only=True)
class Activation(SpecResource):
    """
    An immutable activation receipt whose execution belongs to its parent graph.

    Attributes:
        resource_type (ClassVar[ResourceType]): Namespaced activation API identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Activation",
        f"{GROUP}/{VERSION}",
        "activations",
        description="Durable pulse request, admission decision and execution identity.",
        graph_owned=True,
        reconciled=True,
        auxiliary="activation",
    )


@frozen(kw_only=True)
class TemporaryConnection(SpecResource):
    """
    An expiring connection receipt owned by a persisted graph instance.

    Attributes:
        resource_type (ClassVar[ResourceType]): Namespaced connection receipt identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "TemporaryConnection",
        f"{GROUP}/{VERSION}",
        "temporaryconnections",
        description="TTL-bound graph connection request and admission receipt.",
        graph_owned=True,
        reconciled=True,
        auxiliary="connection",
    )


@frozen(kw_only=True)
class DragonflyPool(SpecResource):
    """
    Bounded scale intent for the operator's bundled HA cache.

    Attributes:
        resource_type (ClassVar[ResourceType]): Internal cache scaling API identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "DragonflyPool", f"{GROUP}/{VERSION}", "dragonflypools", description="Bounded replica requests for bundled HA Dragonfly."
    )


@frozen(kw_only=True)
class OperatorPool(SpecResource):
    """
    Root-managed remote operator execution capacity.

    Attributes:
        resource_type (ClassVar[ResourceType]): Root control-plane API identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "OperatorPool", f"{GROUP}/{VERSION}", "operatorpools", description="Root-managed remote operator execution capacity."
    )


@frozen(kw_only=True)
class RemoteScale(SpecResource):
    """
    Root-local scale intent for a remote ReplicaGroup.

    Attributes:
        resource_type (ClassVar[ResourceType]): Root control-plane API identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "RemoteScale", f"{GROUP}/{VERSION}", "remotescales", description="Root-local scale intent for a remote ReplicaGroup."
    )
