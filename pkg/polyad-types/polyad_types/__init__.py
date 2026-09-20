"""
Define and serialize the shared data exchanged by Polyad operators and applications.

Validated models cover resources, graph rules, replication, networking, API
requests, discovery, events and observed state. The operator, SDK and benchmarks
use these definitions, which also supply the generated JSON Schemas.
"""

from __future__ import annotations

from polyad_types.api.adaptation import AdaptationReport as AdaptationReport
from polyad_types.api.auth import APIKey as APIKey
from polyad_types.api.auth import Authentication as Authentication
from polyad_types.api.auth import CredentialAssignment as CredentialAssignment
from polyad_types.api.auth import GraphAccess as GraphAccess
from polyad_types.api.auth import KeyDirection as KeyDirection
from polyad_types.api.discovery import AccessMode as AccessMode
from polyad_types.api.discovery import AtlasAccess as AtlasAccess
from polyad_types.api.discovery import ServiceAccess as ServiceAccess
from polyad_types.api.discovery import ServiceEndpoint as ServiceEndpoint
from polyad_types.api.requests import ActivationRequest as ActivationRequest
from polyad_types.api.requests import CompositionItem as CompositionItem
from polyad_types.api.requests import CompositionRequest as CompositionRequest
from polyad_types.api.requests import ConnectionRequest as ConnectionRequest
from polyad_types.api.requests import ConnectionResponse as ConnectionResponse
from polyad_types.api.requests import ServiceConnectionRequest as ServiceConnectionRequest
from polyad_types.api.service_level import AdaptationBudget as AdaptationBudget
from polyad_types.api.service_level import ServiceLevelPolicy as ServiceLevelPolicy
from polyad_types.api.service_level import ServiceLevelReport as ServiceLevelReport
from polyad_types.api.throughput import DemandSample as DemandSample
from polyad_types.api.throughput import DemandSource as DemandSource
from polyad_types.api.throughput import ThroughputSample as ThroughputSample
from polyad_types.api.throughput import TrafficSample as TrafficSample
from polyad_types.events.codec import decode_event as decode_event
from polyad_types.events.envelope import Event as Event
from polyad_types.events.envelope import EventRebalanceSettings as EventRebalanceSettings
from polyad_types.events.envelope import EventStreamSettings as EventStreamSettings
from polyad_types.events.envelope import EventTooLarge as EventTooLarge
from polyad_types.events.models import ConnectionEvent as ConnectionEvent
from polyad_types.events.models import ControlEvent as ControlEvent
from polyad_types.events.models import CopulseEvent as CopulseEvent
from polyad_types.events.models import EventAST as EventAST
from polyad_types.events.models import GraphEvent as GraphEvent
from polyad_types.events.models import HeartbeatEvent as HeartbeatEvent
from polyad_types.events.models import TopologyEvent as TopologyEvent
from polyad_types.graphs.activation import ActivationPolicy as ActivationPolicy
from polyad_types.graphs.capacity import CapacityPlan as CapacityPlan
from polyad_types.graphs.capacity import CapacityTuning as CapacityTuning
from polyad_types.graphs.replication import RemoteScaleOwner as RemoteScaleOwner
from polyad_types.graphs.replication import ReplicaConnection as ReplicaConnection
from polyad_types.graphs.replication import ReplicaConnectivity as ReplicaConnectivity
from polyad_types.graphs.replication import ReplicaSource as ReplicaSource
from polyad_types.graphs.replication import ReplicaTemplate as ReplicaTemplate
from polyad_types.graphs.replication import Replication as Replication
from polyad_types.graphs.rules import Cheeger as Cheeger
from polyad_types.graphs.rules import CheegerComputation as CheegerComputation
from polyad_types.graphs.rules import CheegerReduction as CheegerReduction
from polyad_types.graphs.rules import Spectrum as Spectrum
from polyad_types.graphs.rules import StructuralRule as StructuralRule
from polyad_types.graphs.topology import Connection as Connection
from polyad_types.graphs.topology import Dependency as Dependency
from polyad_types.graphs.topology import GraphNode as GraphNode
from polyad_types.graphs.topology import Node as Node
from polyad_types.graphs.topology import Placement as Placement
from polyad_types.graphs.topology import ThroughputLayout as ThroughputLayout
from polyad_types.graphs.topology import ThroughputPolicy as ThroughputPolicy
from polyad_types.graphs.topology import ThroughputTier as ThroughputTier
from polyad_types.graphs.topology import Topology as Topology
from polyad_types.networking.access import MeshPeer as MeshPeer
from polyad_types.networking.access import NetworkAccess as NetworkAccess
from polyad_types.networking.access import NetworkPeer as NetworkPeer
from polyad_types.networking.access import NetworkPort as NetworkPort
from polyad_types.networking.access import TrafficRule as TrafficRule
from polyad_types.networking.traffic import TrafficDestination as TrafficDestination
from polyad_types.networking.traffic import TrafficResilience as TrafficResilience
from polyad_types.networking.traffic import TrafficRoute as TrafficRoute
from polyad_types.networking.traffic import TrafficWeights as TrafficWeights
from polyad_types.resources.autoscaling import VerticalPodAutoscaler as VerticalPodAutoscaler
from polyad_types.resources.base import Resource as Resource
from polyad_types.resources.capacity import CapacityNodeStatus as CapacityNodeStatus
from polyad_types.resources.capacity import CapacityStatus as CapacityStatus
from polyad_types.resources.capacity import PodSet as PodSet
from polyad_types.resources.capacity import PodTemplateReference as PodTemplateReference
from polyad_types.resources.capacity import ProvisioningRequestSpec as ProvisioningRequestSpec
from polyad_types.resources.codec import converter as converter
from polyad_types.resources.codec import encode_body as encode_body
from polyad_types.resources.codec import from_document as from_document
from polyad_types.resources.codec import to_document as to_document
from polyad_types.resources.common import AST as AST
from polyad_types.resources.common import GROUP as GROUP
from polyad_types.resources.common import VERSION as VERSION
from polyad_types.resources.common import DeleteOptions as DeleteOptions
from polyad_types.resources.common import ObjectMeta as ObjectMeta
from polyad_types.resources.common import OwnerReference as OwnerReference
from polyad_types.resources.common import ResourceType as ResourceType
from polyad_types.resources.common import StatusPatch as StatusPatch
from polyad_types.resources.common import UIDPreconditions as UIDPreconditions
from polyad_types.resources.infrastructure import PodTemplateResource as PodTemplateResource
from polyad_types.resources.infrastructure import PostgreSQLCluster as PostgreSQLCluster
from polyad_types.resources.infrastructure import ProvisioningRequest as ProvisioningRequest
from polyad_types.resources.istio import AuthorizationPolicy as AuthorizationPolicy
from polyad_types.resources.istio import DestinationRule as DestinationRule
from polyad_types.resources.istio import PeerAuthentication as PeerAuthentication
from polyad_types.resources.istio import VirtualService as VirtualService
from polyad_types.resources.kubernetes import ConfigMap as ConfigMap
from polyad_types.resources.kubernetes import DaemonSet as DaemonSet
from polyad_types.resources.kubernetes import DaemonSetSpec as DaemonSetSpec
from polyad_types.resources.kubernetes import Deployment as Deployment
from polyad_types.resources.kubernetes import DeploymentSpec as DeploymentSpec
from polyad_types.resources.kubernetes import DeploymentStrategy as DeploymentStrategy
from polyad_types.resources.kubernetes import Job as Job
from polyad_types.resources.kubernetes import JobSpec as JobSpec
from polyad_types.resources.kubernetes import LabelSelector as LabelSelector
from polyad_types.resources.kubernetes import Lease as Lease
from polyad_types.resources.kubernetes import LeaseSpec as LeaseSpec
from polyad_types.resources.kubernetes import NetworkPolicy as NetworkPolicy
from polyad_types.resources.kubernetes import PersistentVolumeClaim as PersistentVolumeClaim
from polyad_types.resources.kubernetes import Pod as Pod
from polyad_types.resources.kubernetes import PodTemplate as PodTemplate
from polyad_types.resources.kubernetes import Service as Service
from polyad_types.resources.kubernetes import StatefulSet as StatefulSet
from polyad_types.resources.kubernetes import StatefulSetSpec as StatefulSetSpec
from polyad_types.resources.mutations import Budget as Budget
from polyad_types.resources.mutations import BudgetDelta as BudgetDelta
from polyad_types.resources.mutations import Independence as Independence
from polyad_types.resources.mutations import Mutation as Mutation
from polyad_types.resources.mutations import MutationPlan as MutationPlan
from polyad_types.resources.mutations import Ordering as Ordering
from polyad_types.resources.mutations import Precondition as Precondition
from polyad_types.resources.mutations import Scope as Scope
from polyad_types.resources.polyad import Activation as Activation
from polyad_types.resources.polyad import Composition as Composition
from polyad_types.resources.polyad import Daemon as Daemon
from polyad_types.resources.polyad import Gate as Gate
from polyad_types.resources.polyad import Graph as Graph
from polyad_types.resources.polyad import GraphRule as GraphRule
from polyad_types.resources.polyad import PolyGraph as PolyGraph
from polyad_types.resources.polyad import ReplicaGroup as ReplicaGroup
from polyad_types.resources.polyad import ResourceDefinition as ResourceDefinition
from polyad_types.resources.polyad import Rewrite as Rewrite
from polyad_types.resources.polyad import ShutdownPolicy as ShutdownPolicy
from polyad_types.resources.polyad import TemporaryConnection as TemporaryConnection
from polyad_types.resources.polyad import Workload as Workload
from polyad_types.resources.registry import AUXILIARY_KINDS as AUXILIARY_KINDS
from polyad_types.resources.registry import BOUNDARY_KINDS as BOUNDARY_KINDS
from polyad_types.resources.registry import CAPACITY_KINDS as CAPACITY_KINDS
from polyad_types.resources.registry import NETWORK_POLICY_KINDS as NETWORK_POLICY_KINDS
from polyad_types.resources.registry import RESOURCE_TYPES as RESOURCE_TYPES
from polyad_types.resources.status import AdmissionMetrics as AdmissionMetrics
from polyad_types.resources.status import ConnectionMetrics as ConnectionMetrics
from polyad_types.resources.status import ExecutionMetrics as ExecutionMetrics
from polyad_types.resources.status import GraphMetrics as GraphMetrics
from polyad_types.resources.status import LayerMetrics as LayerMetrics
from polyad_types.resources.status import NodeCounts as NodeCounts
from polyad_types.resources.status import PhaseCounts as PhaseCounts
from polyad_types.resources.status import ResourceCounts as ResourceCounts
from polyad_types.resources.status import ResourceMetrics as ResourceMetrics
from polyad_types.resources.status import RollupMetrics as RollupMetrics
from polyad_types.resources.status import SubgraphMetrics as SubgraphMetrics
from polyad_types.resources.status import TopologyMetrics as TopologyMetrics
from polyad_types.resources.storage import Persistence as Persistence
from polyad_types.serialization import from_dict as from_dict
from polyad_types.serialization import to_dict as to_dict

__all__ = (
    "APIKey",
    "AST",
    "AUXILIARY_KINDS",
    "AccessMode",
    "Activation",
    "ActivationPolicy",
    "ActivationRequest",
    "AdaptationBudget",
    "AdaptationReport",
    "AdmissionMetrics",
    "AtlasAccess",
    "Authentication",
    "AuthorizationPolicy",
    "BOUNDARY_KINDS",
    "Budget",
    "BudgetDelta",
    "CAPACITY_KINDS",
    "CapacityNodeStatus",
    "CapacityPlan",
    "CapacityStatus",
    "CapacityTuning",
    "Cheeger",
    "CheegerComputation",
    "CheegerReduction",
    "Composition",
    "CompositionItem",
    "CompositionRequest",
    "ConfigMap",
    "Connection",
    "ConnectionEvent",
    "ConnectionMetrics",
    "ConnectionRequest",
    "ConnectionResponse",
    "ControlEvent",
    "CopulseEvent",
    "CredentialAssignment",
    "Daemon",
    "DaemonSet",
    "DaemonSetSpec",
    "DeleteOptions",
    "DemandSample",
    "DemandSource",
    "Dependency",
    "Deployment",
    "DeploymentSpec",
    "DeploymentStrategy",
    "DestinationRule",
    "Event",
    "EventAST",
    "EventRebalanceSettings",
    "EventStreamSettings",
    "EventTooLarge",
    "ExecutionMetrics",
    "GROUP",
    "Gate",
    "Graph",
    "GraphAccess",
    "GraphEvent",
    "GraphMetrics",
    "GraphNode",
    "GraphRule",
    "HeartbeatEvent",
    "Independence",
    "Job",
    "JobSpec",
    "KeyDirection",
    "LabelSelector",
    "LayerMetrics",
    "Lease",
    "LeaseSpec",
    "MeshPeer",
    "Mutation",
    "MutationPlan",
    "NETWORK_POLICY_KINDS",
    "NetworkAccess",
    "NetworkPeer",
    "NetworkPolicy",
    "NetworkPort",
    "Node",
    "NodeCounts",
    "ObjectMeta",
    "Ordering",
    "OwnerReference",
    "PeerAuthentication",
    "Persistence",
    "PersistentVolumeClaim",
    "PhaseCounts",
    "Placement",
    "Pod",
    "PodSet",
    "PodTemplate",
    "PodTemplateReference",
    "PodTemplateResource",
    "PolyGraph",
    "PostgreSQLCluster",
    "Precondition",
    "ProvisioningRequest",
    "ProvisioningRequestSpec",
    "RESOURCE_TYPES",
    "RemoteScaleOwner",
    "ReplicaConnection",
    "ReplicaConnectivity",
    "ReplicaGroup",
    "ReplicaSource",
    "ReplicaTemplate",
    "Replication",
    "Resource",
    "ResourceCounts",
    "ResourceDefinition",
    "ResourceMetrics",
    "ResourceType",
    "Rewrite",
    "RollupMetrics",
    "Scope",
    "Service",
    "ServiceAccess",
    "ServiceConnectionRequest",
    "ServiceEndpoint",
    "ServiceLevelPolicy",
    "ServiceLevelReport",
    "ShutdownPolicy",
    "Spectrum",
    "StatefulSet",
    "StatefulSetSpec",
    "StatusPatch",
    "StructuralRule",
    "SubgraphMetrics",
    "TemporaryConnection",
    "ThroughputLayout",
    "ThroughputPolicy",
    "ThroughputSample",
    "ThroughputTier",
    "Topology",
    "TopologyEvent",
    "TopologyMetrics",
    "TrafficDestination",
    "TrafficResilience",
    "TrafficRoute",
    "TrafficRule",
    "TrafficSample",
    "TrafficWeights",
    "UIDPreconditions",
    "VERSION",
    "VerticalPodAutoscaler",
    "VirtualService",
    "Workload",
    "converter",
    "decode_event",
    "encode_body",
    "from_dict",
    "from_document",
    "to_dict",
    "to_document",
)
