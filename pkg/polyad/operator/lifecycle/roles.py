"""
Select dense or independently deployable operator responsibilities.
"""

from __future__ import annotations

import os

__all__ = (
    "ROLES",
    "executes",
    "role",
    "serves",
)


ROLES = frozenset({"dense", "bootstrap", "executor", "gateway", "telemetry"})


def role() -> str:
    """
    Validate the process role before any controller or listener starts.

    Returns:
        str: Dense by default, or one explicitly selected service component.
    """
    selected = os.environ.get("POLYAD_COMPONENT", "dense")
    if selected not in ROLES:
        raise ValueError("POLYAD_COMPONENT must be dense, bootstrap, executor, gateway or telemetry")
    return selected


def executes() -> bool:
    """
    Identify processes eligible to hold graph mutation leases.

    Returns:
        bool: Whether this process executes reconciliation duties.
    """
    return role() in {"dense", "bootstrap", "executor"}


def serves(feature: str) -> bool:
    """
    Assign HTTP endpoints to the matching component in a split deployment.

    Args:
        feature (str): METRICS, API, EVENTS or CONNECTIONS endpoint family.

    Returns:
        bool: Whether the endpoint is enabled for this process and configuration.
    """
    component = "telemetry" if feature == "METRICS" else "gateway"
    return role() in {"dense", component} and os.environ.get(f"POLYAD_{feature}_ENABLED", "false").lower() == "true"
