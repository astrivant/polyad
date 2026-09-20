"""
Execute compiled mutation batches without abandoning in-flight side effects.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from polyad.compiler.passes.mutations import PreconditionFailed, advance_budgets, check_preconditions, compile_mutations
from polyad.operator.coordination.dispatch import Admission, Batch, admission
from polyad.operator.coordination.settings import WorkGraphSettings
from polyad.operator.observability.decisions import decision

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from polyad_types.resources.mutations import Budget, Mutation, MutationPlan, Scope

__all__ = ("execute_mutations",)


async def execute_mutations(
    mutations: tuple[Mutation, ...],
    *,
    observe: Callable[[Mutation], Awaitable[Mapping[Scope, str | None]]],
    apply: Callable[[Mutation], Awaitable[None]],
    budgets: tuple[Budget, ...] = (),
    observe_budgets: Callable[[], Awaitable[Mapping[str, int]]] | None = None,
    max_parallelism: int | None = None,
) -> MutationPlan:
    """
    Refresh each batch, dispatch bounded callbacks and stop after any failed batch.

    Args:
        mutations (tuple[Mutation, ...]): Declared effects and operation identities.
        observe (Callable[[Mutation], Awaitable[Mapping[Scope, str | None]]]): Refresh exact precondition values.
        apply (Callable[[Mutation], Awaitable[None]]): Fenced operation; completion must satisfy its declared effects.
        budgets (tuple[Budget, ...]): Shared capacity counters controlled by the caller's coordination boundary.
        observe_budgets (Callable[[], Awaitable[Mapping[str, int]]] | None): Required fresh counter reader when budgets exist.
        max_parallelism (int | None): Optional lower batch limit; omitted uses the administrator's configured planner ceiling.

    Returns:
        MutationPlan: Executed plan, suitable for audit serialization after successful completion.
    """
    ceiling = WorkGraphSettings.from_environment().planner_parallelism
    if max_parallelism is not None and (type(max_parallelism) is not int or max_parallelism < 1):
        raise ValueError("mutation parallelism must be a positive integer")
    limit = ceiling if max_parallelism is None else min(max_parallelism, ceiling)
    plan = compile_mutations(mutations, budgets=budgets, max_parallelism=limit)
    for ordering in plan.orderings:
        decision(
            "polyad.mutation.ordered",
            "These requests must run in order because their declared effects or dependencies overlap.",
            outcome="serialized",
            reason="mutation_ordering",
            level=logging.DEBUG,
            attributes={
                "polyad.request.before": ordering.before,
                "polyad.request.after": ordering.after,
                "polyad.ordering.reasons": ordering.reasons,
            },
        )
    if budgets and observe_budgets is None:
        raise ValueError("shared budgets require a refreshed counter reader")
    indexed = {item.name: item for item in plan.mutations}
    usage = {budget.name: budget.current for budget in budgets}

    async def dispatch(mutation: Mutation, approval: Batch) -> None:
        """
        Capture failures raised before an adapter returns its awaitable.

        Args:
            mutation (Mutation): Operation to dispatch.
            approval (Batch): Fresh shared-budget and precondition approval for this batch.

        Returns:
            None: No return value.
        """
        token = admission.set(Admission(approval, mutation))
        try:
            await apply(mutation)
        finally:
            admission.reset(token)
        decision(
            "polyad.mutation.applied",
            "The admitted mutation completed.",
            outcome="applied",
            reason="preconditions_passed",
            attributes={"polyad.request.name": mutation.name},
        )

    # Planning is not a reservation: refresh counters and every precondition at each barrier.
    for names in plan.batches:
        batch = tuple(indexed[name] for name in names)
        if observe_budgets is not None:
            current = await observe_budgets()
            if any(name not in current or type(current[name]) is not int or current[name] != value for name, value in usage.items()):
                decision(
                    "polyad.mutation.conflict",
                    "Shared capacity changed after planning; the batch must be replanned before dispatch.",
                    outcome="blocked",
                    reason="budget_changed",
                    level=logging.WARNING,
                    attributes={"polyad.requests": names},
                )
                raise ValueError("shared budget observations changed; replan before dispatch")
        for mutation in batch:
            try:
                check_preconditions(mutation, await observe(mutation))
            except PreconditionFailed:
                decision(
                    "polyad.mutation.conflict",
                    "A mutation's required observations changed; the batch was not dispatched.",
                    outcome="blocked",
                    reason="precondition_changed",
                    level=logging.WARNING,
                    attributes={"polyad.request.name": mutation.name},
                )
                raise

        # A failed sibling or cancelled caller must not release coordination while
        # another callback is still completing a transport request.
        approval = Batch()
        pending = asyncio.gather(*(dispatch(mutation, approval) for mutation in batch), return_exceptions=True)
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
        approval.active = False
        if cancelled:
            raise asyncio.CancelledError
        failures = [result for result in pending.result() if isinstance(result, BaseException)]
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("mutation batch failed; refresh before retrying", failures)

        # Only a fully successful batch establishes the expected counter values for the next one.
        usage = advance_budgets(batch, budgets, usage)
    return plan
