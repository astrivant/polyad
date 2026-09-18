"""
Admit and clean up service-specific policy grants on every participating cluster.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from polyad.api.connections.consent import decisions
from polyad.api.connections.paths import identities, path
from polyad.api.http.errors import Conflict, Forbidden, Unavailable
from polyad.compiler.passes.network import NetworkScope, traffic
from polyad.events.access import configuration, require_scope
from polyad.graph.service_connections import ANNOTATION, grants
from polyad.graph.temporary import deadline
from polyad.operator.coordination.contracts import expires_before
from polyad.operator.policies.network import context
from polyad.operator.policies.rule_state import check_live_rules
from polyad_types.codec import converter
from polyad_types.discovery import ServiceEndpoint
from polyad_types.network import MeshPeer, NetworkAccess, TrafficRule

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad.operator.reconciliation.controller import Controller


def resolver(controller: Controller, namespace: str) -> Callable[[str], tuple[API, str]]:
    """
    Reuse fenced root transports and reject unregistered destinations.

    Args:
        controller (Controller): Current boundary controller holding graph-family ownership.
        namespace (str): Namespace of the common boundary.

    Returns:
        Callable[[str], tuple[API, str]]: Cluster resolver including this controller's local cluster.
    """

    def resolve(cluster: str) -> tuple[API, str]:
        if not cluster or cluster == controller.federation.name:
            return controller.api, namespace
        return controller.federation.target(cluster)

    return resolve


def capabilities(source: list[tuple[API, dict[str, Any], str, str]], target: list[tuple[API, dict[str, Any], str, str]]) -> None:
    """
    Reject negotiation that this deployment cannot execute and report to its services.

    Args:
        source (list[tuple[API, dict[str, Any], str, str]]): Verified sending service path.
        target (list[tuple[API, dict[str, Any], str, str]]): Verified receiving service path.

    Returns:
        None: Missing network or root capabilities raise Unavailable before durable intake.
    """
    for ancestry in (source, target):
        if not ancestry[0][1]["spec"].get("network"):
            raise Unavailable("child graph requires an explicit network contract for service negotiation")
    if source[0][3] != target[0][3]:
        if (
            os.environ.get("POLYAD_ROOT_ENABLED", "false").lower() != "true"
            or os.environ.get("POLYAD_MESH_ENABLED", "false").lower() != "true"
        ):
            raise Unavailable("cross-cluster negotiation requires root coordination, event streams and mesh integration")
        peers = {peer["name"] for peer in json.loads(os.environ.get("POLYAD_ROOT_MESH_PEERS", "[]"))}
        domains = configuration().trustDomains
        for ancestry in (source, target):
            if ancestry[0][3] not in peers or ancestry[0][3] not in domains or not ancestry[0][1]["spec"]["network"].get("mesh"):
                raise Unavailable("child operator lacks the required mesh peer, trust domain or network.mesh contract")


async def validate(controller: Controller, receipt: dict[str, Any]) -> dict[str, list[tuple[API, dict[str, Any], str, str]]]:
    """
    Recheck all endpoint ownership paths, child ceilings and local GraphRules before granting traffic.

    Args:
        controller (Controller): Common-boundary owner.
        receipt (dict[str, Any]): Immutable proposal containing exact peers.

    Returns:
        dict[str, list[tuple[API, dict[str, Any], str, str]]]: Fresh paths, keyed by source and target.
    """
    resolve = resolver(controller, receipt["metadata"]["namespace"])
    paths = {
        side: await path(converter.structure(peer, ServiceEndpoint), resolve, controller.federation.name)
        for side, peer in receipt["spec"]["peers"].items()
    }
    capabilities(paths["source"], paths["target"])
    require_scope("connections", identities(paths["source"]), identities(paths["target"]))
    require_scope("connections", identities(paths["target"]), identities(paths["source"]))
    for side, ancestry in paths.items():
        if not any(
            obj["metadata"]["uid"] == receipt["spec"]["graphUid"]
            and branch == receipt["spec"][side]
            and cluster == controller.federation.name
            for _, obj, branch, cluster in ancestry
        ):
            raise Conflict("service no longer belongs to the approved common graph boundary")
        api, obj, node, _ = ancestry[0]
        if not obj["spec"].get("network"):
            raise Forbidden("child graph cannot fulfill service negotiation without an explicit network contract")
        children = await api.owned(obj["metadata"]["namespace"], obj["metadata"]["uid"])
        if not any(
            child["kind"] in {"Deployment", "StatefulSet", "DaemonSet", "Job"}
            and child["metadata"].get("labels", {}).get("polyad.astrivant.com/node") == node
            and not child["metadata"].get("deletionTimestamp")
            for child in children
        ):
            raise Unavailable("child operator cannot fulfill a connection before the exact workload exists")
        await check_live_rules(api, obj)
    return paths


def policies(receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Construct exact-node exceptions using administrator-registered mesh transports.

    Args:
        receipt (dict[str, Any]): Fully consented service proposal.

    Returns:
        dict[str, dict[str, Any]]: Per-endpoint grants, including immutable expiry and graph UID.
    """
    peers, spec = receipt["spec"]["peers"], receipt["spec"]
    configured = (
        os.environ.get("POLYAD_ROOT_MESH_PEERS", "[]")
        if os.environ.get("POLYAD_ROOT_ENABLED", "false").lower() == "true"
        else os.environ.get("POLYAD_MESH_PEERS", "[]")
    )
    transports = {peer.name: peer for peer in converter.structure(json.loads(configured), tuple[MeshPeer, ...])}
    consent = decisions(receipt)
    result = {
        side: {"graphUid": peer["graphUid"], "node": peer["node"], "expiresAt": deadline(receipt).isoformat(), "ingress": [], "egress": []}
        for side, peer in peers.items()
    }
    if not spec.get("ports"):
        return result  # Portless edges remain structural, never unrestricted transport rules.
    pairs = [("source", "target"), *(([("target", "source")]) if spec.get("bidirectional") else [])]
    for sending, receiving in pairs:
        source, target = peers[sending], peers[receiving]
        remote = source["cluster"] != target["cluster"]
        outgoing = {"namespace": target["namespace"], "kind": target["kind"], "graph": target["graph"], "node": target["node"]}
        incoming = {"namespace": source["namespace"], "kind": source["kind"], "graph": source["graph"], "node": source["node"]}
        ports = spec["ports"]
        principals = []
        if remote:
            if source["cluster"] not in transports or target["cluster"] not in transports:
                raise Unavailable("child operator has no registered mesh transport for both connection peers")
            if not ports or any(port.get("protocol", "TCP") != "TCP" for port in ports):
                raise Forbidden("cross-cluster connections require explicit TCP ports")
            outgoing, incoming = {"cluster": target["cluster"]}, {"cluster": source["cluster"]}
            username = consent[spec[sending]]["username"].split(":")
            domain = configuration().trustDomains.get(source["cluster"])
            if not domain:
                raise Unavailable("child operator lacks an explicit mesh trust domain")
            principals = [f"{domain}/ns/{username[2]}/sa/{username[3]}"]
            if transports[target["cluster"]].mode == "Gateway":
                ports = [{"port": transports[target["cluster"]].gatewayPort, "protocol": "TCP"}]
        result[sending]["egress"].append({"node": source["node"], "peer": outgoing, "ports": ports})
        result[receiving]["ingress"].append({"node": target["node"], "peer": incoming, "ports": spec["ports"], "principals": principals})
    return result


async def reconcile(controller: Controller, receipt: dict[str, Any], *, remove: bool = False) -> None:
    """
    Persist each participant's bounded grant and observe policies before declaring completion.

    Args:
        controller (Controller): Common graph-family owner.
        receipt (dict[str, Any]): Durable journal of both exact graph addresses.
        remove (bool): Remove this grant even if current scope, consent or graph rules reject new work.

    Returns:
        None: Every destination has acknowledged its policies; pending work retries from fresh reads.
    """
    from polyad.operator.policies.connections import refresh_network
    from polyad.operator.reconciliation.controller import Controller, Pending

    resolve = resolver(controller, receipt["metadata"]["namespace"])
    proposed = {} if remove else policies(receipt)
    changed = False
    for side, peer in receipt["spec"]["peers"].items():
        api, namespace = resolve(peer["cluster"])
        obj = await api.get(peer["kind"], namespace, peer["graph"])
        if obj is None or obj["metadata"]["uid"] != peer["graphUid"]:
            if remove:
                continue
            raise Conflict("service graph disappeared before applying its connection grant")
        meta = obj["metadata"]
        current = grants(obj, active=False)
        previous = copy.deepcopy(current)
        uid = receipt["metadata"]["uid"]
        if remove:
            current.pop(uid, None)
        else:
            if len(current) >= 128 and uid not in current:
                raise Forbidden("child graph service-connection capacity is exhausted")
            current[uid] = proposed[side]
        encoded = json.dumps(current, sort_keys=True)
        if len(encoded.encode()) > 131072:
            raise Forbidden("child graph service grants exceed their storage budget")
        candidate = copy.deepcopy(obj)
        candidate["metadata"].setdefault("annotations", {})[ANNOTATION] = encoded
        if not remove:
            _, scopes = await context(api, candidate, peer["node"])
            opposite = receipt["spec"]["peers"]["target" if side == "source" else "source"]
            other_api, _ = resolve(opposite["cluster"])
            other = await other_api.get(opposite["kind"], opposite["namespace"], opposite["graph"])
            if other is None or other["metadata"]["uid"] != opposite["graphUid"]:
                raise Conflict("connection peer disappeared during policy validation")
            labels, _ = await context(other_api, other, opposite["node"])
            for direction in ("ingress", "egress"):
                for rule in proposed[side][direction]:
                    desired = NetworkAccess(
                        allowWithin=False,
                        allowDNS=False,
                        mesh=True,
                        ingress=(converter.structure(rule, TrafficRule),) if direction == "ingress" else (),
                        egress=(converter.structure(rule, TrafficRule),) if direction == "egress" else (),
                    )
                    required = traffic([NetworkScope(namespace, obj["kind"], meta["name"], peer["node"], desired)], direction)
                    effective = traffic(scopes, direction)
                    assert required is not None
                    wanted = required[0]
                    if effective is None or not any(
                        term.get("cluster") == wanted.get("cluster")
                        and term["namespace"] == wanted["namespace"]
                        and all(labels.get(key) == value for key, value in term["labels"].items())
                        and (not term["principals"] or set(wanted["principals"]) <= set(term["principals"]))
                        and not term["methods"]
                        and not term["paths"]
                        and (not term["ports"] or set(wanted["ports"]) <= set(term["ports"]))
                        for term in effective
                    ):
                        raise Forbidden("child or ancestor network policy cannot fulfill the complete service connection")
            expires_before(deadline(receipt))
            if datetime.now(UTC) >= deadline(receipt):
                raise Conflict("connection expired before policy admission")
        if previous != current:
            obj = await api.request(
                "PATCH",
                peer["kind"],
                namespace,
                peer["graph"],
                {"metadata": {"resourceVersion": meta["resourceVersion"], "annotations": {ANNOTATION: encoded if current else None}}},
            )
        child = Controller(api)
        child.federation.name = peer["cluster"] or controller.federation.name
        try:
            await refresh_network(child, obj, revoking=remove)
        except Pending:
            changed = True
    if changed:
        raise Pending("waiting for both child operators' service policies to be observed")
