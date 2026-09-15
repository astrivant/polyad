"""
Concrete resource kinds and the execution specs synthesized by the compiler.
"""

from __future__ import annotations

from typing import Any, ClassVar

from attrs import frozen

from polyad.compiler.asts.common import AST, GROUP, VERSION, ObjectMeta, ResourceType


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
class Job(Resource):
    """
    A batch/v1 finite workload.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
        spec (JobSpec): Desired resource configuration.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Job", "batch/v1", "jobs")
    spec: JobSpec


@frozen(kw_only=True)
class Deployment(Resource):
    """
    An apps/v1 persistent workload.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
        spec (DeploymentSpec): Desired resource configuration.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Deployment", "apps/v1", "deployments")
    spec: DeploymentSpec


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

    resource_type: ClassVar[ResourceType] = ResourceType("Lease", "coordination.k8s.io/v1", "leases")
    spec: LeaseSpec


@frozen(kw_only=True)
class Service(SpecResource):
    """
    A core/v1 Service exposing graph workloads.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Service", "v1", "services")


@frozen(kw_only=True)
class PersistentVolumeClaim(SpecResource):
    """
    A core/v1 request for graph-owned persistent storage.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("PersistentVolumeClaim", "v1", "persistentvolumeclaims")


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

    resource_type: ClassVar[ResourceType] = ResourceType("ConfigMap", "v1", "configmaps")
    data: dict[str, str] | None = None
    binaryData: dict[str, str] | None = None
    immutable: bool | None = None


@frozen(kw_only=True)
class Graph(SpecResource):
    """
    A Polyad graph boundary; its topology is validated by the graph library.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Graph", f"{GROUP}/{VERSION}", "graphs", boundary=True)


@frozen(kw_only=True)
class PolyGraph(SpecResource):
    """
    A composition boundary whose nodes are graphs of any supported boundary type.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("PolyGraph", f"{GROUP}/{VERSION}", "polygraphs", boundary=True)


@frozen(kw_only=True)
class EphemeralGraph(SpecResource):
    """
    A graph boundary targeting interruptible capacity.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("EphemeralGraph", f"{GROUP}/{VERSION}", "ephemeralgraphs", boundary=True)


@frozen(kw_only=True)
class Feedback(SpecResource):
    """
    A boundary that executes durable graph epochs.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Feedback", f"{GROUP}/{VERSION}", "feedbacks", boundary=True)


@frozen(kw_only=True)
class Workload(SpecResource):
    """
    A reusable finite workload definition.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Workload", f"{GROUP}/{VERSION}", "workloads")


@frozen(kw_only=True)
class Daemon(SpecResource):
    """
    A reusable persistent workload definition.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Daemon", f"{GROUP}/{VERSION}", "daemons")


@frozen(kw_only=True)
class Ephemeral(SpecResource):
    """
    A reusable finite workload definition for interruptible capacity.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Ephemeral", f"{GROUP}/{VERSION}", "ephemerals")


@frozen(kw_only=True)
class ResourceDefinition(SpecResource):
    """
    A Polyad Resource definition, distinct from the Kubernetes AST base class.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Resource", f"{GROUP}/{VERSION}", "resources")


@frozen(kw_only=True)
class Gate(SpecResource):
    """
    A reusable Boolean admission expression.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Gate", f"{GROUP}/{VERSION}", "gates")


@frozen(kw_only=True)
class ShutdownPolicy(SpecResource):
    """
    A reusable runtime limit and termination contract.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("ShutdownPolicy", f"{GROUP}/{VERSION}", "shutdownpolicies")


@frozen(kw_only=True)
class Rewrite(SpecResource):
    """
    A generation-fenced graph revision request.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor used for API routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Rewrite", f"{GROUP}/{VERSION}", "rewrites")


@frozen(kw_only=True)
class GraphRule(SpecResource):
    """
    An engineer-managed structural graph policy.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("GraphRule", f"{GROUP}/{VERSION}", "graphrules")


@frozen(kw_only=True)
class Composition(SpecResource):
    """
    An immutable composition request and its generated resource audit inventory.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for routing and serialization.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Composition", f"{GROUP}/{VERSION}", "compositions")


@frozen(kw_only=True)
class Pod(SpecResource):
    """
    A native Pod observed for composition audit traces.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for read-only API routing.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("Pod", "v1", "pods")


@frozen(kw_only=True)
class NetworkPolicy(SpecResource):
    """
    A managed NetworkPolicy network resource.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for network API routing.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("NetworkPolicy", "networking.k8s.io/v1", "networkpolicies")


@frozen(kw_only=True)
class AuthorizationPolicy(SpecResource):
    """
    A managed AuthorizationPolicy network resource.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for network API routing.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("AuthorizationPolicy", "security.istio.io/v1", "authorizationpolicies")


@frozen(kw_only=True)
class PeerAuthentication(SpecResource):
    """
    A managed PeerAuthentication network resource.

    Attributes:
        resource_type (ClassVar[ResourceType]): Kind descriptor for network API routing.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("PeerAuthentication", "security.istio.io/v1", "peerauthentications")


RESOURCE_CLASSES: tuple[type[Resource], ...] = (
    NetworkPolicy,
    AuthorizationPolicy,
    PeerAuthentication,
    GraphRule,
    Composition,
    Pod,
    Job,
    Deployment,
    Lease,
    Service,
    ConfigMap,
    PersistentVolumeClaim,
    Graph,
    PolyGraph,
    EphemeralGraph,
    Feedback,
    Workload,
    Daemon,
    Ephemeral,
    ResourceDefinition,
    Gate,
    ShutdownPolicy,
    Rewrite,
)
RESOURCE_TYPES = {cls.resource_type.kind: cls.resource_type for cls in RESOURCE_CLASSES}
RESOURCE_REGISTRY = {cls.resource_type.kind: cls for cls in RESOURCE_CLASSES}
BOUNDARY_KINDS = frozenset(kind for kind, descriptor in RESOURCE_TYPES.items() if descriptor.boundary)

NETWORK_POLICY_KINDS = frozenset({"NetworkPolicy", "AuthorizationPolicy", "PeerAuthentication"})
