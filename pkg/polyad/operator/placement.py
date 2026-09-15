"""Intersect graph placement constraints across boundary and pod scopes."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

REQUIRED = "requiredDuringSchedulingIgnoredDuringExecution"
PREFERRED = "preferredDuringSchedulingIgnoredDuringExecution"


def merge_placement(*scopes: dict[str, Any] | None) -> dict[str, Any]:
    """
    Narrow node eligibility, retain preferences and union tolerations without mutating inputs.

    Args:
        *scopes (dict[str, Any] | None): Placement scopes to intersect in inheritance order.

    Returns:
        dict[str, Any]: Combined placement constraints without modifying the input scopes.
    """
    result: dict[str, Any] = {}
    for scope in scopes:
        if not scope:
            continue
        if set(scope) - {"nodeSelector", "nodeAffinity", "tolerations", "enforce"}:
            raise ValueError("unsupported placement field")
        if "enforce" in scope and not isinstance(scope["enforce"], bool):
            raise ValueError("placement.enforce must be Boolean")
        if result and not result.get("enforce", True) and any(scope.get(key) for key in ("nodeSelector", "nodeAffinity", "tolerations")):
            result = {}  # A more specific scope replaces non-enforced defaults in full.
        enforced = bool(result) and result.get("enforce", True)
        if "enforce" in scope and not enforced:
            result["enforce"] = scope["enforce"]
        for key, value in scope.get("nodeSelector", {}).items():
            selector = result.setdefault("nodeSelector", {})
            if key in selector and selector[key] != value:
                raise ValueError(f"conflicting placement label: {key}")
            selector[key] = value
        if scope.get("nodeAffinity"):
            affinity = result.setdefault("nodeAffinity", {})
            incoming = scope["nodeAffinity"]
            if set(incoming) - {REQUIRED, PREFERRED}:
                raise ValueError("unsupported node affinity field")
            if REQUIRED in incoming:
                terms = copy.deepcopy(incoming[REQUIRED]["nodeSelectorTerms"])
                # Empty terms select no nodes; they must never become a wildcard during intersection.
                terms = [term for term in terms if term.get("matchExpressions") or term.get("matchFields")]
                if REQUIRED in affinity:
                    old = affinity[REQUIRED]["nodeSelectorTerms"]
                    if len(old) * len(terms) > 256:
                        raise ValueError("placement intersection exceeds 256 selector terms")
                    terms = [
                        {key: left.get(key, []) + right.get(key, []) for key in ("matchExpressions", "matchFields")}
                        for left in old
                        for right in terms
                    ]
                affinity[REQUIRED] = {"nodeSelectorTerms": terms}
            if PREFERRED in incoming:
                affinity.setdefault(PREFERRED, []).extend(copy.deepcopy(incoming[PREFERRED]))
        for toleration in scope.get("tolerations", []):
            items = result.setdefault("tolerations", [])
            if toleration not in items:
                items.append(copy.deepcopy(toleration))
    return result


def place_pod(pod: dict[str, Any], placement: dict[str, Any]) -> None:
    """
    Apply inherited placement while preserving pod affinity and other scheduling fields.

    Args:
        pod (dict[str, Any]): Pod spec to update in place with effective scheduling constraints.
        placement (dict[str, Any]): Scheduling constraints inherited by descendant execution.

    Returns:
        None: No return value.
    """
    if not placement:
        return
    if pod.get("nodeName"):
        raise ValueError("nodeName bypasses scheduling and cannot be used inside a placed graph")
    own = {
        "nodeSelector": pod.get("nodeSelector", {}),
        "tolerations": pod.get("tolerations", []),
        "nodeAffinity": pod.get("affinity", {}).get("nodeAffinity", {}),
    }
    own = {key: value for key, value in own.items() if value}
    merged = merge_placement(placement, own)
    for key in ("nodeSelector", "tolerations"):
        if key in merged:
            pod[key] = merged[key]
    if "nodeAffinity" in merged:
        pod.setdefault("affinity", {})["nodeAffinity"] = merged["nodeAffinity"]
