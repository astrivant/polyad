"""
Define shared Kubernetes resource envelopes.
"""

from __future__ import annotations

from typing import Any, ClassVar

from attrs import frozen

from polyad_types.resources.common import AST, ObjectMeta, ResourceType

__all__ = (
    "Resource",
    "SpecResource",
)


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
class SpecResource(Resource):
    """
    Preserve open native or CRD spec fields without duplicating their schemas.

    Attributes:
        spec (dict[str, Any]): Desired resource configuration.
    """

    spec: dict[str, Any]
