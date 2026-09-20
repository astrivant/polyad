"""
Read the event tuning shared by local publishers, root-held remote streams and HTTP listeners.
"""

from __future__ import annotations

import os

from polyad_types.events.envelope import EventStreamSettings

__all__ = ("settings_from_environment",)


def settings_from_environment() -> EventStreamSettings:
    """
    Resolve chart-provided budgets before any storage client or listener starts.

    Returns:
        EventStreamSettings: Validated byte, batch and polling limits.
    """
    return EventStreamSettings(
        maxEventBytes=int(os.environ.get("POLYAD_EVENTS_MAX_EVENT_BYTES", "1048576")),
        readBatchSize=int(os.environ.get("POLYAD_EVENTS_READ_BATCH_SIZE", "64")),
        pollIntervalSeconds=float(os.environ.get("POLYAD_EVENTS_POLL_INTERVAL_SECONDS", "1")),
    )
