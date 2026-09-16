"""
Refresh structural policies and validate a complete graph family before admission.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from polyad.graph.rules import evaluate_rule
from polyad_types.codec import converter
from polyad_types.resources import BOUNDARY_KINDS
from polyad_types.rules import StructuralRule
from polyad_types.topology import topology

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.api import API


class RuleViolation(ValueError):
    """
    Reject a graph family that violates an engineer-defined structural rule.
    """


async def check_rules(
    api: API,
    namespace: str,
    kind: str,
    spec: dict[str, Any],
    *,
    definitions: dict[tuple[str, str], dict[str, Any]] | None = None,
    rule_documents: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Apply mandatory and inherited rules to every referenced boundary using refreshed definitions.

    Args:
        api (API): Kubernetes read adapter used inside the current leased reconciliation.
        namespace (str): Namespace containing graph definitions and structural rules.
        kind (str): Root boundary kind.
        spec (dict[str, Any]): Root desired specification.
        definitions (dict[tuple[str, str], dict[str, Any]] | None): Not-yet-created composition definitions for preflight.
        rule_documents (list[dict[str, Any]] | None): Fresh rule snapshot when the caller also verifies revisions.
        observations (list[dict[str, Any]] | None): Optional collector for verdicts at every visited boundary.

    Returns:
        list[dict[str, Any]]: Root rule verdicts with persisted rule identities and measurements.
    """
    if rule_documents is None:
        rule_documents = (await api.request("GET", "GraphRule", namespace)).get("items", [])
    documents = {item["metadata"]["name"]: item for item in rule_documents}
    if len(documents) > 32:
        raise RuleViolation("a namespace supports at most 32 GraphRules")
    rules = {name: converter.structure(item["spec"], StructuralRule) for name, item in documents.items()}
    mandatory = {name for name, rule in rules.items() if rule.enforcement == "Namespace"}
    cache = dict(definitions or {})
    visited_nodes = 0
    boundaries = 0

    async def visit(
        boundary_kind: str, body: dict[str, Any], path: tuple[tuple[str, str], ...], inherited: set[str], cluster_local: bool = False
    ) -> tuple[int, int, list[dict[str, Any]]]:
        nonlocal visited_nodes, boundaries
        boundaries += 1
        if len(path) >= 32 or boundaries > 256:
            raise RuleViolation("graph expansion exceeds 32 nesting levels or 256 boundaries")
        graph = topology(body, boundary_kind)
        cluster_local |= boundary_kind == "Graph"
        selected = mandatory | inherited | set(graph.rules)
        missing = selected - rules.keys()
        if missing:
            raise RuleViolation(f"referenced GraphRule is unavailable: {', '.join(sorted(missing))}")
        if any(documents[name]["metadata"].get("deletionTimestamp") for name in selected):
            raise RuleViolation("a selected GraphRule is being deleted")
        visited_nodes += len(graph.nodes)
        if visited_nodes > 4096:
            raise RuleViolation("expanded graph family exceeds 4096 node occurrences")
        expanded, depth = len(graph.nodes), 1
        for node in graph.nodes:
            if node.kind not in BOUNDARY_KINDS:
                continue
            if getattr(node, "cluster", None):
                if cluster_local:
                    raise RuleViolation("Graph descendants must stay in one cluster; place cross-cluster compositions in a PolyGraph")
                # The remote boundary is a vertex here. Its own operator enforces
                # destination namespace rules against its live local family.
                continue
            key = node.kind, node.ref
            if key in path:
                raise RuleViolation("recursive graph definition references are invalid")
            if key not in cache:
                definition = await api.get(node.kind, namespace, node.ref)
                if definition is None or definition["metadata"].get("deletionTimestamp"):
                    from polyad.operator.controller import Pending

                    raise Pending(f"waiting for graph definition: {node.kind}/{node.ref}")
                cache[key] = definition
            count, levels, _ = await visit(
                node.kind, cache[key]["spec"], (*path, key), {name for name in selected if rules[name].scope == "Subtree"}, cluster_local
            )
            expanded += count
            depth = max(depth, levels + 1)
        reports = []
        for name in sorted(selected):
            report = await asyncio.to_thread(evaluate_rule, rules[name], graph, expanded_nodes=expanded, nesting_depth=depth)
            meta = documents[name]["metadata"]
            # Full spectra remain available through the Python API; status retains compact summaries.
            if report["spectrum"] is not None:
                report["spectrum"] = {key: value for key, value in report["spectrum"].items() if key not in {"adjacency", "laplacian"}}
            reports.append({"name": name, "uid": meta["uid"], "generation": meta.get("generation", 1), **report})
            if observations is not None:
                observations.append({**reports[-1], "path": path})
            if not report["allowed"]:
                location = "/".join(name for _, name in path) or "root"
                raise RuleViolation(f"GraphRule/{name} at {location}: {'; '.join(report['violations'])}")
        return expanded, depth, reports

    return (await visit(kind, spec, (), set()))[2]
