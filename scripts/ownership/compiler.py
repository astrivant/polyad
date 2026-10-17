"""
Normalize literal CODEOWNERS rules without changing ordered ownership policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["Compilation", "compile_owners"]

_LITERAL = re.compile(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+/?")
_OWNER = re.compile(r"(?:@@?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*|[^\s@#]+@[^\s@#]+)")


@dataclass(frozen=True)
class Rule:
    """
    Retain an anchored literal rule and its exact source line.

    Attributes:
        pattern (str): Root-relative literal file or directory pattern.
        owners (tuple[str, ...]): Ordered owner tokens, without interpreting host eligibility.
        source (str): Original line including its terminator.
    """

    pattern: str
    owners: tuple[str, ...]
    source: str

    def covers(self, path: str) -> bool:
        """
        Match one repository-relative path under this literal scope.

        Args:
            path (str): Relative path without a leading slash.

        Returns:
            bool: Whether the literal scope includes the path.
        """
        base = self.pattern.strip("/")
        return path.startswith(base + "/") or (not self.pattern.endswith("/") and path == base)

    def contains(self, other: Rule) -> bool:
        """
        Establish containment for every path, including future additions.

        Args:
            other (Rule): Candidate narrower ownership scope.

        Returns:
            bool: Whether this rule covers every path the other could match.
        """
        base, child = self.pattern.strip("/"), other.pattern.strip("/")
        return child.startswith(base + "/") or (base == child and (not self.pattern.endswith("/") or other.pattern.endswith("/")))


@dataclass(frozen=True)
class Compilation:
    """
    Return normalized text and a visible account of untouched syntax.

    Attributes:
        text (str): Candidate ownership document.
        barriers (tuple[int, ...]): One-based non-comment lines retained as opaque boundaries.
        consolidated (tuple[str, ...]): New directory scopes requiring explicit review.
    """

    text: str
    barriers: tuple[int, ...]
    consolidated: tuple[str, ...]


def _parse(line: str, paths: frozenset[str]) -> Rule | None:
    """
    Accept only existing, anchored literal scopes with explicit plain owners.

    Args:
        line (str): Original source line.
        paths (frozenset[str]): Tracked repository paths supplied by the caller.

    Returns:
        Rule | None: Transformable rule or an opaque boundary.
    """
    fields = line.split()
    if len(fields) < 2 or not _LITERAL.fullmatch(fields[0]):
        return None
    if any(part in {".", ".."} for part in fields[0].strip("/").split("/")):
        return None
    if not all(_OWNER.fullmatch(owner) for owner in fields[1:]):
        return None
    rule = Rule(fields[0], tuple(fields[1:]), line)
    return rule if any(rule.covers(path) for path in paths) else None


def _overlap(left: Rule, right: Rule) -> bool:
    """
    Conservatively identify scopes whose order could affect ownership.

    Args:
        left (Rule): First scope.
        right (Rule): Second scope.

    Returns:
        bool: Whether either literal scope contains the other.
    """
    return left.contains(right) or right.contains(left)


def _simplify(rules: Sequence[Rule]) -> list[Rule]:
    """
    Remove shadowed rules and inherited duplicates without broadening ownership.

    Args:
        rules (Sequence[Rule]): One contiguous run, in its original evaluation order.

    Returns:
        list[Rule]: Equivalent scopes for present and future paths.
    """
    kept: list[Rule] = []
    for index, rule in enumerate(rules):
        # A later covering rule wins on every path this rule could affect.
        if any(later.contains(rule) for later in rules[index + 1 :]):
            continue
        previous = next((earlier for earlier in reversed(kept) if _overlap(earlier, rule)), None)
        if previous is not None and previous.contains(rule) and previous.owners == rule.owners:
            continue
        kept.append(rule)
    return kept


def _ordered(rules: Sequence[Rule]) -> list[Rule]:
    """
    Group directory branches while retaining precedence for overlapping scopes.

    Args:
        rules (Sequence[Rule]): Simplified literal run.

    Returns:
        list[Rule]: Deterministic directory-tree order subject to precedence constraints.
    """
    graph: nx.DiGraph[int, dict[str, object], dict[str, object]] = nx.DiGraph()
    graph.add_nodes_from(range(len(rules)))
    # Preserve policy dependencies; sorting strings alone could move an override before its parent.
    for index, rule in enumerate(rules):
        for later in range(index + 1, len(rules)):
            if _overlap(rule, rules[later]):
                graph.add_edge(index, later)
    order = nx.lexicographical_topological_sort(graph, key=lambda index: tuple(rules[index].pattern.strip("/").split("/")))
    return [rules[index] for index in order]


def _fold(rules: Sequence[Rule], paths: frozenset[str], newline: str) -> tuple[list[Rule], list[str]]:
    """
    Optionally combine complete tracked subtrees under a reviewed directory scope.

    Args:
        rules (Sequence[Rule]): Literal scopes in one boundary-delimited run.
        paths (frozenset[str]): Complete tracked repository path inventory.
        newline (str): Original document line-ending convention.

    Returns:
        tuple[list[Rule], list[str]]: Rules and the new future-facing directory scopes.
    """
    result = list(rules)
    folded: list[str] = []
    directories = {path.rsplit("/", 1)[0] for path in paths if "/" in path}
    directories |= {
        "/".join(directory.split("/")[:depth]) for directory in tuple(directories) for depth in range(1, len(directory.split("/")))
    }
    for directory in sorted(directories, key=lambda value: (-value.count("/"), value)):
        prefix = directory + "/"
        selected = [rule for rule in result if rule.pattern.lstrip("/").startswith(prefix)]
        if len(selected) < 2 or any(rule.owners != selected[0].owners for rule in selected):
            continue
        members = [path for path in paths if path.startswith(prefix)]
        if not members or any(not any(rule.covers(path) for rule in selected) for path in members):
            continue
        # All intersecting rules in this run must be selected: an intervening parent may override them.
        replacement = Rule("/" + prefix, selected[0].owners, "/" + prefix + " " + " ".join(selected[0].owners) + newline)
        if any(_overlap(rule, replacement) and rule not in selected for rule in result):
            continue
        first = result.index(selected[0])
        result = [rule for rule in result if rule not in selected]
        result.insert(first, replacement)
        folded.append(replacement.pattern)
    return result, folded


def compile_owners(source: str, paths: frozenset[str], *, consolidate: bool = False) -> Compilation:
    """
    Parse, simplify, order and emit without executing repository-owned content.

    Args:
        source (str): Complete CODEOWNERS text; comments and opaque syntax stay in place.
        paths (frozenset[str]): Tracked file paths, including symlinks as entries, never followed.
        consolidate (bool): Explicitly allow new directory scopes covering future files too.

    Returns:
        Compilation: Candidate text, untouched boundary lines, and new directory scopes.
    """
    output: list[str] = []
    run: list[Rule] = []
    barriers: list[int] = []
    folded: list[str] = []
    newline = "\r\n" if "\r\n" in source else "\n"

    def flush() -> None:
        """
        Emit one independently transformable run without crossing an opaque boundary.

        Returns:
            None: Output buffers receive the normalized run.
        """
        rules = _simplify(run)
        if consolidate:
            rules, changes = _fold(rules, paths, newline)
            folded.extend(changes)
        ordered = _ordered(rules)
        output.extend(rule.source.rstrip("\r\n") + newline for rule in ordered)
        run.clear()

    for number, line in enumerate(source.splitlines(keepends=True), 1):
        rule = _parse(line, paths)
        if rule is not None:
            run.append(rule)
        else:
            flush()
            output.append(line)
            if line.strip() and not line.lstrip().startswith("#"):
                barriers.append(number)
    flush()
    text = "".join(output)
    if source and not source.endswith(("\n", "\r")):
        text = text.rstrip("\r\n")
    return Compilation(text, tuple(barriers), tuple(folded))
