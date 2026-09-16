"""
Intersect inherited traffic rules and compile pod-specific network and mesh policies.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, cast

from attrs import frozen

from polyad_types.network import NetworkAccess, NetworkPeer, NetworkPort, TrafficRule
from polyad_types.resources import GROUP

if TYPE_CHECKING:
    from typing import Any


@frozen
class NetworkScope:
    """
    Resolve one ancestor's network policy against the descendant branch being compiled.

    Attributes:
        namespace (str): Namespace owning the scope.
        kind (str): Graph kind declaring the scope.
        name (str): Persisted graph instance name.
        branch (str): Local node whose subtree contains the destination workload.
        access (NetworkAccess): Traffic restrictions at this scope.
        connections (tuple[tuple[str, str, tuple[NetworkPort, ...]], ...]): Transport edges declared by this boundary.
        expires_at (datetime | None): Earliest temporary grant deadline in this compiled scope.
    """

    namespace: str
    kind: str
    name: str
    branch: str
    access: NetworkAccess
    connections: tuple[tuple[str, str, tuple[NetworkPort, ...]], ...] = ()
    expires_at: datetime | None = None


def scope_label(namespace: str, kind: str, name: str, node: str | None = None) -> str:
    """
    Derive a label key selecting a graph instance or one of its node subtrees.

    Args:
        namespace (str): Namespace containing the graph instance.
        kind (str): Graph resource kind.
        name (str): Graph instance name.
        node (str | None): Optional node subtree within this boundary.

    Returns:
        str: Stable label key whose value is true on selected descendant pods.
    """
    digest = hashlib.sha256(json.dumps([namespace, kind, name, node]).encode()).hexdigest()[:32]
    return f"{GROUP}/network-scope-{digest}"


def _term(scope: NetworkScope, rule: TrafficRule) -> dict[str, Any]:
    peer = rule.peer
    namespace = peer.namespace or scope.namespace
    labels = dict(peer.podLabels)
    if peer.graph or peer.node:
        labels[scope_label(namespace, peer.kind if peer.graph else scope.kind, peer.graph or scope.name, peer.node)] = "true"
    return {
        "namespace": namespace,
        "labels": labels,
        "ports": [(port.protocol, port.port) for port in rule.ports],
        "principals": list(rule.principals),
        "methods": list(rule.methods),
        "paths": list(rule.paths),
    }


def _terms(scope: NetworkScope, direction: str) -> list[dict[str, Any]]:
    access = scope.access
    result = [_term(scope, rule) for rule in getattr(access, direction) if rule.node is None or rule.node == scope.branch]
    if access.allowWithin:
        result.append(_term(scope, TrafficRule(peer=NetworkPeer(graph=scope.name, kind=cast("Any", scope.kind)))))
    if direction == "egress" and access.allowDNS:
        result.append(
            _term(
                scope,
                TrafficRule(
                    peer=NetworkPeer(namespace="kube-system", podLabels={"k8s-app": "kube-dns"}),
                    ports=(NetworkPort(53), NetworkPort(53, "UDP")),
                ),
            )
        )
    for source, target, ports in scope.connections:
        if not ports:
            continue
        if (direction == "egress" and source == scope.branch) or (direction == "ingress" and target == scope.branch):
            result.append(_term(scope, TrafficRule(peer=NetworkPeer(node=target if direction == "egress" else source), ports=ports)))
    return result


def _intersect(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
    if left["namespace"] != right["namespace"]:
        return None
    labels = {**left["labels"], **right["labels"]}
    if any(left["labels"][key] != right["labels"][key] for key in left["labels"].keys() & right["labels"].keys()):
        return None
    result: dict[str, Any] = {"namespace": left["namespace"], "labels": labels}
    for key in ("ports", "principals", "methods", "paths"):
        a, b = left[key], right[key]
        common = [value for value in a if value in b] if a and b else a or b
        if a and b and not common:
            return None
        result[key] = common
    return result


def traffic(scopes: list[NetworkScope], direction: str) -> list[dict[str, Any]] | None:
    """
    Intersect allowances across isolated scopes instead of unioning parent policies.

    Args:
        scopes (list[NetworkScope]): Ancestors and current-boundary restrictions for one pod.
        direction (str): Ingress or egress.

    Returns:
        list[dict[str, Any]] | None: Effective terms, empty for deny-all, or None for no isolation.
    """
    result = None
    for scope in scopes:
        if not getattr(scope.access, "isolate" + direction.title()):
            continue
        incoming = _terms(scope, direction)
        if result is None:
            result = incoming
        else:
            if len(result) * len(incoming) > 1024:
                raise ValueError("network intersection exceeds 1024 candidate terms")
            result = [term for left in result for right in incoming if (term := _intersect(left, right)) is not None]
        unique = {json.dumps(term, sort_keys=True): term for term in result}
        result = [unique[key] for key in sorted(unique)]
        if len(result) > 256:
            raise ValueError("network intersection exceeds 256 effective terms")
    return result


def policy_specs(
    selector: dict[str, str], scopes: list[NetworkScope], *, mesh_namespace: str = "istio-system"
) -> dict[str, dict[str, Any]]:
    """
    Emit NetworkPolicy and optional Istio resources for one workload's pods.

    Args:
        selector (dict[str, str]): Exact operator-assigned pod identity labels.
        scopes (list[NetworkScope]): Effective ancestor constraints for this node.
        mesh_namespace (str): Namespace containing the Istio control plane.

    Returns:
        dict[str, dict[str, Any]]: Native policy specs keyed by resource kind.
    """
    result: dict[str, dict[str, Any]] = {}
    network: dict[str, Any] = {"podSelector": {"matchLabels": selector}, "policyTypes": []}
    ingress = traffic(scopes, "ingress")
    for direction, terms in (("ingress", ingress), ("egress", traffic(scopes, "egress"))):
        if terms is None:
            continue
        network["policyTypes"].append(direction.title())
        network[direction] = []
        for term in terms:
            rule: dict[str, Any] = {
                "from" if direction == "ingress" else "to": [
                    {
                        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": term["namespace"]}},
                        "podSelector": {"matchLabels": term["labels"]},
                    }
                ],
            }
            if term["ports"]:
                rule["ports"] = [{"protocol": protocol, "port": port} for protocol, port in term["ports"]]
            network[direction].append(rule)
    if network["policyTypes"]:
        result["NetworkPolicy"] = network
    if any(scope.access.mesh for scope in scopes):
        if "egress" in network:
            network["egress"].append(
                {
                    "to": [
                        {
                            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": mesh_namespace}},
                            "podSelector": {"matchLabels": {"app": "istiod"}},
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 15012}],
                }
            )
        rules = []
        for term in ingress or []:
            source = {"namespaces": [term["namespace"]]}
            if term["principals"]:
                source["principals"] = term["principals"]
            operation = {key: term[key] for key in ("methods", "paths") if term[key]}
            if term["ports"]:
                operation["ports"] = [str(port) for protocol, port in term["ports"] if protocol == "TCP"]
                if not operation["ports"]:
                    continue
            rules.append({"from": [{"source": source}], **({"to": [{"operation": operation}]} if operation else {})})
        result["PeerAuthentication"] = {"selector": {"matchLabels": selector}, "mtls": {"mode": "STRICT"}}
        result["AuthorizationPolicy"] = {"selector": {"matchLabels": selector}, "action": "ALLOW", "rules": rules}
    return result


def configure_pod(pod: dict[str, Any], labels: dict[str, str], *, isolated: bool, mesh: bool) -> None:
    """
    Apply unspoofable compiler labels and require mesh interception for isolated pods.

    Args:
        pod (dict[str, Any]): Pod template being compiled.
        labels (dict[str, str]): Operator-assigned graph and node membership labels.
        isolated (bool): Whether graph networking restricts this workload.
        mesh (bool): Whether this workload requires an Istio proxy.

    Returns:
        None: The pod template is modified in place.
    """
    metadata = pod.setdefault("metadata", {})
    current = metadata.setdefault("labels", {})
    for key in list(current):
        if key.startswith(f"{GROUP}/network-"):
            del current[key]
    current.update(labels)
    if isolated:
        spec = pod["spec"]
        if any(spec.get(key) for key in ("hostNetwork", "hostPID", "hostIPC")):
            raise ValueError("host namespaces bypass graph network isolation")
        if any("hostPath" in volume for volume in spec.get("volumes", [])):
            raise ValueError("hostPath volumes are incompatible with graph network isolation")
        for container in (*spec.get("containers", []), *spec.get("initContainers", [])):
            security = container.get("securityContext", {})
            if security.get("privileged") or set(security.get("capabilities", {}).get("add", [])) - {"NET_BIND_SERVICE"}:
                raise ValueError("privileged workloads and elevated capabilities are incompatible with graph network isolation")
    if mesh:
        for container in (*pod["spec"].get("containers", []), *pod["spec"].get("initContainers", [])):
            security = container.setdefault("securityContext", {})
            if security.get("runAsUser", pod["spec"].get("securityContext", {}).get("runAsUser")) in {0, 1337}:
                raise ValueError("mesh workloads must use a non-root UID distinct from Istio's reserved UID 1337")
            security.update(runAsNonRoot=True, allowPrivilegeEscalation=False)
            security.setdefault("capabilities", {})["drop"] = ["ALL"]
        annotations = metadata.setdefault("annotations", {})
        if any(key.startswith(("traffic.sidecar.istio.io/", "sidecar.istio.io/", "proxy.istio.io/")) for key in annotations):
            raise ValueError("mesh interception overrides are invalid inside a network-isolated graph")
        annotations["sidecar.istio.io/inject"] = "true"
        annotations["sidecar.istio.io/nativeSidecar"] = "true"
        config = json.loads(annotations.get("proxy.istio.io/config", "{}"))
        config["holdApplicationUntilProxyStarts"] = True
        annotations["proxy.istio.io/config"] = json.dumps(config, sort_keys=True)
        current["sidecar.istio.io/inject"] = "true"
