"""
Shared Kubernetes identity, metadata and mutation syntax trees.
"""

from __future__ import annotations

from typing import Any, Literal

from attrs import field, frozen

GROUP = "polyad.astrivant.com"
VERSION = "v1alpha1"


@frozen(kw_only=True)
class AST:
    """
    Retain native fields outside the explicitly modeled subset.

    Attributes:
        extra (dict[str, Any]): Unmodeled native fields preserved during serialization.
    """

    extra: dict[str, Any] = field(factory=dict)


@frozen
class ResourceType:
    """
    Bind a Kubernetes kind to its API version, plural and namespaced endpoint.

    Attributes:
        kind (str): Kubernetes resource kind.
        api_version (str): Kubernetes API group/version, or the core version.
        plural (str): Plural resource name used in Kubernetes API paths.
        boundary (bool): Whether this kind owns a graph reconciliation boundary.
        description (str): Purpose of this supported resource type.
        namespaced (bool): Whether Kubernetes scopes instances to a namespace.
        graph_owned (bool): Whether graph inventory and resource counts include this kind.
        definition (bool): Whether this kind is a reusable definition without independent execution.
        reconciled (bool): Whether the operator schedules reconciliation duties for this kind.
        composable (bool): Whether composition requests may declare this kind.
        auxiliary (Literal['network', 'capacity', 'activation', 'connection', 'traffic'] | None): Role excluded from graph vertices.
        required_feature (Literal['mesh', 'capacity'] | None): Operator feature required for graph inventory reads.
    """

    kind: str
    api_version: str
    plural: str
    boundary: bool = False
    description: str = ""
    namespaced: bool = True
    graph_owned: bool = False
    definition: bool = False
    reconciled: bool = False
    composable: bool = False
    auxiliary: Literal["network", "capacity", "activation", "connection", "traffic"] | None = None
    required_feature: Literal["mesh", "capacity"] | None = None

    @property
    def api_group(self) -> str:
        """
        Return the API group, or the empty string for core Kubernetes resources.

        Returns:
            str: API group without its version.
        """
        return self.api_version.rsplit("/", 1)[0] if "/" in self.api_version else ""

    @property
    def polyad(self) -> bool:
        """
        Identify resources whose CRD API is owned by Polyad.

        Returns:
            bool: Whether this descriptor belongs to the Polyad API group.
        """
        return self.api_group == GROUP

    @property
    def prefix(self) -> str:
        """
        Return the versioned API prefix used for this resource kind.

        Returns:
            str: Versioned Kubernetes API path prefix.
        """
        return f"/apis/{self.api_version}" if "/" in self.api_version else f"/api/{self.api_version}"


@frozen(kw_only=True)
class OwnerReference(AST):
    """
    Identify an owning resource without inferring controller or deletion flags.

    Attributes:
        apiVersion (str): API group/version of the owning resource.
        kind (str): Kubernetes resource kind.
        name (str): Resource name within its namespace.
        uid (str): Persisted Kubernetes identity used to fence ownership.
        controller (bool | None): Whether this owner controls the dependent resource.
        blockOwnerDeletion (bool | None): Whether this dependent blocks foreground deletion of its owner.
    """

    apiVersion: str
    kind: str
    name: str
    uid: str
    controller: bool | None = None
    blockOwnerDeletion: bool | None = None


@frozen(kw_only=True)
class ObjectMeta(AST):
    """
    Represent object or pod-template metadata, including API concurrency fences.

    Attributes:
        name (str | None): Resource name within its namespace.
        namespace (str | None): Namespace containing the operator resources.
        uid (str | None): Persisted Kubernetes identity used to fence ownership.
        resourceVersion (str | None): Opaque API version used for optimistic concurrency.
        generation (int | None): API-assigned revision of the desired configuration.
        annotations (dict[str, str] | None): Resource annotations carrying coordination or revision metadata.
        labels (dict[str, str] | None): Kubernetes labels used for selection and ownership lookup.
        ownerReferences (tuple[OwnerReference, ...] | None): Owners tracked by Kubernetes garbage collection.
        finalizers (tuple[str, ...] | None): Cleanup gates that must be released before deletion.
        deletionTimestamp (str | None): API timestamp indicating that deletion has begun.
        creationTimestamp (str | None): API timestamp recording resource creation.
    """

    name: str | None = None
    namespace: str | None = None
    uid: str | None = None
    resourceVersion: str | None = None
    generation: int | None = None
    annotations: dict[str, str] | None = None
    labels: dict[str, str] | None = None
    ownerReferences: tuple[OwnerReference, ...] | None = None
    finalizers: tuple[str, ...] | None = None
    deletionTimestamp: str | None = None
    creationTimestamp: str | None = None


@frozen(kw_only=True)
class StatusPatch(AST):
    """
    Patch observations using metadata.resourceVersion for optimistic concurrency.

    Attributes:
        metadata (ObjectMeta): Object identity, ownership and concurrency metadata.
        status (dict[str, Any]): Observed resource state.
    """

    metadata: ObjectMeta
    status: dict[str, Any]


@frozen(kw_only=True)
class UIDPreconditions(AST):
    """
    Fence deletion against a same-name resource with a different identity.

    Attributes:
        uid (str): Persisted Kubernetes identity used to fence ownership.
        resourceVersion (str | None): Opaque API version used for optimistic concurrency.
    """

    uid: str
    resourceVersion: str | None = None


@frozen(kw_only=True)
class DeleteOptions(AST):
    """
    Request foreground deletion without treating acknowledgement as disappearance.

    Attributes:
        preconditions (UIDPreconditions): Identity and version that must still match before deletion.
        propagationPolicy (Literal['Foreground']): Foreground deletion mode, which waits for dependent cleanup.
    """

    preconditions: UIDPreconditions
    propagationPolicy: Literal["Foreground"] = "Foreground"
