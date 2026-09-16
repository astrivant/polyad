"""
Concrete resource kinds and the execution specs synthesized by the compiler.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, ClassVar

from attrs import field, frozen

from polyad_types.resources.common import AST, GROUP, VERSION, ObjectMeta, ResourceType


@frozen(kw_only=True)
class Resource(AST):
    """
    Represent a Kubernetes resource with an immutable kind and API identity.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
        metadata (ObjectMeta): Object identity, ownership and concurrency metadata.
        status (dict[str, Any] | None): Observed resource state.
    """

    resource_type: ClassVar[ResourceType]
    metadata: ObjectMeta
    status: dict[str, Any] | None = None

    def __attrs_post_init__(self) -> None:
        """
        Require a named object before it can be emitted to Kubernetes.

        Returns:
            None: No return value.
        """
        if not self.metadata.name:
            raise ValueError("resource metadata requires a name")


@frozen(kw_only=True)
class PodTemplate(AST):
    """
    Retain application pod settings and typed pod-template metadata.

    Attributes:
        spec (dict[str, Any]): Desired resource configuration.
        metadata (ObjectMeta | None): Object identity, ownership and concurrency metadata.
    """

    spec: dict[str, Any]
    metadata: ObjectMeta | None = None


@frozen(kw_only=True)
class JobSpec(AST):
    """
    Describe finite execution and its retry budget.

    Attributes:
        template (PodTemplate): Pod template instantiated by the workload controller.
        backoffLimit (int): Maximum failed Job retries before terminal failure.
    """

    template: PodTemplate
    backoffLimit: int = 6


@frozen(kw_only=True)
class LabelSelector(AST):
    """
    Identify workload pods while preserving native selector expressions.

    Attributes:
        matchLabels (dict[str, str]): Labels identifying pods owned by this workload.
    """

    matchLabels: dict[str, str]


@frozen(kw_only=True)
class DeploymentStrategy(AST):
    """
    Describe a daemon's deployment update strategy.

    Attributes:
        type (str): Kubernetes Deployment update strategy.
    """

    type: str


@frozen(kw_only=True)
class DeploymentSpec(AST):
    """
    Describe persistent execution with explicit pod ownership and replica count.

    Attributes:
        template (PodTemplate): Pod template instantiated by the workload controller.
        selector (LabelSelector): Selector identifying the Deployment pods.
        replicas (int): Desired number of persistent workload replicas.
        strategy (DeploymentStrategy): Policy for replacing pods during Deployment updates.
    """

    template: PodTemplate
    selector: LabelSelector
    replicas: int
    strategy: DeploymentStrategy


@frozen(kw_only=True)
class DaemonSetSpec(AST):
    """
    Run one persistent Pod on each eligible node without a replica-count field.

    Attributes:
        template (PodTemplate): Application Pod template and node placement.
        selector (LabelSelector): Operator-managed Pod selector.
        updateStrategy (dict[str, Any]): Native RollingUpdate or OnDelete policy.
        minReadySeconds (int): Minimum ready duration before a Pod is available.
    """

    template: PodTemplate
    selector: LabelSelector
    updateStrategy: dict[str, Any] = field(factory=lambda: {"type": "RollingUpdate"})
    minReadySeconds: int = 0


@frozen(kw_only=True)
class DaemonSet(Resource):
    """
    An apps/v1 persistent workload scheduled by node eligibility.

    Attributes:
        resource_type (ClassVar[ResourceType]): API identity and graph ownership descriptor.
        spec (DaemonSetSpec): Desired controller configuration.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "DaemonSet", "apps/v1", "daemonsets", description="One daemon execution per eligible node.", graph_owned=True
    )
    spec: DaemonSetSpec


@frozen(kw_only=True)
class StatefulSetSpec(AST):
    """
    Describe persistent execution with stable Pod identities and per-replica claims.

    Attributes:
        template (PodTemplate): Application Pod template, including volume mounts.
        selector (LabelSelector): Operator-managed Pod selector.
        replicas (int): Desired number of Pods in this set.
        serviceName (str): Governing headless Service name.
        volumeClaimTemplates (tuple[dict[str, Any], ...]): Native PVC templates, preserving metadata and storage specifications.
        podManagementPolicy (str): OrderedReady or Parallel Pod management.
        updateStrategy (dict[str, Any]): Native RollingUpdate or OnDelete policy.
        persistentVolumeClaimRetentionPolicy (dict[str, str]): Explicit retention on set deletion and native scale-in.
        minReadySeconds (int): Minimum ready duration before a Pod is available.
        revisionHistoryLimit (int): Number of retained controller revisions.
        ordinals (dict[str, int] | None): Optional starting Pod ordinal.
    """

    template: PodTemplate
    selector: LabelSelector
    replicas: int
    serviceName: str
    volumeClaimTemplates: tuple[dict[str, Any], ...] = ()
    podManagementPolicy: str = "OrderedReady"
    updateStrategy: dict[str, Any] = field(factory=lambda: {"type": "RollingUpdate"})
    persistentVolumeClaimRetentionPolicy: dict[str, str] = field(factory=lambda: {"whenDeleted": "Retain", "whenScaled": "Retain"})
    minReadySeconds: int = 0
    revisionHistoryLimit: int = 10
    ordinals: dict[str, int] | None = None


@frozen(kw_only=True)
class Job(Resource):
    """
    A batch/v1 finite workload.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
        spec (JobSpec): Desired resource configuration.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Job", "batch/v1", "jobs", description="Finite workload execution.", graph_owned=True
    )
    spec: JobSpec


@frozen(kw_only=True)
class Deployment(Resource):
    """
    An apps/v1 persistent workload.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
        spec (DeploymentSpec): Desired resource configuration.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Deployment", "apps/v1", "deployments", description="Persistent daemon execution.", graph_owned=True
    )
    spec: DeploymentSpec


@frozen(kw_only=True)
class StatefulSet(Resource):
    """
    An apps/v1 persistent workload with stable identity and storage.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
        spec (StatefulSetSpec): Desired resource configuration.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "StatefulSet", "apps/v1", "statefulsets", description="Stateful daemon execution.", graph_owned=True
    )
    spec: StatefulSetSpec


@frozen(kw_only=True)
class SpecResource(Resource):
    """
    Preserve open native or CRD spec fields without duplicating their schemas.

    Attributes:
        spec (dict[str, Any]): Desired resource configuration.
    """

    spec: dict[str, Any]


@frozen(kw_only=True)
class LeaseSpec(AST):
    """
    Describe a renewable coordination claim and its holder identity.

    Attributes:
        holderIdentity (str | None): Unique process identity currently claiming the Lease.
        leaseDurationSeconds (int | None): Duration of the claim before renewal is required.
        renewTime (str | None): UTC timestamp of the latest claim renewal.
    """

    holderIdentity: str | None = None
    leaseDurationSeconds: int | None = None
    renewTime: str | None = None


@frozen(kw_only=True)
class Lease(Resource):
    """
    A coordination.k8s.io/v1 leader, membership or shard lease.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
        spec (LeaseSpec): Desired resource configuration.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Lease", "coordination.k8s.io/v1", "leases", description="Replica membership, leadership and shard coordination."
    )
    spec: LeaseSpec


@frozen(kw_only=True)
class Service(SpecResource):
    """
    A core/v1 Service exposing graph workloads.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Service", "v1", "services", description="Network endpoint for graph workloads.", graph_owned=True
    )


@frozen(kw_only=True)
class PersistentVolumeClaim(SpecResource):
    """
    A core/v1 request for graph-owned persistent storage.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "PersistentVolumeClaim",
        "v1",
        "persistentvolumeclaims",
        description="Persistent storage claimed by graph workloads.",
        graph_owned=True,
    )


@frozen(kw_only=True)
class ConfigMap(Resource):
    """
    A core/v1 ConfigMap with data fields at the document root, without a spec.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
        data (dict[str, str] | None): UTF-8 ConfigMap entries.
        binaryData (dict[str, str] | None): Base64-encoded ConfigMap entries.
        immutable (bool | None): Whether Kubernetes should reject changes to ConfigMap data.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "ConfigMap", "v1", "configmaps", description="Configuration data owned by a graph.", graph_owned=True
    )
    data: dict[str, str] | None = None
    binaryData: dict[str, str] | None = None
    immutable: bool | None = None


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
class Pod(SpecResource):
    """
    A native Pod observed for composition audit traces.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for read-only API routing.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Pod",
        "v1",
        "pods",
        description="Capacity reservation or observed workload Pod.",
        graph_owned=True,
        auxiliary="capacity",
        required_feature="capacity",
    )


@frozen(kw_only=True)
class NetworkPolicy(SpecResource):
    """
    A managed NetworkPolicy network resource.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for network API routing.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "NetworkPolicy",
        "networking.k8s.io/v1",
        "networkpolicies",
        description="Pod network isolation policy.",
        graph_owned=True,
        auxiliary="network",
    )


@frozen(kw_only=True)
class AuthorizationPolicy(SpecResource):
    """
    A managed AuthorizationPolicy network resource.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for network API routing.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "AuthorizationPolicy",
        "security.istio.io/v1",
        "authorizationpolicies",
        description="Optional Istio HTTP and service identity authorization.",
        graph_owned=True,
        auxiliary="network",
        required_feature="mesh",
    )


@frozen(kw_only=True)
class PeerAuthentication(SpecResource):
    """
    A managed PeerAuthentication network resource.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for network API routing.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "PeerAuthentication",
        "security.istio.io/v1",
        "peerauthentications",
        description="Optional Istio mutual TLS authentication policy.",
        graph_owned=True,
        auxiliary="network",
        required_feature="mesh",
    )


@frozen(kw_only=True)
class ProvisioningRequest(SpecResource):
    """
    A namespaced request for future workload capacity.

    Attributes:
        resource_type (ClassVar[ResourceType]): Autoscaler API identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "ProvisioningRequest",
        "autoscaling.x-k8s.io/v1",
        "provisioningrequests",
        description="Optional cluster autoscaler capacity request.",
        graph_owned=True,
        auxiliary="capacity",
        required_feature="capacity",
    )


@frozen(kw_only=True)
class PodTemplateResource(Resource):
    """
    A core PodTemplate object consumed by the node autoscaler.

    Attributes:
        resource_type (ClassVar[ResourceType]): Native PodTemplate API identity.
        template (PodTemplate): Future workload's scheduling specification.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "PodTemplate",
        "v1",
        "podtemplates",
        description="Future workload template for node capacity provisioning.",
        graph_owned=True,
        auxiliary="capacity",
        required_feature="capacity",
    )
    template: PodTemplate


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
class Dragonfly(SpecResource):
    """
    Upstream Dragonfly instance managed through its declared replica count.

    Attributes:
        resource_type (ClassVar[ResourceType]): Upstream cache API identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Dragonfly", "dragonflydb.io/v1alpha1", "dragonflies", description="Upstream Dragonfly cache instance."
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


@frozen(kw_only=True)
class Secret(Resource):
    """
    A root-managed Secret used to bootstrap execution replicas.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kubernetes management API identity.
        data (dict[str, str]): Base64-encoded credential entries.
        type (str): Native Kubernetes Secret type.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Secret", "v1", "secrets", description="Root-managed worker credentials.")
    data: dict[str, str] = field(factory=dict)
    type: str = "Opaque"


@frozen(kw_only=True)
class CustomResourceDefinition(SpecResource):
    """
    A root-managed CustomResourceDefinition used to bootstrap execution replicas.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kubernetes management API identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "CustomResourceDefinition",
        "apiextensions.k8s.io/v1",
        "customresourcedefinitions",
        namespaced=False,
        description="Root-installed Polyad API schemas.",
    )


RESOURCE_CLASSES: tuple[type[Resource], ...] = (
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
    GraphRule,
    Composition,
    Pod,
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
# Immutable projections of the descriptors declared on the AST classes above.
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
