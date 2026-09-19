"""
Describe infrastructure services and advance capacity resources.
"""

from __future__ import annotations

from typing import ClassVar

from attrs import frozen

from polyad_types.resources.base import Resource, SpecResource
from polyad_types.resources.common import ResourceType
from polyad_types.resources.kubernetes import PodTemplate


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
class PostgreSQLCluster(SpecResource):
    """
    A CloudNativePG cluster observed without taking over its lifecycle.

    Attributes:
        resource_type (ClassVar[ResourceType]): Namespaced PostgreSQL observation API identity.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "Cluster", "postgresql.cnpg.io/v1", "clusters", description="Read-only observation of a CloudNativePG database cluster."
    )
