"""
Describe native Kubernetes workloads, networking and management resources.
"""

from __future__ import annotations

from typing import Any, ClassVar

from attrs import field, frozen

from polyad_types.resources.base import Resource, SpecResource
from polyad_types.resources.common import AST, ObjectMeta, ResourceType


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
class ReplicaSet(SpecResource):
    """
    Observe a Deployment's Pod ownership chain without managing its ReplicaSets.

    Attributes:
        resource_type (ClassVar[ResourceType]): Read-only native ownership identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType("ReplicaSet", "apps/v1", "replicasets", description="Observed Pod ownership.")


@frozen(kw_only=True)
class ServiceAccount(SpecResource):
    """
    Observe the current incarnation of a connection participant's identity.

    Attributes:
        resource_type (ClassVar[ResourceType]): Native service-account identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "ServiceAccount", "v1", "serviceaccounts", description="Observed workload identity."
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
