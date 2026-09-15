"""Shared Kubernetes identity, metadata and mutation syntax trees."""

from typing import Any, Literal

from attrs import field, frozen

GROUP = "polyad.astrivant.com"
VERSION = "v1alpha1"


@frozen(kw_only=True)
class AST:
    """Retain native fields outside the explicitly modeled subset."""

    extra: dict[str, Any] = field(factory=dict)


@frozen
class ResourceType:
    """Bind a Kubernetes kind to its API version, plural and namespaced endpoint."""

    kind: str
    api_version: str
    plural: str
    boundary: bool = False

    @property
    def prefix(self) -> str:
        """Return the versioned API prefix used for this resource kind."""
        return f"/apis/{self.api_version}" if "/" in self.api_version else f"/api/{self.api_version}"


@frozen(kw_only=True)
class OwnerReference(AST):
    """Identify an owning resource without inferring controller or deletion flags."""

    apiVersion: str
    kind: str
    name: str
    uid: str
    controller: bool | None = None
    blockOwnerDeletion: bool | None = None


@frozen(kw_only=True)
class ObjectMeta(AST):
    """Represent object or pod-template metadata, including API concurrency fences."""

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
    """Patch observations using metadata.resourceVersion for optimistic concurrency."""

    metadata: ObjectMeta
    status: dict[str, Any]


@frozen(kw_only=True)
class UIDPreconditions(AST):
    """Fence deletion against a same-name resource with a different identity."""

    uid: str
    resourceVersion: str | None = None


@frozen(kw_only=True)
class DeleteOptions(AST):
    """Request foreground deletion without treating acknowledgement as disappearance."""

    preconditions: UIDPreconditions
    propagationPolicy: Literal["Foreground"] = "Foreground"
