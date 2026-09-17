"""
Recompute structural constraints from live graph families before scaling mutations.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from polyad.compiler.passes.traffic import subset_name
from polyad.graph.temporary import ANNOTATION, active_entries, overlay
from polyad.operator.clusters.remote_scaling import approved_intent
from polyad.operator.policies.rules import RuleViolation, check_rules
from polyad_types.codec import converter
from polyad_types.replication import Replication, replica_topology
from polyad_types.resources import AUXILIARY_KINDS, BOUNDARY_KINDS, GROUP, VERSION
from polyad_types.topology import topology

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API


def _revision(obj: dict[str, Any]) -> dict[str, Any]:
    """
    Select the identity and intent that affect structural computations.

    Args:
        obj (dict[str, Any]): Observed Kubernetes document.

    Returns:
        dict[str, Any]: Comparable intent, excluding unrelated status updates.
    """
    meta = obj["metadata"]
    return {
        "spec": obj.get("spec"),
        "temporaryConnections": meta.get("annotations", {}).get(ANNOTATION),
        "remoteScaleIntent": meta.get("annotations", {}).get(f"{GROUP}/remote-scale-intent"),
        "membership": {key: value for key, value in meta.get("labels", {}).items() if key in {f"{GROUP}/node", f"{GROUP}/runtime-node"}},
        **{key: meta.get(key) for key in ("uid", "generation", "deletionTimestamp", "ownerReferences")},
    }


def _activation_topology(raw: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Account for each live pulse instance while preserving logical dependency edges.

    Args:
        raw (dict[str, Any]): Logical topology specification.
        children (list[dict[str, Any]]): Live resources, including retiring pulse instances.

    Returns:
        dict[str, Any]: Topology with one vertex per observed activation instance.
    """
    aliases = {}
    for node in raw.get("nodes", []):
        runtime = sorted(
            {
                child["metadata"]["labels"][f"{GROUP}/runtime-node"]
                for child in children
                if child["metadata"].get("labels", {}).get(f"{GROUP}/node") == node["name"]
                and child["metadata"].get("labels", {}).get(f"{GROUP}/runtime-node")
            }
        )
        aliases[node["name"]] = runtime or [node["name"]]
    if (
        sum(map(len, aliases.values())) > 4096
        or sum(len(aliases[edge["source"]]) * len(aliases[edge["target"]]) for edge in raw.get("connections", [])) > 16384
    ):
        raise RuleViolation("expanded activation topology exceeds 4096 vertices or 16384 connections")
    return {
        **raw,
        # Traffic selectors retain logical node names; only the structural
        # projection expands runtime aliases, as in Activations.prepare.
        "network": None,
        "throughput": None,
        "traffic": [],
        "nodes": [
            {
                **node,
                "name": name,
                "requires": [{**edge, "node": dependency} for edge in node.get("requires", []) for dependency in aliases[edge["node"]]],
            }
            for node in raw.get("nodes", [])
            for name in aliases[node["name"]]
        ],
        "connections": [
            {**edge, "source": source, "target": target}
            for edge in raw.get("connections", [])
            for source in aliases[edge["source"]]
            for target in aliases[edge["target"]]
        ],
    }


async def check_live_rules(
    api: API, obj: dict[str, Any], *, candidate: dict[str, Any] | None = None, candidate_is_logical: bool = False
) -> list[dict[str, Any]]:
    """
    Validate a proposed boundary against fresh ancestors, sibling instances and rules.

    Args:
        api (API): Read adapter under the existing root-family lease.
        obj (dict[str, Any]): Reconciled boundary, with effective topology for a ReplicaGroup.
        candidate (dict[str, Any] | None): Locally planned activation topology to evaluate before dispatch.
        candidate_is_logical (bool): Expand current activation instances when the candidate replaces logical connections.

    Returns:
        list[dict[str, Any]]: Current-boundary verdicts after every family constraint passes.
    """
    from polyad.operator.reconciliation.controller import Pending

    namespace = obj["metadata"]["namespace"]
    target_uid = obj["metadata"]["uid"]
    documents: dict[tuple[str, str], dict[str, Any]] = {}
    owned: dict[str, list[dict[str, Any]]] = {}
    definitions: dict[tuple[str, str], dict[str, Any]] = {}
    identities: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    boundaries = vertices = 0
    target_path: tuple[tuple[str, str], ...] | None = None
    deadlines: list[datetime] = []

    async def read(kind: str, name: str) -> dict[str, Any]:
        """
        Read one input once per evaluation and retain it for revision fencing.

        Args:
            kind (str): Resource kind.
            name (str): Namespaced resource name.

        Returns:
            dict[str, Any]: Live input document.
        """
        key = kind, name
        if key not in documents:
            current = await api.get(kind, namespace, name)
            if current is None or current["metadata"].get("deletionTimestamp"):
                raise Pending(f"waiting for live rule input: {kind}/{name}")
            documents[key] = current
        return documents[key]

    async def children(instance: dict[str, Any] | None) -> list[dict[str, Any]]:
        """
        Retain live children, including terminating resources, for one boundary.

        Args:
            instance (dict[str, Any] | None): Persisted boundary, or an uninstantiated template.

        Returns:
            list[dict[str, Any]]: Owned execution and boundary resources.
        """
        if instance is None:
            return []
        uid = instance["metadata"]["uid"]
        if uid not in owned:
            owned[uid] = [child for child in await api.owned(namespace, uid) if child["kind"] not in AUXILIARY_KINDS]
        return owned[uid]

    current = await read(obj["kind"], obj["metadata"]["name"])
    if current["metadata"]["uid"] != target_uid or current["metadata"].get("generation") != obj["metadata"].get("generation"):
        raise Pending("graph changed before structural rule evaluation")
    if _revision(current)["temporaryConnections"] != _revision(obj)["temporaryConnections"]:
        raise Pending("temporary connections changed before structural rule evaluation")
    if _revision(current)["remoteScaleIntent"] != _revision(obj)["remoteScaleIntent"]:
        raise Pending("remote scale intent changed before structural rule evaluation")
    root = current
    ancestors = {target_uid}
    while True:
        owners = [
            owner
            for owner in root["metadata"].get("ownerReferences", [])
            if owner.get("controller") and owner.get("apiVersion") == f"{GROUP}/{VERSION}" and owner.get("kind") in BOUNDARY_KINDS
        ]
        if not owners:
            break
        owner = owners[0]
        root = await read(owner["kind"], owner["name"])
        if root["metadata"]["uid"] != owner["uid"]:
            raise Pending("graph ancestor incarnation changed")
        if owner["uid"] in ancestors or len(ancestors) >= 32:
            raise RuleViolation("cyclic graph ownership or more than 32 nesting levels")
        ancestors.add(owner["uid"])

    async def expand(
        kind: str,
        body: dict[str, Any],
        instance: dict[str, Any] | None,
        path: tuple[tuple[str, str], ...],
        references: tuple[tuple[str, str], ...],
    ) -> dict[str, Any]:
        """
        Substitute effective instance topologies into a bounded family snapshot.

        Args:
            kind (str): Boundary kind.
            body (dict[str, Any]): Current instance or template specification.
            instance (dict[str, Any] | None): Persisted instance where present.
            path (tuple[tuple[str, str], ...]): Unique occurrence path for report attribution.
            references (tuple[tuple[str, str], ...]): Definition references for recursion detection.

        Returns:
            dict[str, Any]: Independent specification referring to expanded snapshot definitions.
        """
        nonlocal boundaries, vertices, target_path
        boundaries += 1
        if boundaries > 256 or len(path) >= 32:
            raise RuleViolation("graph expansion exceeds 32 nesting levels or 256 boundaries")
        identity = instance["metadata"] if instance else {}
        identities[path] = {"kind": kind, "name": identity.get("name"), "uid": identity.get("uid"), "path": [name for _, name in path]}
        body = copy.deepcopy(body)
        live_children = await children(instance)
        is_target = identity.get("uid") == target_uid
        if kind == "ReplicaGroup":
            policy = converter.structure(body, Replication)
            intent = approved_intent(instance) if instance else None
            if intent:
                body["replicas"] = intent["replicas"]
            if policy.replicaSource and policy.inheritReplicas:
                source = await read("ReplicaGroup", policy.replicaSource.name)
                if source["metadata"]["uid"] != policy.replicaSource.uid or not source["spec"].get("templateOnly"):
                    raise Pending("replica source incarnation is unavailable")
                source_intent = approved_intent(source)
                count = source_intent["replicas"] if source_intent else converter.structure(source["spec"], Replication).replicas
                if not policy.minReplicas <= count <= policy.maxReplicas:
                    raise RuleViolation("inherited replicas exceed this instance's bounds")
                body["replicas"] = count
            projected = replica_topology(body)
            if is_target and projected != obj["spec"]:
                raise Pending("replica intent changed before structural rule evaluation")
            # Sibling scale-in has not released capacity until deletion is observed.
            # Keep retiring ordinals in the sibling projection while reserving all
            # requested scale-out ordinals, including a shared source's other uses.
            if not is_target:
                retained = {ordinal for child in live_children if (ordinal := child["metadata"].get("labels", {}).get(f"{GROUP}/node"))}
                projected = replica_topology(body, retained=retained)
            body = projected
        elif is_target and body != obj["spec"]:
            raise Pending("graph intent changed before structural rule evaluation")
        if is_target:
            target_path = path
            if candidate is not None:
                body = copy.deepcopy(candidate)
        if instance is not None:
            deadlines.extend(datetime.fromisoformat(grant["expiresAt"]) for grant in active_entries(instance).values())
            body = overlay(instance, body)
        traffic_routes = topology(body, kind).traffic
        if not is_target or candidate is None or candidate_is_logical:
            body = _activation_topology(body, live_children)
        graph = topology(body, kind)
        vertices += len(graph.nodes)
        if vertices > 4096:
            raise RuleViolation("expanded graph family exceeds 4096 node occurrences")
        for node in body.get("nodes", []):
            if node["kind"] not in BOUNDARY_KINDS:
                continue
            if node.get("cluster"):
                continue  # Remote execution belongs to its destination's independent rule family.
            reference = node["kind"], node["ref"]
            if reference in references:
                raise RuleViolation("recursive graph definition references are invalid")
            matches = [
                child
                for child in live_children
                if child["kind"] in BOUNDARY_KINDS
                and child["metadata"]
                .get("labels", {})
                .get(f"{GROUP}/runtime-node", child["metadata"].get("labels", {}).get(f"{GROUP}/node"))
                == node["name"]
            ]
            if len(matches) > 1:
                raise Pending("multiple live boundary instances require a refreshed topology")
            selected_child = matches[0] if matches else None
            if selected_child is not None:
                documents[(selected_child["kind"], selected_child["metadata"]["name"])] = selected_child
                child_body = selected_child["spec"]
                child_kind = selected_child["kind"]
            else:
                definition = await read(*reference)
                child_body, child_kind = definition["spec"], node["kind"]
            key = child_kind, selected_child["metadata"]["name"] if selected_child else f"{node['name']}-{boundaries}"
            definitions[key] = {"spec": await expand(child_kind, child_body, selected_child, (*path, key), (*references, reference))}
            node["kind"], node["ref"] = key
        for route in traffic_routes:
            for destination in route.destinations:
                target_body = body
                parts = destination.target.split("/")
                for index, part in enumerate(parts):
                    endpoint = next((node for node in target_body["nodes"] if node["name"] == part), None)
                    if endpoint is None and destination.weight == 0:
                        if instance is not None and not is_target:
                            from polyad.operator.reconciliation.controller import child_name

                            persisted = await api.get("VirtualService", namespace, child_name(instance, f"traffic-{route.name}"))
                            if persisted:
                                actual = persisted.get("spec", {}).get("http", [{}])[0].get("route", [])
                                if any(
                                    item.get("destination", {}).get("subset") == subset_name(destination.target)
                                    and item.get("weight", 0) > 0
                                    for item in actual
                                ):
                                    raise Pending("persist zero traffic weight before removing its graph replica")
                        break
                    if endpoint is None or endpoint.get("cluster") or endpoint["kind"] == "Resource":
                        raise RuleViolation(
                            "a positive traffic weight requires a present local downstream execution; drain its weight before scale-in"
                        )
                    if index < len(parts) - 1:
                        if endpoint["kind"] not in BOUNDARY_KINDS:
                            raise RuleViolation("traffic destination paths can traverse only graph boundaries")
                        target_body = definitions[(endpoint["kind"], endpoint["ref"])]["spec"]
        return body

    spec = await expand(root["kind"], root["spec"], root, (), ())
    if target_path is None:
        raise Pending("scaling target is no longer part of its owner's topology")
    rule_documents = (await api.request("GET", "GraphRule", namespace)).get("items", [])
    reports: list[dict[str, Any]] = []
    await check_rules(api, namespace, root["kind"], spec, definitions=definitions, rule_documents=rule_documents, observations=reports)
    # Persist only the reconciling boundary's reports, as before. Repeating all
    # 32 rules at 256 boundaries in every status could exceed Kubernetes object
    # size limits. Ancestor and sibling failures still block the action.
    reports = [report for report in reports if report["path"] == target_path]
    for report in reports:
        report["boundary"] = identities[tuple(tuple(key) for key in report.pop("path"))]
    # Reject inputs that changed during the potentially expensive computations.
    # The root-family lease serializes operator actions; external writers still
    # require fresh revision checks immediately before dispatch.
    for (kind, name), original in documents.items():
        fresh = await api.get(kind, namespace, name)
        if fresh is None or _revision(fresh) != _revision(original):
            raise Pending("graph family changed during structural rule evaluation")
    for uid, original_children in owned.items():
        fresh_children = [child for child in await api.owned(namespace, uid) if child["kind"] not in AUXILIARY_KINDS]
        if {child["metadata"]["uid"]: _revision(child) for child in fresh_children} != {
            child["metadata"]["uid"]: _revision(child) for child in original_children
        }:
            raise Pending("graph children changed during structural rule evaluation")
    fresh_rules = (await api.request("GET", "GraphRule", namespace)).get("items", [])
    if {item["metadata"]["uid"]: _revision(item) for item in fresh_rules} != {
        item["metadata"]["uid"]: _revision(item) for item in rule_documents
    }:
        raise Pending("GraphRules changed during structural rule evaluation")
    if any(value <= datetime.now(UTC) for value in deadlines):
        raise Pending("a temporary connection expired during structural rule evaluation")
    return reports
