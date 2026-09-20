"""
Expose sharing contracts and opt-in live container resource disclosure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_sdk.runtime.resources import container_metrics
from polyad_types.api.capabilities import CapabilityAdvertisement as CapabilityAdvertisement
from polyad_types.api.capabilities import CapabilityContract as CapabilityContract
from polyad_types.api.capabilities import CapabilityOffer as CapabilityOffer
from polyad_types.api.capabilities import ResourceAvailability as ResourceAvailability

if TYPE_CHECKING:
    from polyad_sdk.runtime.context import WorkloadContext
    from polyad_sdk.runtime.resources import ContainerMetrics

__all__ = (
    "CapabilityAdvertisement",
    "CapabilityContract",
    "CapabilityOffer",
    "ResourceAvailability",
    "resource_availability",
)


def resource_availability(
    context: WorkloadContext, *, metrics: ContainerMetrics | None = None, container: str = ""
) -> ResourceAvailability:
    """
    Sample live cgroup limits alongside clearly separate startup requests and VPA bounds.

    Args:
        context (WorkloadContext): Projected application context; requests and policy bounds can be stale after startup.
        metrics (ContainerMetrics | None): Explicit sample for testing or custom cgroup paths; None reads the current cgroup.
        container (str): Optional name identifying the single sampled application container.

    Returns:
        ResourceAvailability: Best-effort observations; unknown readings stay None, with no inferred job throughput.
    """
    observed = container_metrics() if metrics is None else metrics

    # Do not substitute startup limits for missing live observations. An in-place
    # resize can invalidate the environment while the process remains running.
    return ResourceAvailability(
        container=container,
        cpuLimitMillicores=observed.cpu_limit_millicores,
        cpuUsageUsec=observed.cpu_usage_usec,
        memoryLimitBytes=observed.memory_limit_bytes,
        memoryUsageBytes=observed.memory_usage_bytes,
        cpuRequestMillicores=context.resources.cpu_request_millicores,
        memoryRequestBytes=context.resources.memory_request_bytes,
        vpaMinCpuMillicores=context.vpa.min_cpu_millicores,
        vpaMaxCpuMillicores=context.vpa.max_cpu_millicores,
        vpaMinMemoryBytes=context.vpa.min_memory_bytes,
        vpaMaxMemoryBytes=context.vpa.max_memory_bytes,
    )
