"""
Compile declared mutation effects into conservative, explainable execution batches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import networkx as nx

from polyad_types.resources.mutations import Independence, MutationPlan, Ordering

if TYPE_CHECKING:
    from collections.abc import Mapping

    from polyad_types.resources.mutations import Budget, Mutation, Scope


class PreconditionFailed(ValueError):
    """
    Require refreshed state before admitting a mutation with stale observations.
    """


def conflicts(left: Mutation, right: Mutation) -> tuple[str, ...]:
    """
    Explain effect conflicts independently of explicit ordering and budget bounds.

    Args:
        left (Mutation): First operation.
        right (Mutation): Second operation.

    Returns:
        tuple[str, ...]: Stable reasons; empty means declared effects do not conflict.
    """
    reasons = []
    if not left.effects_complete or not right.effects_complete:
        reasons.append("incomplete effect declaration")

    # Preconditions are reads too: another operation cannot invalidate them inside the same batch.
    left_reads = left.reads + tuple(item.scope for item in left.preconditions)
    right_reads = right.reads + tuple(item.scope for item in right.preconditions)
    for label, first, second in (
        ("write/write", left.writes, right.writes),
        ("write/read", left.writes, right_reads),
        ("read/write", left_reads, right.writes),
    ):
        for a in first:
            for b in second:
                if a.overlaps(b):
                    reasons.append(f"{label}: {a.path!r} overlaps {b.path!r}")
    return tuple(dict.fromkeys(reasons))


def check_preconditions(mutation: Mutation, observations: Mapping[Scope, str | None]) -> None:
    """
    Fail closed when a required observation is missing or stale.

    Args:
        mutation (Mutation): Operation to admit.
        observations (Mapping[Scope, str | None]): Refreshed values; absence must be explicit.

    Returns:
        None: No return value.
    """
    for condition in mutation.preconditions:
        if condition.scope not in observations or observations[condition.scope] != condition.expected:
            raise PreconditionFailed(f"mutation {mutation.name!r} precondition failed at {condition.scope.path!r}")


def advance_budgets(batch: tuple[Mutation, ...], budgets: tuple[Budget, ...], usage: Mapping[str, int]) -> dict[str, int]:
    """
    Check every possible completion order without borrowing uncompleted releases.

    Args:
        batch (tuple[Mutation, ...]): Operations proposed for concurrent dispatch.
        budgets (tuple[Budget, ...]): Inclusive shared bounds.
        usage (Mapping[str, int]): Counter values before the batch.

    Returns:
        dict[str, int]: Projected values after all operations complete.
    """
    result = dict(usage)
    for budget in budgets:
        changes = [delta.amount for mutation in batch for delta in mutation.deltas if delta.budget == budget.name]
        current = usage[budget.name]

        # Check both extreme completion orders; releases cannot fund allocations still in flight.
        low = current + sum(min(0, change) for change in changes)
        high = current + sum(max(0, change) for change in changes)
        if low < budget.minimum or high > budget.maximum:
            raise ValueError(f"shared budget {budget.name!r} would exceed [{budget.minimum}, {budget.maximum}]")
        result[budget.name] = current + sum(changes)
    return result


def compile_mutations(mutations: tuple[Mutation, ...], *, budgets: tuple[Budget, ...] = (), max_parallelism: int = 1) -> MutationPlan:
    """
    Respect explicit dependencies, serialize uncertain effects and validate shared bounds.

    Args:
        mutations (tuple[Mutation, ...]): Input order breaks ties between otherwise unordered conflicts.
        budgets (tuple[Budget, ...]): Initial shared counter observations.
        max_parallelism (int): Positive maximum batch size; serial execution is the default.

    Returns:
        MutationPlan: Deterministic execution batches and their ordering evidence.
    """
    if type(max_parallelism) is not int or max_parallelism < 1:
        raise ValueError("max_parallelism must be a positive integer")
    names = [item.name for item in mutations]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError("mutation names must be nonempty and unique")
    counters = {item.name: item for item in budgets}
    if len(counters) != len(budgets):
        raise ValueError("budget names must be unique")
    for budget in budgets:
        if (
            not budget.name
            or any(type(value) is not int for value in (budget.current, budget.minimum, budget.maximum))
            or not budget.minimum <= budget.current <= budget.maximum
        ):
            raise ValueError("budgets require integer bounds containing current usage")
    graph: nx.DiGraph[str] = nx.DiGraph()
    graph.add_nodes_from(names)
    reasons: dict[tuple[str, str], tuple[str, ...]] = {}
    for mutation in mutations:
        if len({delta.budget for delta in mutation.deltas}) != len(mutation.deltas):
            raise ValueError("each mutation may declare only one delta per budget")
        for delta in mutation.deltas:
            if delta.budget not in counters or type(delta.amount) is not int:
                raise ValueError("budget deltas require a declared counter and integer amount")
        for predecessor in mutation.after:
            if predecessor not in names:
                raise ValueError(f"unknown mutation predecessor {predecessor!r}")
            graph.add_edge(predecessor, mutation.name)
            reasons[predecessor, mutation.name] = ("explicit dependency",)
    if not nx.is_directed_acyclic_graph(graph):
        raise ValueError("mutation ordering contains a cycle")

    # Establish dependency-compatible order before orienting conflicts, avoiding artificial cycles.
    rank = {name: index for index, name in enumerate(names)}
    ordered_names = tuple(nx.lexicographical_topological_sort(graph, key=rank.__getitem__))
    indexed = {item.name: item for item in mutations}
    ordered = tuple(indexed[name] for name in ordered_names)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            issues = conflicts(left, right)
            if issues:
                graph.add_edge(left.name, right.name)
                key = left.name, right.name
                reasons[key] = reasons.get(key, ()) + issues
    batches = []
    independences = []
    remaining = set(names)
    usage = {budget.name: budget.current for budget in budgets}
    while remaining:
        ready = tuple(name for name in ordered_names if name in remaining and not (set(graph.predecessors(name)) & remaining))
        batch = ready[:max_parallelism]
        usage = advance_budgets(tuple(indexed[name] for name in batch), budgets, usage)
        for index, left_name in enumerate(batch):
            for right_name in batch[index + 1 :]:
                independences.append(
                    Independence(
                        left_name,
                        right_name,
                        (
                            "adapters declare complete effects",
                            "declared read/write scopes do not overlap",
                            "no explicit or inferred dependency between operations",
                            "shared bounds hold for every completion order in this batch",
                        ),
                    )
                )
        batches.append(batch)
        remaining.difference_update(batch)
    return MutationPlan(
        mutations=ordered,
        budgets=budgets,
        orderings=tuple(Ordering(before, after, why) for (before, after), why in reasons.items()),
        independences=tuple(independences),
        batches=tuple(batches),
        max_parallelism=max_parallelism,
    )
