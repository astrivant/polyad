"""
Describe optional Kubernetes autoscaling compatibility resources.
"""

from __future__ import annotations

from typing import ClassVar

from attrs import frozen

from polyad_types.resources.base import SpecResource
from polyad_types.resources.common import ResourceType

__all__ = ("VerticalPodAutoscaler",)


@frozen(kw_only=True)
class VerticalPodAutoscaler(SpecResource):
    """
    A graph-owned VPA policy whose controller is installed separately.

    Attributes:
        resource_type (ClassVar[ResourceType]): Optional VPA API identity and graph ownership descriptor.
    """

    resource_type: ClassVar[ResourceType] = ResourceType(
        "VerticalPodAutoscaler",
        "autoscaling.k8s.io/v1",
        "verticalpodautoscalers",
        description="Optional VPA resource-policy and in-place resize compatibility.",
        graph_owned=True,
        auxiliary="scaling",
        required_feature="vpa",
    )
