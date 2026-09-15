"""Concrete resource kinds and the execution specs synthesized by the compiler."""

from typing import Any, ClassVar

from attrs import frozen

from polyad.operator.compiler.asts.common import AST, GROUP, VERSION, ObjectMeta, ResourceType


@frozen(kw_only=True)
class Resource(AST):
    """Represent a Kubernetes resource with an immutable kind and API identity."""

    resource_type: ClassVar[ResourceType]
    metadata: ObjectMeta
    status: dict[str, Any] | None = None

    def __attrs_post_init__(self) -> None:
        """Require a named object before it can be emitted to Kubernetes."""
        if not self.metadata.name:
            raise ValueError("resource metadata requires a name")


@frozen(kw_only=True)
class PodTemplate(AST):
    """Retain application pod settings and typed pod-template metadata."""

    spec: dict[str, Any]
    metadata: ObjectMeta | None = None


@frozen(kw_only=True)
class JobSpec(AST):
    """Describe finite execution and its retry budget."""

    template: PodTemplate
    backoffLimit: int = 6


@frozen(kw_only=True)
class LabelSelector(AST):
    """Identify workload pods while preserving native selector expressions."""

    matchLabels: dict[str, str]


@frozen(kw_only=True)
class DeploymentStrategy(AST):
    """Describe a daemon's deployment update strategy."""

    type: str


@frozen(kw_only=True)
class DeploymentSpec(AST):
    """Describe persistent execution with explicit pod ownership and replica count."""

    template: PodTemplate
    selector: LabelSelector
    replicas: int
    strategy: DeploymentStrategy


@frozen(kw_only=True)
class Job(Resource):
    """A batch/v1 finite workload."""

    resource_type: ClassVar[ResourceType] = ResourceType("Job", "batch/v1", "jobs")
    spec: JobSpec


@frozen(kw_only=True)
class Deployment(Resource):
    """An apps/v1 persistent workload."""

    resource_type: ClassVar[ResourceType] = ResourceType("Deployment", "apps/v1", "deployments")
    spec: DeploymentSpec


@frozen(kw_only=True)
class SpecResource(Resource):
    """Preserve open native or CRD spec fields without duplicating their schemas."""

    spec: dict[str, Any]


@frozen(kw_only=True)
class LeaseSpec(AST):
    """Describe a renewable coordination claim and its holder identity."""

    holderIdentity: str | None = None
    leaseDurationSeconds: int | None = None
    renewTime: str | None = None


@frozen(kw_only=True)
class Lease(Resource):
    """A coordination.k8s.io/v1 leader, membership or shard lease."""

    resource_type: ClassVar[ResourceType] = ResourceType("Lease", "coordination.k8s.io/v1", "leases")
    spec: LeaseSpec


@frozen(kw_only=True)
class Service(SpecResource):
    """A core/v1 Service exposing graph workloads."""

    resource_type: ClassVar[ResourceType] = ResourceType("Service", "v1", "services")


@frozen(kw_only=True)
class PersistentVolumeClaim(SpecResource):
    """A core/v1 request for graph-owned persistent storage."""

    resource_type: ClassVar[ResourceType] = ResourceType("PersistentVolumeClaim", "v1", "persistentvolumeclaims")


@frozen(kw_only=True)
class ConfigMap(Resource):
    """A core/v1 ConfigMap with data fields at the document root, without a spec."""

    resource_type: ClassVar[ResourceType] = ResourceType("ConfigMap", "v1", "configmaps")
    data: dict[str, str] | None = None
    binaryData: dict[str, str] | None = None
    immutable: bool | None = None


@frozen(kw_only=True)
class Graph(SpecResource):
    """A Polyad graph boundary; its topology is validated by the graph library."""

    resource_type: ClassVar[ResourceType] = ResourceType("Graph", f"{GROUP}/{VERSION}", "graphs", boundary=True)


@frozen(kw_only=True)
class EphemeralGraph(SpecResource):
    """A graph boundary targeting interruptible capacity."""

    resource_type: ClassVar[ResourceType] = ResourceType("EphemeralGraph", f"{GROUP}/{VERSION}", "ephemeralgraphs", boundary=True)


@frozen(kw_only=True)
class Feedback(SpecResource):
    """A boundary that executes durable graph epochs."""

    resource_type: ClassVar[ResourceType] = ResourceType("Feedback", f"{GROUP}/{VERSION}", "feedbacks", boundary=True)


@frozen(kw_only=True)
class Workload(SpecResource):
    """A reusable finite workload definition."""

    resource_type: ClassVar[ResourceType] = ResourceType("Workload", f"{GROUP}/{VERSION}", "workloads")


@frozen(kw_only=True)
class Daemon(SpecResource):
    """A reusable persistent workload definition."""

    resource_type: ClassVar[ResourceType] = ResourceType("Daemon", f"{GROUP}/{VERSION}", "daemons")


@frozen(kw_only=True)
class Ephemeral(SpecResource):
    """A reusable finite workload definition for interruptible capacity."""

    resource_type: ClassVar[ResourceType] = ResourceType("Ephemeral", f"{GROUP}/{VERSION}", "ephemerals")


@frozen(kw_only=True)
class ResourceDefinition(SpecResource):
    """A Polyad Resource definition, distinct from the Kubernetes AST base class."""

    resource_type: ClassVar[ResourceType] = ResourceType("Resource", f"{GROUP}/{VERSION}", "resources")


@frozen(kw_only=True)
class Gate(SpecResource):
    """A reusable Boolean admission expression."""

    resource_type: ClassVar[ResourceType] = ResourceType("Gate", f"{GROUP}/{VERSION}", "gates")


@frozen(kw_only=True)
class ShutdownPolicy(SpecResource):
    """A reusable runtime limit and termination contract."""

    resource_type: ClassVar[ResourceType] = ResourceType("ShutdownPolicy", f"{GROUP}/{VERSION}", "shutdownpolicies")


@frozen(kw_only=True)
class Rewrite(SpecResource):
    """A generation-fenced graph revision request."""

    resource_type: ClassVar[ResourceType] = ResourceType("Rewrite", f"{GROUP}/{VERSION}", "rewrites")


RESOURCE_CLASSES: tuple[type[Resource], ...] = (
    Job,
    Deployment,
    Lease,
    Service,
    ConfigMap,
    PersistentVolumeClaim,
    Graph,
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
