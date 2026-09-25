"""
Refresh structural policies and validate a complete graph family before admission.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import TYPE_CHECKING

# Export the policy exception from its central definition.
from polyad.exceptions.policies import PolicyViolation as PolicyViolation
from polyad.exceptions.reconciliation import Pending
from polyad.graph.policies import evaluate_policy
from polyad.operator.observability.decisions import decision
from polyad.operator.policies.cheeger import computation_limits
from polyad_types.graphs.policies import StructuralPolicy
from polyad_types.graphs.topology import topology
from polyad_types.resources import BOUNDARY_KINDS
from polyad_types.serialization import converter

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API

__all__ = (
    "PolicyViolation",
    "check_policies",
)


async def check_policies(
    api: API,
    namespace: str,
    kind: str,
    spec: dict[str, Any],
    *,
    definitions: dict[tuple[str, str], dict[str, Any]] | None = None,
    policy_documents: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
    cache_scope: str = "",
) -> list[dict[str, Any]]:
    """
    Apply mandatory and inherited policies to every referenced boundary using refreshed definitions.

    Args:
        api (API): Kubernetes read adapter used inside the current leased reconciliation.
        namespace (str): Namespace containing graph definitions and structural policies.
        kind (str): Root boundary kind.
        spec (dict[str, Any]): Root desired specification.
        definitions (dict[tuple[str, str], dict[str, Any]] | None): Not-yet-created composition definitions for preflight.
        policy_documents (list[dict[str, Any]] | None): Fresh policy snapshot when the caller also verifies revisions.
        observations (list[dict[str, Any]] | None): Optional collector for verdicts at every visited boundary.
        cache_scope (str): Persisted root UID when available; preflight falls back to a content-isolated identity.

    Returns:
        list[dict[str, Any]]: Root policy verdicts with persisted policy identities and measurements.
    """
    if policy_documents is None:
        policy_documents = (await api.request("GET", "GraphPolicy", namespace)).get("items", [])
    documents = {item["metadata"]["name"]: item for item in policy_documents}
    if len(documents) > 32:
        raise PolicyViolation("a namespace supports at most 32 GraphPolicies")
    policies = {name: converter.structure(item["spec"], StructuralPolicy) for name, item in documents.items()}
    mandatory = {name for name, policy in policies.items() if policy.enforcement == "Namespace"}
    cache = dict(definitions or {})
    visited_nodes = 0
    boundaries = 0
    scope = cache_scope or hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()

    async def visit(
        boundary_kind: str, body: dict[str, Any], path: tuple[tuple[str, str], ...], inherited: set[str], cluster_local: bool = False
    ) -> tuple[int, int, list[dict[str, Any]]]:
        nonlocal visited_nodes, boundaries
        boundaries += 1
        if len(path) >= 32 or boundaries > 256:
            raise PolicyViolation("graph expansion exceeds 32 nesting levels or 256 boundaries")
        graph = topology(body, boundary_kind)
        cluster_local |= boundary_kind == "Graph"
        selected = mandatory | inherited | set(graph.policies)
        missing = selected - policies.keys()
        if missing:
            raise PolicyViolation(f"referenced GraphPolicy is unavailable: {', '.join(sorted(missing))}")
        if any(documents[name]["metadata"].get("deletionTimestamp") for name in selected):
            raise PolicyViolation("a selected GraphPolicy is being deleted")
        visited_nodes += len(graph.nodes)
        if visited_nodes > 4096:
            raise PolicyViolation("expanded graph family exceeds 4096 node occurrences")
        expanded, depth = len(graph.nodes), 1
        for node in graph.nodes:
            if node.kind not in BOUNDARY_KINDS:
                continue
            if getattr(node, "cluster", None):
                if cluster_local:
                    raise PolicyViolation("Graph descendants must stay in one cluster; place cross-cluster compositions in a PolyGraph")

                # The remote boundary is a vertex here. Its own operator enforces
                # destination namespace policies against its live local family.
                continue
            key = node.kind, node.ref
            if key in path:
                raise PolicyViolation("recursive graph definition references are invalid")
            if key not in cache:
                definition = await api.get(node.kind, namespace, node.ref)
                if definition is None or definition["metadata"].get("deletionTimestamp"):
                    raise Pending(f"waiting for graph definition: {node.kind}/{node.ref}")
                cache[key] = definition
            count, levels, _ = await visit(
                node.kind, cache[key]["spec"], (*path, key), {name for name in selected if policies[name].scope == "Subtree"}, cluster_local
            )
            expanded += count
            depth = max(depth, levels + 1)
        reports = []
        for name in sorted(selected):
            report = await asyncio.to_thread(
                evaluate_policy,
                policies[name],
                graph,
                expanded_nodes=expanded,
                nesting_depth=depth,
                cheeger_limits=computation_limits(),
                cache_scope=json.dumps([namespace, kind, scope, path, documents[name]["metadata"]["uid"], policies[name].relation]),
            )
            meta = documents[name]["metadata"]

            # Keep full eigenvalue arrays only when benchmark diagnostics are enabled.
            if report["spectrum"] is not None and os.getenv("POLYAD_METRICS_GRAPH_SPECTRA", "false").lower() != "true":
                report["spectrum"] = {key: value for key, value in report["spectrum"].items() if key not in {"adjacency", "laplacian"}}
            reports.append({"name": name, "uid": meta["uid"], "generation": meta.get("generation", 1), **report})
            if observations is not None:
                observations.append({**reports[-1], "path": path})
            if not report["allowed"]:
                location = "/".join(name for _, name in path) or "root"
                decision(
                    "polyad.policies.rejected",
                    f"GraphPolicy {name} blocks boundary {location}: {'; '.join(report['violations'])}.",
                    obj=documents[name],
                    outcome="blocked",
                    reason="structural_constraint",
                    level=logging.WARNING,
                    attributes={"polyad.boundary.path": location, "polyad.boundary.kind": boundary_kind},
                )
                raise PolicyViolation(f"GraphPolicy/{name} at {location}: {'; '.join(report['violations'])}")
            decision(
                "polyad.policies.allowed",
                f"GraphPolicy {name} permits the measured boundary.",
                obj=documents[name],
                outcome="allowed",
                reason="structural_constraints_passed",
                level=logging.DEBUG,
                attributes={"polyad.boundary.path": "/".join(name for _, name in path) or "root"},
            )
        return expanded, depth, reports

    return (await visit(kind, spec, (), set()))[2]
