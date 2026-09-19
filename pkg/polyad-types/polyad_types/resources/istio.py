"""
Describe Istio routing, authentication and authorization resources.
"""

from __future__ import annotations

from typing import ClassVar

from attrs import frozen

from polyad_types.resources.base import SpecResource
from polyad_types.resources.common import ResourceType


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
class VirtualService(SpecResource):
    """
    Route HTTP requests among graph-owned destination subsets.

    Attributes:
        resource_type (ClassVar[ResourceType]): Optional Istio traffic routing API identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "VirtualService",
        "networking.istio.io/v1",
        "virtualservices",
        description="Optional percentage routing between connected graph nodes.",
        graph_owned=True,
        auxiliary="traffic",
        required_feature="mesh",
    )


@frozen(kw_only=True)
class DestinationRule(SpecResource):
    """
    Select downstream graph subtrees within a shared Service.

    Attributes:
        resource_type (ClassVar[ResourceType]): Optional Istio destination subset API identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "DestinationRule",
        "networking.istio.io/v1",
        "destinationrules",
        description="Optional downstream subsets scoped to the caller subtree.",
        graph_owned=True,
        auxiliary="traffic",
        required_feature="mesh",
    )
