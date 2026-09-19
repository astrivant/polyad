"""
Derive resource registries and kind sets from canonical API descriptors.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from polyad_types.resources.autoscaling import VerticalPodAutoscaler
from polyad_types.resources.infrastructure import Dragonfly, PodTemplateResource, PostgreSQLCluster, ProvisioningRequest
from polyad_types.resources.istio import AuthorizationPolicy, DestinationRule, PeerAuthentication, VirtualService
from polyad_types.resources.kubernetes import (
    ConfigMap,
    CustomResourceDefinition,
    DaemonSet,
    Deployment,
    Job,
    Lease,
    NetworkPolicy,
    PersistentVolumeClaim,
    Pod,
    ReplicaSet,
    Secret,
    Service,
    ServiceAccount,
    StatefulSet,
)
from polyad_types.resources.polyad import (
    Activation,
    Composition,
    Daemon,
    DragonflyPool,
    Gate,
    Graph,
    GraphRule,
    OperatorPool,
    PolyGraph,
    RemoteScale,
    ReplicaGroup,
    ResourceDefinition,
    Rewrite,
    ShutdownPolicy,
    TemporaryConnection,
    Workload,
)

if TYPE_CHECKING:
    from polyad_types.resources.base import Resource

RESOURCE_CLASSES: tuple[type[Resource], ...] = (
    VerticalPodAutoscaler,
    PostgreSQLCluster,
    Secret,
    CustomResourceDefinition,
    Dragonfly,
    DragonflyPool,
    OperatorPool,
    RemoteScale,
    Activation,
    TemporaryConnection,
    ReplicaGroup,
    ProvisioningRequest,
    PodTemplateResource,
    NetworkPolicy,
    AuthorizationPolicy,
    PeerAuthentication,
    VirtualService,
    DestinationRule,
    GraphRule,
    Composition,
    Pod,
    ReplicaSet,
    ServiceAccount,
    Job,
    Deployment,
    DaemonSet,
    StatefulSet,
    Lease,
    Service,
    ConfigMap,
    PersistentVolumeClaim,
    Graph,
    PolyGraph,
    Workload,
    Daemon,
    ResourceDefinition,
    Gate,
    ShutdownPolicy,
    Rewrite,
)
# Immutable projections of the descriptors declared on the resource classes.
RESOURCE_TYPES = MappingProxyType({cls.resource_type.kind: cls.resource_type for cls in RESOURCE_CLASSES})
RESOURCE_REGISTRY = MappingProxyType({cls.resource_type.kind: cls for cls in RESOURCE_CLASSES})
BOUNDARY_KINDS = frozenset(kind for kind, descriptor in RESOURCE_TYPES.items() if descriptor.boundary)
GRAPH_OWNED_KINDS = frozenset(kind for kind, descriptor in RESOURCE_TYPES.items() if descriptor.graph_owned)
DEFINITION_KINDS = frozenset(kind for kind, descriptor in RESOURCE_TYPES.items() if descriptor.definition)
RECONCILED_KINDS = frozenset(kind for kind, descriptor in RESOURCE_TYPES.items() if descriptor.reconciled)
COMPOSABLE_KINDS = frozenset(kind for kind, descriptor in RESOURCE_TYPES.items() if descriptor.composable)
POLYAD_KINDS = frozenset(kind for kind, descriptor in RESOURCE_TYPES.items() if descriptor.polyad)
NETWORK_POLICY_KINDS = frozenset(kind for kind, descriptor in RESOURCE_TYPES.items() if descriptor.auxiliary == "network")
CAPACITY_KINDS = frozenset(kind for kind, descriptor in RESOURCE_TYPES.items() if descriptor.auxiliary == "capacity")
AUXILIARY_KINDS = frozenset(kind for kind, descriptor in RESOURCE_TYPES.items() if descriptor.auxiliary is not None)
