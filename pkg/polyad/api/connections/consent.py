"""
Bind temporary-connection consent to verified Pods and current graph ownership.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from polyad.api.errors import Forbidden
from polyad_types.resources import BOUNDARY_KINDS, GROUP, RESOURCE_TYPES

if TYPE_CHECKING:
    from typing import Any

    from polyad.api.connections.store import Caller, ConnectionSettings
    from polyad.operator.adapters.kubernetes import API

CONSENTS = f"{GROUP}/connection-consents"
TERMINAL = {"Expired", "Revoked", "Rejected"}


def decisions(receipt: dict[str, Any]) -> dict[str, Any]:
    """
    Decode bounded endpoint decisions stored with the immutable proposal.

    Args:
        receipt (dict[str, Any]): Current TemporaryConnection.

    Returns:
        dict[str, Any]: At most one decision per endpoint, fenced to this receipt UID.
    """
    raw = receipt["metadata"].get("annotations", {}).get(CONSENTS, "{}")
    if len(raw) > 16384:
        raise ValueError("connection consent metadata exceeds its bound")
    values = json.loads(raw)
    endpoints = {receipt["spec"]["source"], receipt["spec"]["target"]}
    if not isinstance(values, dict) or values.keys() - endpoints:
        raise ValueError("connection consent metadata must identify only its endpoints")
    for value in values.values():
        if (
            not isinstance(value, dict)
            or value.get("receiptUid") != receipt["metadata"]["uid"]
            or value.get("decision") not in {"Approve", "Reject"}
            or value.get("verb") not in {"connect", "approve"}
        ):
            raise ValueError("connection consent does not match its receipt")
    return values


async def endpoint(api: API, caller: Caller, receipt: dict[str, Any]) -> str:
    """
    Resolve a Pod-bound caller to one endpoint through live UID-fenced owners.

    Args:
        api (API): Fresh Kubernetes reader in the receipt's cluster.
        caller (Caller): TokenReview-verified service-account and Pod identity.
        receipt (dict[str, Any]): Immutable graph and endpoint proposal.

    Returns:
        str: Logical endpoint represented by this Pod; unrelated callers are forbidden.
    """
    names = caller.extra.get("authentication.kubernetes.io/pod-name", [])
    uids = caller.extra.get("authentication.kubernetes.io/pod-uid", [])
    if not isinstance(names, (list, tuple)) or not isinstance(uids, (list, tuple)) or len(names) != 1 or len(uids) != 1:
        raise Forbidden("connection consent requires a Pod-bound projected token")
    pod = await api.get("Pod", caller.namespace, names[0])
    account = caller.username.rsplit(":", 1)[-1]
    identity = await api.get("ServiceAccount", caller.namespace, account)
    if (
        pod is None
        or pod["metadata"]["uid"] != uids[0]
        or pod["metadata"].get("deletionTimestamp")
        or pod["spec"].get("serviceAccountName", "default") != account
        or identity is None
        or identity["metadata"]["uid"] != caller.uid
        or identity["metadata"].get("deletionTimestamp")
    ):
        raise Forbidden("connection participant Pod or service account is absent or replaced")
    spec = receipt["spec"]
    current, seen = pod, set()
    for _ in range(64):
        meta = current["metadata"]
        if meta["uid"] in seen:
            break
        seen.add(meta["uid"])
        owners = [owner for owner in meta.get("ownerReferences", []) if owner.get("controller")]
        if len(owners) != 1:
            break
        owner = owners[0]
        kind = owner["kind"]
        if kind not in {"ReplicaSet", "Deployment", "StatefulSet", "DaemonSet", "Job", *BOUNDARY_KINDS}:
            break
        if owner.get("apiVersion") != RESOURCE_TYPES[kind].api_version:
            break
        parent = await api.get(kind, meta["namespace"], owner["name"])
        if parent is None or parent["metadata"]["uid"] != owner["uid"] or parent["metadata"].get("deletionTimestamp"):
            break
        if kind == spec["kind"] and parent["metadata"]["name"] == spec["graph"] and parent["metadata"]["namespace"] == spec["namespace"]:
            node = meta.get("labels", {}).get(f"{GROUP}/node")
            if parent["metadata"]["uid"] == spec["graphUid"] and node in {spec["source"], spec["target"]}:
                return str(node)
            break
        current = parent
    raise Forbidden("caller Pod does not belong to either proposed graph endpoint")


async def confirmed(api: API, receipt: dict[str, Any], settings: ConnectionSettings) -> set[str]:
    """
    Recheck persisted consent identities and permissions immediately before admission.

    Args:
        api (API): Fresh, decision-capturing Kubernetes reader.
        receipt (dict[str, Any]): Pending immutable proposal.
        settings (ConnectionSettings): Validated connection namespace policy.

    Returns:
        set[str]: Endpoints whose approvals still have live identity and authorization.
    """
    from polyad.api.connections.store import Caller, ConnectionStore

    store = ConnectionStore(api, settings)
    result = set()
    for node, value in decisions(receipt).items():
        if value["decision"] != "Approve":
            continue
        try:
            caller = Caller(value["username"], value["uid"], value["namespace"], tuple(value["groups"]), value["extra"])
            if await endpoint(api, caller, receipt) != node:
                continue
            await store.authorize(
                caller, receipt["spec"]["namespace"], receipt["spec"]["kind"], receipt["spec"]["graph"], verb=value["verb"]
            )
        except (Forbidden, KeyError, TypeError):
            continue
        result.add(node)
    return result
