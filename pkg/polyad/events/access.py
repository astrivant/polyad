"""
Enforce operator-tree service access ceilings independently of credential graph grants.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from polyad.api.http.errors import Forbidden
from polyad_types.api.discovery import AccessMode, AtlasAccess
from polyad_types.serialization import converter

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API

__all__ = (
    "configuration",
    "parent_allows_worker",
    "require_scope",
    "scope_allows",
)


def configuration() -> AtlasAccess:
    """
    Load the administrator's operator-tree ceilings without accepting HTTP overrides.

    Returns:
        AtlasAccess: Validated inherited service access configuration.
    """
    return converter.structure(json.loads(os.environ.get("POLYAD_SERVICE_ACCESS", "{}")), AtlasAccess)


def scope_allows(mode: AccessMode, home: list[dict[str, Any]], target: list[dict[str, Any]]) -> bool:
    """
    Compare verified home and target ancestry against an effective access mode.

    Args:
        mode (AccessMode): Most restrictive operator ceiling for this capability.
        home (list[dict[str, Any]]): Home identity followed by freshly verified ancestors.
        target (list[dict[str, Any]]): Destination identity followed by freshly verified ancestors.

    Returns:
        bool: Whether the requested relationship fits the selected mode.
    """
    if mode == AccessMode.DISABLED or not home or not target:
        return False
    if mode == AccessMode.ATLAS:
        return True
    if home[0].get("cluster", "") != target[0].get("cluster", ""):
        return False
    if mode == AccessMode.CLUSTER:
        return True
    fields = ("cluster", "namespace", "kind", "name", "uid")

    def identity(item: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(item.get(field, "") for field in fields)

    if mode == AccessMode.SAME_GRAPH:
        return identity(home[0]) == identity(target[0])
    local = home[0].get("cluster", "")
    roots = {identity(item) for item in home if item.get("cluster", "") == local}
    return any(identity(item) in roots for item in target if item.get("cluster", "") == local)


def require_scope(capability: str, home: list[dict[str, Any]], target: list[dict[str, Any]], *, policy: AtlasAccess | None = None) -> None:
    """
    Reject unsupported scopes locally instead of escalating them to another operator.

    Args:
        capability (str): Discovery or connections.
        home (list[dict[str, Any]]): Verified service home path.
        target (list[dict[str, Any]]): Verified target path.
        policy (AtlasAccess | None): Explicit policy for tests or an inherited configuration.

    Returns:
        None: A mismatch raises Forbidden with the effective limiting mode.
    """
    policy = policy or configuration()
    mode = policy.effective(home[0].get("cluster", ""), capability) if home else AccessMode.DISABLED
    serving = policy.effective(os.environ.get("POLYAD_CLUSTER_NAME", ""), capability)
    mode = min((mode, serving), key=list(AccessMode).index)
    if not scope_allows(mode, home, target):
        from polyad.operator.observability.decisions import decision

        decision(
            "polyad.service_access.denied",
            f"Rejected {capability}: requested scope exceeds the effective {mode.value} mode.",
            outcome="blocked",
            reason="operator_mode_mismatch",
            attributes={"polyad.access.mode": mode.value, "polyad.access.capability": capability},
        )
        raise Forbidden(f"{capability} request exceeds this operator's effective {mode.value} mode")


async def parent_allows_worker(api: API, namespace: str) -> bool:
    """
    Fence remote execution when its local policy would exceed the live root deployment policy.

    Args:
        api (API): Root-cluster reader using the worker's configured root credentials.
        namespace (str): Root deployment namespace.

    Returns:
        bool: True for non-workers or a child policy no broader than the root; unavailable roots deny execution.
    """
    if os.environ.get("POLYAD_ROOT_WORKER", "false").lower() != "true":
        return True
    name = os.environ.get("POLYAD_ROOT_DEPLOYMENT", "")
    root = await api.get("Deployment", namespace, name) if name else None
    if root is None or root["metadata"].get("deletionTimestamp"):
        return False
    operator = next((item for item in root["spec"]["template"]["spec"]["containers"] if item["name"] == "operator"), None)
    if operator is None:
        return False
    raw = next((item.get("value", "") for item in operator.get("env", []) if item["name"] == "POLYAD_SERVICE_ACCESS"), "{}")
    parent = converter.structure(json.loads(raw), AtlasAccess)
    child = configuration()
    order = list(AccessMode)

    # Compare every configured cluster in both policies, including the local/root default.
    names = {"", *child.clusters, *parent.clusters}
    allowed = all(
        order.index(child.effective(name, capability)) <= order.index(parent.effective(name, capability))
        for name in names
        for capability in ("discovery", "connections")
    )
    allowed = allowed and all(parent.trustDomains.get(name) == domain for name, domain in child.trustDomains.items())
    if not allowed:
        from polyad.operator.observability.decisions import decision

        decision(
            "polyad.service_access.worker_fenced",
            "Worker policy exceeds its root's current access ceiling; mutations are paused.",
            outcome="blocked",
            reason="worker_access_policy_conflict",
        )
    return allowed
