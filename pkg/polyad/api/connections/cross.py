"""
Validate atlas service proposals before durable intake.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from attrs import evolve

from polyad.api.connections.consent import endpoint
from polyad.api.connections.paths import identities, path
from polyad.events.access import require_scope
from polyad.exceptions.api import Conflict, Forbidden, Unavailable
from polyad_types.api.requests import ConnectionRequest
from polyad_types.serialization import converter

if TYPE_CHECKING:
    from polyad.api.connections.store import Caller, ConnectionStore
    from polyad_types.api.requests import ServiceConnectionRequest

__all__ = (
    "authorize_request",
    "connection_request",
)


async def connection_request(store: ConnectionStore, request: ServiceConnectionRequest) -> ConnectionRequest:
    """
    Locate the narrowest shared boundary without silently escalating to its operator.

    Args:
        store (ConnectionStore): This operator's intake and registered cluster transports.
        request (ServiceConnectionRequest): Exact source and destination service identities.

    Returns:
        ConnectionRequest: Boundary edge retaining exact leaf participants for network admission.
    """
    source = await path(request.source, store.resolve, store.federation.name)
    target = await path(request.target, store.resolve, store.federation.name)

    # Both administrators must allow negotiation; one endpoint's broader scope cannot authorize its peer.
    require_scope("connections", identities(source), identities(target))
    require_scope("connections", identities(target), identities(source))
    same = identities(source)[0] == identities(target)[0]
    if not same:
        if os.environ.get("POLYAD_EVENTS_ENABLED", "false").lower() != "true":
            raise Unavailable("service negotiation requires this operator to publish participant events")
        from polyad.operator.policies.service_connections import capabilities

        capabilities(source, target)
        for api, graph, node, _ in (source[0], target[0]):
            children = await api.owned(graph["metadata"]["namespace"], graph["metadata"]["uid"])
            if not any(
                child["kind"] in {"Deployment", "StatefulSet", "DaemonSet", "Job"}
                and child["metadata"].get("labels", {}).get("polyad.astrivant.com/node") == node
                and not child["metadata"].get("deletionTimestamp")
                for child in children
            ):
                raise Unavailable("child operator cannot fulfill negotiation before the exact workload exists")
    destinations = {(cluster, obj["metadata"]["uid"]): branch for _, obj, branch, cluster in target}
    for api, obj, branch, cluster in source:
        opposite = destinations.get((cluster, obj["metadata"]["uid"]))
        if opposite is None or branch == opposite:
            continue
        if api is not store.api:
            raise Unavailable("this operator does not own the common boundary; explicitly address its owning operator")
        return ConnectionRequest(
            request.requestId,
            obj["metadata"]["namespace"],
            obj["kind"],
            obj["metadata"]["name"],
            obj["metadata"]["uid"],
            branch,
            opposite,
            request.ttlSeconds,
            request.ports,
            request.bidirectional,
            {}
            if same
            else {"source": evolve(request.source, cluster=source[0][3]), "target": evolve(request.target, cluster=target[0][3])},
        )
    raise Forbidden("services do not share an application graph boundary managed by this operator")


async def authorize_request(store: ConnectionStore, request: ConnectionRequest, caller: Caller) -> None:
    """
    Recompute scope and require the requester to represent the exact sending service.

    Args:
        store (ConnectionStore): Intake store.
        request (ConnectionRequest): Boundary proposal with explicit participants.
        caller (Caller): Projected-token identity verified in its home cluster.

    Returns:
        None: Inconsistent boundaries, access modes or participant permissions reject intake.
    """
    from polyad_types.api.requests import ServiceConnectionRequest

    proposed = ServiceConnectionRequest(
        request.requestId, request.peers["source"], request.peers["target"], request.ttlSeconds, request.ports, request.bidirectional
    )
    expected = await connection_request(store, proposed)
    if request != expected:
        raise Conflict("requested boundary does not match the current endpoint ancestry")
    receipt = {"metadata": {"namespace": request.namespace}, "spec": converter.unstructure(request)}
    if await endpoint(store.api, caller, receipt, resolve=store.resolve) != request.source:
        raise Forbidden("only the verified source service may initiate an atlas connection")
    await store.authorize_receipt(caller, receipt)
