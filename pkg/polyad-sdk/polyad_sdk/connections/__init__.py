"""
Open explicitly configured workload transports without operator credentials.
"""

from __future__ import annotations

from polyad_sdk.connections.client import WorkloadClient as WorkloadClient
from polyad_sdk.connections.endpoint import WorkloadEndpoint as WorkloadEndpoint

__all__ = (
    "WorkloadClient",
    "WorkloadEndpoint",
)
