"""
Fence workload connection admission against the currently observed consent receipt.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    from polyad_sdk.connections.endpoint import WorkloadEndpoint
    from polyad_sdk.symbiosis.models import Environment
    from polyad_types import ServiceEndpoint

__all__ = ("authorize_connection",)


def authorize_connection(home: ServiceEndpoint, endpoint: WorkloadEndpoint, view: Environment, uid: str, now: float) -> None:
    """
    Require fresh topology and an active, matching, directed TCP grant.

    Args:
        home (ServiceEndpoint): Exact source identity owned by the adaptive service.
        endpoint (WorkloadEndpoint): Explicit destination and destination Pod port.
        view (Environment): Newly evaluated application observation state.
        uid (str): Operator-issued connection receipt UID, not a request ID.
        now (float): Current Unix time, using the adaptive service's clock.

    Returns:
        None: The observed grant permits opening this connection.

    Raises:
        PermissionError: Evidence is stale, malformed, expired or does not authorize this edge.
    """
    receipt = view.connections.get(uid)
    if not view.available or not receipt or receipt.get("uid") != uid or receipt.get("revokeRequested"):
        raise PermissionError("a fresh, unrevoked connection receipt is required")
    status = receipt.get("status")
    if not isinstance(status, Mapping) or status.get("phase") != "Active":
        raise PermissionError("connection receipt is not Active")
    try:
        expiry = datetime.fromisoformat(str(receipt["expiresAt"]).replace("Z", "+00:00"))
        if expiry.tzinfo is None or expiry.timestamp() <= now:
            raise ValueError("expired or timezone absent")
    except (KeyError, ValueError, OverflowError) as error:
        raise PermissionError("connection receipt has no valid remaining lifetime") from error

    target = receipt.get("target")
    if not isinstance(target, Mapping):
        raise PermissionError("connection receipt has no target")

    # Atlas receipts carry both complete identities. Local receipts instead bind
    # two node names to a single namespace and exact graph incarnation.
    peers = receipt.get("peers")
    if peers:

        def matches(value: Any, identity: ServiceEndpoint) -> bool:
            return isinstance(value, Mapping) and all(
                value.get(field) == getattr(identity, field) for field in ("cluster", "namespace", "kind", "graph", "graphUid", "node")
            )

        forward = isinstance(peers, Mapping) and matches(peers.get("source"), home) and matches(peers.get("target"), endpoint.identity)
        reverse = isinstance(peers, Mapping) and matches(peers.get("target"), home) and matches(peers.get("source"), endpoint.identity)
    else:
        boundary = all(
            getattr(home, field) == getattr(endpoint.identity, field) for field in ("cluster", "namespace", "kind", "graph", "graphUid")
        )
        boundary = boundary and receipt.get("namespace") == home.namespace
        boundary = boundary and all(target.get(field) == getattr(home, field) for field in ("kind", "graph", "graphUid"))
        forward = boundary and target.get("source") == home.node and target.get("target") == endpoint.identity.node
        reverse = boundary and target.get("target") == home.node and target.get("source") == endpoint.identity.node
    if not forward and not (reverse and target.get("bidirectional") is True):
        raise PermissionError("connection receipt does not authorize these directed workload identities")

    # HTTP, WebSocket, gRPC and broker protocols all require a TCP grant. A UDP
    # grant with the same number, or an empty structural edge, grants no access.
    ports = target.get("ports")
    if not isinstance(ports, (list, tuple)) or not any(
        isinstance(port, Mapping) and port.get("port") == endpoint.network_port.port and port.get("protocol", "TCP") == "TCP"
        for port in ports
    ):
        raise PermissionError("connection receipt does not grant the destination TCP port")
