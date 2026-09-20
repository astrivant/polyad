"""
Resolve locally authorized remote intent without changing local replica declarations.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from polyad_types.resources import GROUP

if TYPE_CHECKING:
    from typing import Any

__all__ = (
    "INTENT",
    "approved_intent",
    "remote_revision",
)


INTENT = f"{GROUP}/remote-scale-intent"


def approved_intent(obj: dict[str, Any]) -> dict[str, Any] | None:
    """
    Discard intents whose request identity, local generation or bounds no longer match.

    Args:
        obj (dict[str, Any]): Live destination ReplicaGroup.

    Returns:
        dict[str, Any] | None: Authorized intent, or local control when absent or invalid.
    """
    spec, meta = obj["spec"], obj["metadata"]
    grant = spec.get("remoteScaling")
    if not grant or (spec.get("replicaSource") and spec.get("inheritReplicas", True)):
        return None
    try:
        intent = json.loads(meta.get("annotations", {}).get(INTENT, "null"))
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(intent, dict)
        or intent.get("owner") != grant
        or intent.get("targetUid") != meta["uid"]
        or intent.get("targetGeneration") != meta.get("generation", 1)
        or type(intent.get("replicas")) is not int
        or not spec.get("minReplicas", 0) <= intent["replicas"] <= spec.get("maxReplicas", 32)
    ):
        return None
    return intent


def remote_revision(obj: dict[str, Any]) -> str:
    """
    Fingerprint the accepted request so annotation updates invalidate scale observations.

    Args:
        obj (dict[str, Any]): Destination ReplicaGroup.

    Returns:
        str: Exact intent digest, or an empty string for local control.
    """
    intent = approved_intent(obj)
    return hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest() if intent else ""
