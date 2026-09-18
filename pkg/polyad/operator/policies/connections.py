"""
Admit temporary edges under the graph-family lease and revoke expired grants.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cattrs.errors import CattrsError

from polyad.api.connections.consent import confirmed, decisions
from polyad.api.connections.store import FINALIZER, ConnectionSettings
from polyad.api.http.errors import Conflict, Forbidden, Unavailable
from polyad.compiler.passes.network import NetworkScope
from polyad.graph.temporary import ANNOTATION, CLEANUP, MAX_CONNECTIONS, active_entries, deadline, entries, overlay
from polyad.operator.coordination.contracts import expires_before
from polyad.operator.observability.decisions import decision
from polyad.operator.policies.network import context, ensure_policies
from polyad.operator.policies.rule_state import check_live_rules
from polyad.operator.policies.rules import RuleViolation
from polyad.operator.reconciliation.replication import effective_spec
from polyad_types import resources as asts
from polyad_types.codec import converter
from polyad_types.network import NetworkAccess
from polyad_types.replication import replica_topology
from polyad_types.requests import ConnectionRequest

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.reconciliation.controller import Controller

REVOKE_REASON = f"{asts.GROUP}/connection-revocation-reason"


async def write_grants(controller: Controller, graph: dict[str, Any], grants: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """
    Atomically replace only this graph's temporary overlay with a revision fence.

    Args:
        controller (Controller): Controller holding the root-family shard.
        graph (dict[str, Any]): Refreshed target instance.
        grants (dict[str, dict[str, Any]]): Remaining admitted receipts.

    Returns:
        dict[str, Any]: Acknowledged graph document.
    """
    encoded = json.dumps(grants, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > 131072 or len(grants) > MAX_CONNECTIONS:
        raise ValueError("temporary connections exceed the per-graph annotation budget")
    meta = graph["metadata"]
    annotations = {ANNOTATION: encoded if grants else None}
    if entries(graph).keys() - grants.keys():
        annotations[CLEANUP] = "true"
    result: dict[str, Any] = await controller.api.request(
        "PATCH",
        graph["kind"],
        meta["namespace"],
        meta["name"],
        {"metadata": {"resourceVersion": meta["resourceVersion"], "annotations": annotations}},
    )
    return result


async def refresh_network(controller: Controller, root: dict[str, Any], *, revoking: bool) -> None:
    """
    Refresh policies on existing descendant workloads without creating or replacing Pods.

    Args:
        controller (Controller): Controller holding the family's existing shard.
        root (dict[str, Any]): Boundary whose connection overlay changed.
        revoking (bool): Fail closed if stale or invalid graph intent prevents safe policy reconstruction.

    Returns:
        None: Policies are observed after writes before a receipt becomes terminal.
    """
    from polyad.operator.reconciliation.controller import Pending

    pending = [root]
    seen: set[str] = set()
    changed = False
    while pending:
        graph = pending.pop()
        meta = graph["metadata"]
        if meta["uid"] in seen or len(seen) >= 256:
            raise ValueError("temporary connection cleanup exceeds the graph-family boundary limit")
        seen.add(meta["uid"])
        children = await controller.api.owned(meta["namespace"], meta["uid"])
        pending.extend(child for child in children if child["kind"] in asts.BOUNDARY_KINDS)
        nodes = {
            child["metadata"].get("labels", {}).get(f"{asts.GROUP}/node")
            for child in children
            if child["kind"] in {"Job", "Deployment", "StatefulSet", "DaemonSet"}
        } - {None}
        plans = {}
        for node in sorted(nodes):
            try:
                _, scopes = await context(controller.api, graph, node)
            except (ValueError, TypeError, KeyError, Pending, CattrsError):
                if not revoking:
                    raise
                # Invalid rules or a deleting ancestor must never preserve an
                # expired allowance. Keep an explicit deny policy until normal
                # graph reconciliation can rebuild the intended contract.
                scopes = [
                    NetworkScope(
                        meta["namespace"],
                        graph["kind"],
                        meta["name"],
                        node,
                        NetworkAccess(
                            allowWithin=False, allowDNS=False, mesh=any(child["kind"] == "AuthorizationPolicy" for child in children)
                        ),
                    )
                ]
            plans[node] = scopes
        try:
            await ensure_policies(controller, graph, plans)
        except Pending:
            changed = True
    if changed:
        raise Pending("waiting to observe temporary connection policy updates")
    if revoking and root["metadata"].get("annotations", {}).get(CLEANUP) == "true":
        meta = root["metadata"]
        await controller.api.request(
            "PATCH",
            root["kind"],
            meta["namespace"],
            meta["name"],
            {"metadata": {"resourceVersion": meta["resourceVersion"], "annotations": {CLEANUP: None}}},
        )


async def cleanup_connections(controller: Controller, graph: dict[str, Any]) -> dict[str, Any]:
    """
    Reconcile tracked grants before graph admission, including orphaned receipts.

    Args:
        controller (Controller): Controller holding the graph-family lease.
        graph (dict[str, Any]): Current boundary intent with tracked connection metadata.

    Returns:
        dict[str, Any]: Fresh boundary after cleanup; incomplete policy writes remain pending.
    """
    from polyad.operator.reconciliation.controller import Pending

    meta = graph["metadata"]

    async def refreshed() -> dict[str, Any]:
        current = await controller.api.get(graph["kind"], meta["namespace"], meta["name"])
        if current is None or current["metadata"]["uid"] != meta["uid"]:
            raise Pending("connection graph changed during cleanup")
        return current

    if meta.get("annotations", {}).get(CLEANUP) == "true":
        await refresh_network(controller, graph, revoking=True)
        graph = await refreshed()
    grants = entries(graph)
    if not grants:
        return graph
    listing = await controller.api.request(
        "GET", "TemporaryConnection", meta["namespace"], query=[("labelSelector", f"{asts.GROUP}/owner={meta['uid']}")]
    )
    receipts = {item["metadata"]["uid"]: item for item in listing.get("items", [])}
    orphaned = grants.keys() - receipts.keys()
    if orphaned:
        graph = await write_grants(controller, graph, {uid: grant for uid, grant in grants.items() if uid not in orphaned})
        decision(
            "polyad.connections.cleaned",
            "Removed temporary connection grants whose receipts no longer exist.",
            obj=graph,
            outcome="applied",
            reason="connection_receipts_absent",
            attributes={"polyad.connections.removed": len(orphaned)},
        )
        await refresh_network(controller, graph, revoking=True)
    for uid in sorted(grants.keys() & receipts.keys()):
        await reconcile_connection(controller, receipts[uid])
    return await refreshed()


async def finish(controller: Controller, receipt: dict[str, Any], graph: dict[str, Any] | None, phase: str, message: str) -> None:
    """
    Revoke only this receipt's contribution, then retain a bounded audit record.

    Args:
        controller (Controller): Leased controller.
        receipt (dict[str, Any]): Fresh request or deleting receipt.
        graph (dict[str, Any] | None): Matching graph incarnation, if it still exists.
        phase (str): Expired, Revoked or Rejected result.
        message (str): User-visible reason.

    Returns:
        None: Finalizers are released only after policy cleanup has been observed.
    """
    meta = receipt["metadata"]
    if receipt["spec"].get("peers"):
        from polyad.operator.policies.service_connections import reconcile as reconcile_services

        if meta.get("annotations", {}).get(f"{asts.GROUP}/revoke-requested") != "true":
            receipt = await controller.api.request(
                "PATCH",
                "TemporaryConnection",
                meta["namespace"],
                meta["name"],
                {
                    "metadata": {
                        "resourceVersion": meta["resourceVersion"],
                        "annotations": {
                            f"{asts.GROUP}/revoke-requested": "true",
                            REVOKE_REASON: message,
                            f"{asts.GROUP}/connection-terminal-phase": phase,
                        },
                    }
                },
            )
            meta = receipt["metadata"]
        await reconcile_services(controller, receipt, remove=True)
    if graph is not None:
        grants = entries(graph)
        if meta["uid"] in grants:
            del grants[meta["uid"]]
            graph = await write_grants(controller, graph, grants)
        # Retry policy cleanup even if the graph annotation write succeeded in
        # an earlier attempt whose policy write or acknowledgement failed.
        await refresh_network(controller, graph, revoking=True)
    status = receipt.get("status", {})
    finished = status.get("finishedAt") or datetime.now(UTC).isoformat()
    if not meta.get("deletionTimestamp"):
        await controller.status(
            receipt,
            {
                "phase": phase,
                "message": message,
                "expiresAt": deadline(receipt).isoformat(),
                "finishedAt": finished,
                "observedGeneration": meta["generation"],
            },
        )
    latest = await controller.api.get("TemporaryConnection", meta["namespace"], meta["name"])
    if latest is None:
        return
    if latest["metadata"].get("deletionTimestamp"):
        if FINALIZER in latest["metadata"].get("finalizers", []):
            await controller.api.request(
                "PATCH",
                "TemporaryConnection",
                meta["namespace"],
                meta["name"],
                {
                    "metadata": {
                        "resourceVersion": latest["metadata"]["resourceVersion"],
                        "finalizers": [value for value in latest["metadata"].get("finalizers", []) if value != FINALIZER],
                    }
                },
            )
    elif datetime.now(UTC) >= datetime.fromisoformat(finished) + timedelta(
        seconds=ConnectionSettings.from_environment(os.environ.get("POLYAD_NAMESPACE", meta["namespace"])).retention
    ):
        await controller.api.delete(latest)


async def reconcile_connection(controller: Controller, receipt: dict[str, Any]) -> None:
    """
    Admit against fresh family rules or revoke at the immutable TTL deadline.

    Args:
        controller (Controller): Controller holding the request's owning graph-family shard.
        receipt (dict[str, Any]): Persisted TemporaryConnection intent.

    Returns:
        None: Admission, policy observation or cleanup advances one durable step.
    """
    from polyad.operator.reconciliation.controller import Pending

    meta, spec = receipt["metadata"], receipt["spec"]
    request = converter.structure({key: value for key, value in spec.items() if key != "requester"}, ConnectionRequest)
    expires = deadline(receipt)
    graph = await controller.api.get(request.kind, meta["namespace"], request.graph)
    if graph is None or graph["metadata"]["uid"] != request.graphUid:
        await finish(controller, receipt, None, "Rejected", "target graph incarnation is no longer present")
        return
    if request.namespace != meta["namespace"] or not any(
        owner.get("controller")
        and owner.get("apiVersion") == f"{asts.GROUP}/{asts.VERSION}"
        and owner.get("uid") == request.graphUid
        and owner.get("name") == request.graph
        and owner.get("kind") == request.kind
        for owner in meta.get("ownerReferences", [])
    ):
        raise ValueError("TemporaryConnection ownership must match its target graph")
    terminal = receipt.get("status", {}).get("phase")
    revoked = meta.get("annotations", {}).get(f"{asts.GROUP}/revoke-requested") == "true"
    if (
        terminal in {"Expired", "Revoked", "Rejected"}
        or revoked
        or meta.get("deletionTimestamp")
        or graph["metadata"].get("deletionTimestamp")
        or datetime.now(UTC) >= expires
    ):
        phase = terminal if terminal in {"Expired", "Revoked", "Rejected"} else "Expired" if datetime.now(UTC) >= expires else "Revoked"
        recorded = meta.get("annotations", {}).get(f"{asts.GROUP}/connection-terminal-phase")
        if recorded in {"Rejected", "Revoked", "Expired"}:
            phase = recorded
        message = (
            receipt.get("status", {}).get("message")
            if terminal in {"Expired", "Revoked", "Rejected"}
            else meta.get("annotations", {}).get(REVOKE_REASON)
        )
        await finish(controller, receipt, graph, phase, message or "connection expired or revocation requested")
        return
    settings = ConnectionSettings.from_environment(os.environ.get("POLYAD_NAMESPACE", meta["namespace"]))
    username = spec["requester"]["username"].split(":")
    if (
        len(username) != 4
        or username[:2] != ["system", "serviceaccount"]
        or not settings.allows(username[2])
        or not settings.allows(meta["namespace"])
    ):
        await finish(controller, receipt, graph, "Rejected", "caller or target is outside this operator's connections scope")
        return
    if (
        request.ttlSeconds > settings.max_ttl
        or graph["spec"].get("templateOnly")
        or graph["spec"].get("suspend")
        or graph.get("status", {}).get("phase") == "Stopped"
    ):
        await finish(controller, receipt, graph, "Rejected", "target or TTL is not eligible for temporary connections")
        return
    from polyad.events.access import configuration
    from polyad_types.discovery import AccessMode

    if configuration().effective(controller.federation.name, "connections") == AccessMode.DISABLED:
        await finish(controller, receipt, graph, "Rejected", "connections are disabled by this operator access mode")
        return
    if request.peers:
        from polyad.operator.policies.service_connections import validate

        try:
            await validate(controller, receipt)
        except (Conflict, Forbidden, Unavailable, ValueError, RuleViolation) as error:
            await finish(controller, receipt, graph, "Rejected", str(error))
            return
    grants = entries(graph)
    base = copy.deepcopy(graph)
    consent = decisions(receipt)
    if any(value["decision"] == "Reject" for value in consent.values()):
        await finish(controller, receipt, graph, "Rejected", "a participating service rejected the connection")
        return
    if graph["kind"] == "ReplicaGroup":
        base["spec"], _ = await effective_spec(controller.api, graph)
        base["spec"] = replica_topology(base["spec"])
    nodes = {node["name"] for node in base["spec"].get("nodes", [])}
    missing = {request.source, request.target} - nodes
    if missing or (terminal == "Active" and meta["uid"] not in grants):
        message = (
            f"connection endpoint removed from the graph: {', '.join(sorted(missing))}"
            if missing
            else "admitted connection grant is no longer present"
        )
        if meta["uid"] not in grants and terminal != "Active":
            await finish(controller, receipt, graph, "Rejected", message)
            return
        # Persist revocation before removing the grant: a retry must not restore
        # it if the endpoint returns while network policy cleanup is incomplete.
        receipt = await controller.api.request(
            "PATCH",
            "TemporaryConnection",
            meta["namespace"],
            meta["name"],
            {
                "metadata": {
                    "resourceVersion": meta["resourceVersion"],
                    "annotations": {
                        f"{asts.GROUP}/revoke-requested": "true",
                        REVOKE_REASON: message,
                        f"{asts.GROUP}/connection-terminal-phase": "Revoked",
                    },
                }
            },
        )
        decision(
            "polyad.connections.revoking",
            f"Revoking temporary connection: {message}.",
            obj=receipt,
            outcome="applied",
            reason="connection_endpoint_removed" if missing else "connection_grant_absent",
        )
        await finish(controller, receipt, graph, "Revoked", message)
        return
    if terminal != "Active":
        if meta["uid"] not in grants and os.environ.get("POLYAD_CONNECTIONS_ENABLED", "false").lower() != "true":
            await finish(controller, receipt, graph, "Rejected", "temporary connection admission is disabled in this namespace")
            return
        from polyad.operator.policies.service_connections import resolver

        approved = await confirmed(controller.api, receipt, settings, resolve=resolver(controller, meta["namespace"]))
        awaiting = sorted({request.source, request.target} - approved)
        if awaiting:
            if meta["uid"] in grants:
                await finish(controller, receipt, graph, "Revoked", "service consent became unavailable before activation")
                return
            await controller.status(
                receipt,
                {
                    "phase": "Pending",
                    "expiresAt": expires.isoformat(),
                    "awaitingApproval": awaiting,
                    "message": "waiting for authenticated service consent",
                    "observedGeneration": meta["generation"],
                },
            )
            return
    elif set(consent) != {request.source, request.target}:
        await finish(controller, receipt, graph, "Revoked", "connection lacks the required service consent")
        return
    if meta["uid"] not in grants:
        if os.environ.get("POLYAD_CONNECTIONS_ENABLED", "false").lower() != "true":
            await finish(controller, receipt, graph, "Rejected", "temporary connection admission is disabled in this namespace")
            return
        grants = active_entries(graph)
        if len(grants) >= MAX_CONNECTIONS:
            await finish(controller, receipt, graph, "Rejected", "graph already has 128 temporary connections")
            return
        grants[meta["uid"]] = {
            "graphUid": request.graphUid,
            "source": request.source,
            "target": request.target,
            "ports": [] if request.peers else converter.unstructure(request.ports),
            "bidirectional": request.bidirectional,
            "expiresAt": expires.isoformat(),
        }
        proposed = copy.deepcopy(base)
        proposed["metadata"].setdefault("annotations", {})[ANNOTATION] = json.dumps(grants)
        try:
            await check_live_rules(controller.api, base, candidate=overlay(proposed, base["spec"]))
        except RuleViolation as error:
            await finish(controller, receipt, graph, "Rejected", str(error))
            return
        refreshed = await controller.api.get("TemporaryConnection", meta["namespace"], meta["name"])
        if refreshed is None or refreshed["metadata"]["resourceVersion"] != meta["resourceVersion"] or datetime.now(UTC) >= expires:
            raise Pending("temporary connection intent or deadline changed before admission")
        expires_before(expires)
        graph = await write_grants(controller, graph, grants)
    if request.peers:
        from polyad.operator.policies.service_connections import reconcile as reconcile_services

        try:
            await reconcile_services(controller, receipt)
        except (Conflict, Forbidden, Unavailable, ValueError) as error:
            await finish(controller, receipt, graph, "Rejected", str(error))
            return
    if terminal == "Active":
        return
    # Re-evaluate on policy retries too: a persisted annotation may precede
    # rule edits or workload creation while admission is still pending.
    base = copy.deepcopy(graph)
    if graph["kind"] == "ReplicaGroup":
        base["spec"], _ = await effective_spec(controller.api, graph)
        base["spec"] = replica_topology(base["spec"])
    try:
        await check_live_rules(controller.api, base)
    except RuleViolation as error:
        await finish(controller, receipt, graph, "Rejected", str(error))
        return
    await refresh_network(controller, graph, revoking=False)
    latest = await controller.api.get("TemporaryConnection", meta["namespace"], meta["name"])
    if latest is not None and datetime.now(UTC) < expires:
        await controller.status(
            latest,
            {
                "phase": "Active",
                "awaitingApproval": [],
                "expiresAt": expires.isoformat(),
                "observedAt": datetime.now(UTC).isoformat(),
                "observedGeneration": latest["metadata"]["generation"],
            },
        )
