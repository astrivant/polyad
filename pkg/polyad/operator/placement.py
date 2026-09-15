"""Intersect graph placement constraints across boundary and pod scopes."""

import copy
from typing import Any

REQUIRED = "requiredDuringSchedulingIgnoredDuringExecution"
PREFERRED = "preferredDuringSchedulingIgnoredDuringExecution"


def merge_placement(*scopes: dict[str, Any] | None) -> dict[str, Any]:
    """Narrow node eligibility, retain preferences and union tolerations without mutating inputs."""
    result: dict[str, Any] = {}
    for scope in scopes:
        if not scope:
            continue
        if set(scope) - {"nodeSelector", "nodeAffinity", "tolerations"}:
            raise ValueError("unsupported placement field")
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
    """Apply inherited placement while preserving pod affinity and other scheduling fields."""
    if not placement:
        return
    if pod.get("nodeName"):
        raise ValueError("nodeName bypasses scheduling and cannot be used inside a placed graph")
    own = {
        "nodeSelector": pod.get("nodeSelector", {}),
        "tolerations": pod.get("tolerations", []),
        "nodeAffinity": pod.get("affinity", {}).get("nodeAffinity", {}),
    }
    merged = merge_placement(placement, own)
    for key in ("nodeSelector", "tolerations"):
        if key in merged:
            pod[key] = merged[key]
    if "nodeAffinity" in merged:
        pod.setdefault("affinity", {})["nodeAffinity"] = merged["nodeAffinity"]
