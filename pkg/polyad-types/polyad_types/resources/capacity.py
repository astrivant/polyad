"""
Represent advance capacity observations and autoscaler Pod sets as attrs trees.
"""

from __future__ import annotations

from typing import Literal

from attrs import field, frozen

from polyad_types.resources.common import AST


@frozen(kw_only=True)
class PodTemplateReference(AST):
    """
    Reference a native PodTemplate in the same namespace.

    Attributes:
        name (str): Persisted PodTemplate name.
    """

    name: str


@frozen(kw_only=True)
class PodSet(AST):
    """
    Request a group of identically configured Pods.

    Attributes:
        podTemplateRef (PodTemplateReference): Scheduling template for this group.
        count (int): Number of Pods requiring capacity.
    """

    podTemplateRef: PodTemplateReference
    count: int


@frozen(kw_only=True)
class ProvisioningRequestSpec(AST):
    """
    Describe immutable autoscaling.x-k8s.io/v1 provisioning intent.

    Attributes:
        provisioningClassName (str): Supported autoscaler provisioning class.
        podSets (tuple[PodSet, ...]): Groups of future workload Pods.
        parameters (dict[str, str]): Provider-specific options.
    """

    provisioningClassName: str
    podSets: tuple[PodSet, ...]
    parameters: dict[str, str] = field(factory=dict)


@frozen(kw_only=True)
class CapacityNodeStatus(AST):
    """
    Retain generation-fenced provisioning and handoff state for one execution node.

    Attributes:
        revision (str): Hash of the graph generation, workload and capacity policy.
        backend (str): Backend selected for this revision.
        phase (Literal['Planned', 'Provisioning', 'Ready', 'Releasing', 'Consumed', 'Failed', 'Expired', 'Cancelled']):
            Current capacity lifecycle.
        startedAt (str): UTC start of this bounded request.
        pods (int): Requested Pod count.
        waitSeconds (int): Elapsed time since the capacity forecast was recorded.
        readyPods (int): Pods with freshly observed usable capacity.
        resources (dict[str, str]): Total requested resources for this node's future Pods.
        message (str): Provisioning or expiry explanation.
        requestName (str): ProvisioningRequest name, or empty for placeholders.
    """

    revision: str
    backend: str
    phase: Literal["Planned", "Provisioning", "Ready", "Releasing", "Consumed", "Failed", "Expired", "Cancelled"] = field(
        default="Planned", metadata={"schema": {"description": "Current capacity lifecycle."}}
    )
    startedAt: str
    pods: int
    waitSeconds: int = 0
    readyPods: int = 0
    resources: dict[str, str] = field(factory=dict)
    message: str = ""
    requestName: str = ""


@frozen(kw_only=True)
class CapacityStatus(AST):
    """
    Expose durable advance capacity plans independently of execution readiness.

    Attributes:
        observedGeneration (int): Graph generation used for this forecast.
        nodes (dict[str, CapacityNodeStatus]): Per-execution-node capacity lifecycle.
    """

    observedGeneration: int = 0
    nodes: dict[str, CapacityNodeStatus] = field(factory=dict)
