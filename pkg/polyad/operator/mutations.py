"""
Execute compiled mutation batches without abandoning in-flight side effects.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from polyad.compiler.passes.mutations import advance_budgets, check_preconditions, compile_mutations

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from polyad_types.resources.mutations import Budget, Mutation, MutationPlan, Scope


async def execute_mutations(
    mutations: tuple[Mutation, ...],
    *,
    observe: Callable[[Mutation], Awaitable[Mapping[Scope, str | None]]],
    apply: Callable[[Mutation], Awaitable[None]],
    budgets: tuple[Budget, ...] = (),
    observe_budgets: Callable[[], Awaitable[Mapping[str, int]]] | None = None,
    max_parallelism: int = 1,
) -> MutationPlan:
    """
    Refresh each batch, dispatch bounded callbacks and stop after any failed batch.

    Args:
        mutations (tuple[Mutation, ...]): Declared effects and operation identities.
        observe (Callable[[Mutation], Awaitable[Mapping[Scope, str | None]]]): Refresh exact precondition values.
        apply (Callable[[Mutation], Awaitable[None]]): Fenced operation; completion must satisfy its declared effects.
        budgets (tuple[Budget, ...]): Shared capacity counters controlled by the caller's coordination boundary.
        observe_budgets (Callable[[], Awaitable[Mapping[str, int]]] | None): Required fresh counter reader when budgets exist.
        max_parallelism (int): Positive concurrency bound, defaulting to serial execution.

    Returns:
        MutationPlan: Executed plan, suitable for audit serialization after successful completion.
    """
    plan = compile_mutations(mutations, budgets=budgets, max_parallelism=max_parallelism)
    if budgets and observe_budgets is None:
        raise ValueError("shared budgets require a refreshed counter reader")
    indexed = {item.name: item for item in plan.mutations}
    usage = {budget.name: budget.current for budget in budgets}

    async def dispatch(mutation: Mutation) -> None:
        """
        Capture failures raised before an adapter returns its awaitable.

        Args:
            mutation (Mutation): Operation to dispatch.

        Returns:
            None: No return value.
        """
        await apply(mutation)

    for names in plan.batches:
        batch = tuple(indexed[name] for name in names)
        if observe_budgets is not None:
            current = await observe_budgets()
            if any(name not in current or type(current[name]) is not int or current[name] != value for name, value in usage.items()):
                raise ValueError("shared budget observations changed; replan before dispatch")
        for mutation in batch:
            check_preconditions(mutation, await observe(mutation))

        # A failed sibling or cancelled caller must not release coordination while
        # another callback is still completing a transport request.
        pending = asyncio.gather(*(dispatch(mutation) for mutation in batch), return_exceptions=True)
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        failures = [result for result in pending.result() if isinstance(result, BaseException)]
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("mutation batch failed; refresh before retrying", failures)
        usage = advance_budgets(batch, budgets, usage)
    return plan
