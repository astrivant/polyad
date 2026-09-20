"""
Persist graph-owned Istio routes using refreshed constraints and ownership fences.
"""

from __future__ import annotations

import copy
import hashlib
import os
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING

from polyad.compiler.passes.network import scope_label
from polyad.compiler.passes.traffic import route_specs
from polyad.operator.observability.decisions import decision
from polyad.operator.policies.rule_state import check_live_rules
from polyad.operator.reconciliation.replication import effective_spec
from polyad_types import resources as asts
from polyad_types.graphs.topology import topology

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.reconciliation.controller import Controller

__all__ = (
    "KINDS",
    "ensure_routes",
    "target_selector",
)


KINDS = frozenset({"VirtualService", "DestinationRule"})


async def target_selector(
    controller: Controller, obj: dict[str, Any], target: str, *, executions: dict[str, Any] | None = None, allow_absent: bool = False
) -> dict[str, str]:
    """
    Resolve a graph-replica entrypoint through current local owner identities.

    Args:
        controller (Controller): Refreshed Kubernetes reader.
        obj (dict[str, Any]): Graph declaring the incoming traffic route.
        target (str): Relative node path, including replica ordinals where applicable.
        executions (dict[str, Any] | None): Optional output map receiving the terminal execution resource.
        allow_absent (bool): Allow a zero-weight destination to remain declared after scale-in.

    Returns:
        dict[str, str]: Intersection of membership labels along the selected subtree.
    """
    from polyad.operator.reconciliation.controller import Pending, child_name

    current = obj
    labels = {}
    parts = target.split("/")
    inactive = {f"{asts.GROUP}/traffic-inactive": hashlib.sha256(target.encode()).hexdigest()[:32]}
    for index, part in enumerate(parts):
        meta = current["metadata"]
        spec = current["spec"]
        if current["kind"] == "ReplicaGroup":
            spec, _ = await effective_spec(controller.api, current)
        graph = topology(spec, current["kind"])
        node = next((node for node in graph.nodes if node.name == part), None)
        if node is None or getattr(node, "cluster", None):
            if allow_absent and node is None:
                return inactive
            raise Pending("traffic destination is absent or no longer local; adjust its route before changing replicas")
        labels[scope_label(meta["namespace"], current["kind"], meta["name"], part)] = "true"
        if index == len(parts) - 1:
            if node.kind == "Resource":
                raise ValueError("traffic destinations must contain executable workloads")
            if executions is not None:
                matches = [
                    child
                    for child in await controller.api.owned(meta["namespace"], meta["uid"])
                    if child["metadata"].get("labels", {}).get(f"{asts.GROUP}/node") == part
                    and child["kind"] in asts.BOUNDARY_KINDS | {"Deployment", "StatefulSet", "DaemonSet", "Job"}
                    and not child["metadata"].get("deletionTimestamp")
                ]
                if len(matches) != 1:
                    raise Pending("traffic measurement requires one current execution per destination")
                executions[target] = matches[0]
            break
        if node.kind not in asts.BOUNDARY_KINDS:
            raise ValueError("traffic paths can traverse only local graph boundaries")
        child = await controller.api.get(node.kind, meta["namespace"], child_name(current, part))
        if (
            child is None
            or child["metadata"].get("deletionTimestamp")
            or not any(
                owner.get("controller") and owner.get("uid") == meta["uid"] for owner in child["metadata"].get("ownerReferences", [])
            )
        ):
            if allow_absent:
                return inactive
            raise Pending("waiting for the graph replica containing the traffic entrypoint")
        current = child
    return labels


async def ensure_routes(controller: Controller, obj: dict[str, Any]) -> None:
    """
    Reconcile optional percentage routing without adopting administrator-owned policies.

    Args:
        controller (Controller): Family-leased controller with a fenced Kubernetes adapter.
        obj (dict[str, Any]): Fresh graph intent whose routing configuration is applied.

    Returns:
        None: Admission proceeds only after routing writes have been freshly observed.
    """
    from polyad.operator.reconciliation.controller import Pending

    graph = topology(obj["spec"], obj["kind"])
    enabled = os.environ.get("POLYAD_MESH_ENABLED", "false").lower() == "true"
    if graph.traffic and not enabled:
        raise ValueError("traffic routing requires the operator's mesh integration to be enabled")
    if not enabled:
        return
    meta = obj["metadata"]
    namespace = meta["namespace"]

    async def refresh() -> None:
        current = await controller.api.get(obj["kind"], namespace, meta["name"])
        if current is None or any(current["metadata"].get(key) != meta.get(key) for key in ("uid", "generation")):
            raise Pending("graph changed before traffic routing could be applied")
        if current["metadata"].get("deletionTimestamp"):
            raise Pending("graph is deleting; traffic routing is paused")
        await check_live_rules(controller.api, current)

    wanted = set()
    changed = False
    for route in graph.traffic:
        host = f"{route.service}.{namespace}.svc.cluster.local"
        for kind in KINDS:
            existing = (await controller.api.request("GET", kind, namespace)).get("items", [])
            for item in existing:
                if any(
                    owner.get("controller") and owner.get("uid") == meta["uid"] for owner in item["metadata"].get("ownerReferences", [])
                ):
                    continue
                policy = item.get("spec", {})
                if kind == "VirtualService" and "mesh" not in policy.get("gateways", ["mesh"]):
                    continue
                hosts = policy.get("hosts", []) if kind == "VirtualService" else [policy.get("host", "")]
                if any(
                    fnmatchcase(host, candidate if "." in candidate or "*" in candidate else f"{candidate}.{namespace}.svc.cluster.local")
                    for candidate in hosts
                ):
                    raise ValueError("traffic host already has an administrator or another graph's Istio policy")

        # The Service is intentionally supplied by the application; routing does not create DNS or endpoints.
        service = await controller.api.get("Service", namespace, route.service)
        if service is None or service["metadata"].get("deletionTimestamp"):
            raise Pending("waiting for the traffic route's Service")
        spec = service.get("spec", {})
        if spec.get("type") == "ExternalName" or not spec.get("selector"):
            raise ValueError("traffic routing requires a local Service with a Pod selector")
        ports = [port for port in spec.get("ports", []) if port.get("port") == route.port and port.get("protocol", "TCP") == "TCP"]
        if not ports or not any(
            port.get("appProtocol") in {"http", "http2", "grpc", "kubernetes.io/h2c"}
            or port.get("name", "").split("-", 1)[0] in {"http", "http2", "grpc"}
            for port in ports
        ):
            raise ValueError("traffic routing requires an explicitly identified HTTP, HTTP/2 or gRPC Service port")
        selectors = {
            destination.target: await target_selector(controller, obj, destination.target, allow_absent=destination.weight == 0)
            for destination in route.destinations
        }
        for kind, spec in route_specs(namespace, obj["kind"], meta["name"], route, selectors).items():
            desired = asts.to_document(controller.child(obj, f"traffic-{route.name}", kind, spec))
            name = desired["metadata"]["name"]
            wanted.add((kind, name))
            current = await controller.api.get(kind, namespace, name)
            if current:
                if current["metadata"].get("ownerReferences") != desired["metadata"].get("ownerReferences"):
                    raise ValueError("refusing to adopt a traffic policy owned by another controller")
                if current["metadata"].get("deletionTimestamp"):
                    raise Pending("waiting for deleted traffic policies to disappear")
                if current.get("spec") == spec:
                    continue
                annotations = desired["metadata"].get("annotations", {})
                desired = copy.deepcopy(current)
                desired["spec"] = spec
                desired["metadata"].setdefault("annotations", {}).update(annotations)
            await refresh()
            for destination in route.destinations:
                if (
                    await target_selector(controller, obj, destination.target, allow_absent=destination.weight == 0)
                    != selectors[destination.target]
                ):
                    raise Pending("traffic destination identity changed before dispatch")
            await controller.api.request("PUT" if current else "POST", kind, namespace, name if current else "", desired)
            decision(
                "polyad.traffic.configured",
                "Configured an approved Istio traffic split; proxy propagation remains asynchronous.",
                obj=obj,
                outcome="applied",
                reason="routing_constraints_passed",
                attributes={"polyad.traffic.route": route.name, "polyad.traffic.kind": kind},
            )
            changed = True
    if changed:
        raise Pending("traffic policies persisted; refresh before workload admission")

    # Remove the forwarding rule before its subsets when a route is removed.
    children = await controller.api.owned(namespace, meta["uid"])
    for kind in ("VirtualService", "DestinationRule"):
        for child in children:
            if child["kind"] == kind and (kind, child["metadata"]["name"]) not in wanted:
                await refresh()
                if not child["metadata"].get("deletionTimestamp"):
                    await controller.api.delete(child)
                raise Pending("waiting for obsolete traffic policies to disappear")
