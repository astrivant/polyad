"""
Exercise mutation commutation assumptions, bounds and failure-safe execution.
"""

from __future__ import annotations

import asyncio

import pytest
from attrs import evolve

from polyad.compiler.passes.mutations import check_preconditions, compile_mutations, conflicts
from polyad.operator.mutations import execute_mutations
from polyad_types.resources import Budget, BudgetDelta, Mutation, MutationPlan, Precondition, Scope, converter


def operation(name, **kwargs):
    """
    Declare a test adapter whose effects are restricted to one object.
    """
    return Mutation(name, writes=(Scope(("objects", name)),), effects_complete=True, **kwargs)


def test_independent_mutations_commute_and_plan_roundtrips():
    """
    Disjoint effects allow a batch and either order gives the same modeled state.
    """
    a, b = operation("ingestion"), operation("reporting")
    plan = compile_mutations((a, b), max_parallelism=2)
    assert plan.batches == (("ingestion", "reporting"),)
    assert not plan.orderings
    assert [(item.left, item.right) for item in plan.independences] == [("ingestion", "reporting")]
    assert converter.structure(converter.unstructure(plan), MutationPlan) == plan
    results = []
    for order in ((a, b), (b, a)):
        state = {"ingestion": 2, "reporting": 3}
        for mutation in order:
            state[mutation.name] += 2
        results.append(state)
    assert results[0] == results[1] == {"ingestion": 4, "reporting": 5}


@pytest.mark.parametrize("mode", ["write", "read", "precondition"])
def test_ancestor_effects_conflict_with_descendants(mode):
    """
    Whole-object writes conflict with field writes and implicit precondition reads.
    """
    a = operation("a")
    scope = Scope(("objects", "a", "spec", "replicas"))
    kwargs = {"writes": (scope,)} if mode == "write" else {"reads": (scope,)}
    if mode == "precondition":
        kwargs = {"preconditions": (Precondition(scope, "2"),)}
    b = Mutation("b", effects_complete=True, **kwargs)
    assert conflicts(a, b) and conflicts(b, a)
    plan = compile_mutations((a, b), max_parallelism=2)
    assert plan.batches == (("a",), ("b",))
    assert "overlaps" in plan.orderings[0].reasons[0]


def test_unknown_effects_default_to_ordered_execution():
    """
    Separate object names alone cannot establish independence.
    """
    plan = compile_mutations((Mutation("a"), operation("b")), max_parallelism=8)
    assert plan.batches == (("a",), ("b",))
    assert plan.orderings[0].reasons == ("incomplete effect declaration",)
    assert compile_mutations((operation("a"), operation("b"))).batches == (("a",), ("b",))


def test_explicit_reverse_order_wins_over_input_order_and_conflicts():
    """
    Preserve supplied dependencies before adding conservative conflict edges.
    """
    a, b = Mutation("a", after=("b",)), Mutation("b")
    plan = compile_mutations((a, b), max_parallelism=2)
    assert plan.batches == (("b",), ("a",))
    assert "explicit dependency" in plan.orderings[0].reasons


@pytest.mark.parametrize(
    "mutations",
    [(Mutation("a", after=("b",)),), (Mutation("a", after=("b",)), Mutation("b", after=("a",))), (Mutation("a"), Mutation("a"))],
)
def test_invalid_dependency_graphs_fail_closed(mutations):
    """
    Reject unknown references, cycles and ambiguous operation identities.
    """
    with pytest.raises(ValueError):
        compile_mutations(mutations)


def test_shared_capacity_checks_combined_demand():
    """
    Individually admissible changes can exceed a shared parent limit together.
    """
    budget = Budget("root-replicas", 5, 0, 8)
    a = operation("a", deltas=(BudgetDelta(budget.name, 2),))
    b = operation("b", deltas=(BudgetDelta(budget.name, 2),))
    assert compile_mutations((a,), budgets=(budget,))
    with pytest.raises(ValueError, match="shared budget"):
        compile_mutations((a, b), budgets=(budget,), max_parallelism=2)


def test_capacity_releases_need_completion_before_reuse():
    """
    A net-zero change is unsafe if concurrent increases can finish first.
    """
    budget = Budget("slots", 8, 0, 8)
    release = operation("release", deltas=(BudgetDelta("slots", -2),))
    acquire = operation("acquire", deltas=(BudgetDelta("slots", 2),))
    with pytest.raises(ValueError, match="shared budget"):
        compile_mutations((release, acquire), budgets=(budget,), max_parallelism=2)
    plan = compile_mutations((release, evolve(acquire, after=("release",))), budgets=(budget,), max_parallelism=2)
    assert plan.batches == (("release",), ("acquire",))


@pytest.mark.parametrize("amount", [True, 1.5, "2"])
def test_invalid_budget_deltas(amount):
    """
    Counters use exact integers instead of coercing invalid user values.
    """
    with pytest.raises(ValueError, match="integer"):
        compile_mutations((operation("a", deltas=(BudgetDelta("slots", amount),)),), budgets=(Budget("slots", 0, 0, 3),))


def test_missing_and_stale_preconditions_are_distinct_from_absence():
    """
    Missing observations cannot silently satisfy an absence precondition.
    """
    scope = Scope(("object", "uid"))
    mutation = operation("a", preconditions=(Precondition(scope, None),))
    check_preconditions(mutation, {scope: None})
    for observation in ({}, {scope: "replacement-uid"}):
        with pytest.raises(ValueError, match="precondition"):
            check_preconditions(mutation, observation)


def test_executor_bounds_parallelism_and_waits_for_dependencies():
    """
    Independent callbacks overlap while successors wait for the complete batch.
    """

    async def scenario():
        running = set()
        finished = set()
        both_started = asyncio.Event()

        async def observe(mutation):
            return {}

        async def apply(mutation):
            if mutation.name == "c":
                assert finished == {"a", "b"}
            else:
                running.add(mutation.name)
                if len(running) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), 1)
                running.remove(mutation.name)
            finished.add(mutation.name)

        plan = await execute_mutations(
            (operation("a"), operation("b"), operation("c", after=("a", "b"))),
            observe=observe,
            apply=apply,
            max_parallelism=2,
        )
        assert plan.batches == (("a", "b"), ("c",))
        assert finished == {"a", "b", "c"}

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_failure_or_cancellation_settles_inflight_operations(cancel):
    """
    A failing or cancelled batch does not abandon siblings or start successors.
    """

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        finished = []

        async def observe(mutation):
            return {}

        async def apply(mutation):
            assert mutation.name != "c"
            if mutation.name == "a":
                started.set()
                await release.wait()
                finished.append("a")
            else:
                raise ValueError("failed b")

        task = asyncio.create_task(
            execute_mutations(
                (operation("a"), operation("b"), operation("c", after=("a", "b"))),
                observe=observe,
                apply=apply,
                max_parallelism=2,
            )
        )
        await started.wait()
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError):
            await task
        assert finished == ["a"]

    asyncio.run(scenario())


def test_stale_second_batch_prevents_dispatch():
    """
    Preconditions are reobserved after predecessors have completed.
    """

    async def scenario():
        applied = []
        scope = Scope(("policy", "revision"))

        async def observe(mutation):
            return {scope: "changed" if applied else "1"}

        async def apply(mutation):
            applied.append(mutation.name)

        with pytest.raises(ValueError, match="precondition"):
            await execute_mutations(
                (operation("a"), operation("b", after=("a",), preconditions=(Precondition(scope, "1"),))),
                observe=observe,
                apply=apply,
            )
        assert applied == ["a"]

    asyncio.run(scenario())


def test_budget_observations_are_refreshed_before_dispatch():
    """
    Shared capacity drift invalidates the complete plan before side effects.
    """

    async def scenario():
        async def observe(mutation):
            return {}

        async def apply(mutation):
            pytest.fail("stale budget must prevent dispatch")

        async def counters():
            return {"slots": 4}

        for reader in (None, counters):
            with pytest.raises(ValueError, match="shared budget"):
                await execute_mutations(
                    (operation("a", deltas=(BudgetDelta("slots", 1),)),),
                    observe=observe,
                    apply=apply,
                    budgets=(Budget("slots", 2, 0, 4),),
                    observe_budgets=reader,
                )

    asyncio.run(scenario())


def test_executor_reports_all_batch_failures():
    """
    Even synchronous adapter failures settle and preserve every sibling error.
    """

    async def scenario():
        async def observe(mutation):
            return {}

        def apply(mutation):
            raise ValueError(mutation.name)

        with pytest.raises(ExceptionGroup) as caught:
            await execute_mutations((operation("a"), operation("b")), observe=observe, apply=apply, max_parallelism=2)
        assert [str(error) for error in caught.value.exceptions] == ["a", "b"]

    asyncio.run(scenario())


def test_shared_invariants_and_object_fences_require_ordering():
    """
    Distinct subgraphs remain dependent when they modify a shared invariant.
    """
    invariant = Scope(("invariants", "namespace", "network-policy"))
    a = evolve(operation("a"), writes=(Scope(("objects", "a")), invariant))
    b = operation("b", reads=(invariant,))
    assert compile_mutations((a, b), max_parallelism=2).batches == (("a",), ("b",))


def test_budget_releases_are_refreshed_before_later_acquisition():
    """
    A successful release only funds the next batch after a fresh observation.
    """

    async def scenario():
        usage = {"slots": 4}
        reads = []

        async def observe(mutation):
            return {}

        async def counters():
            reads.append(usage["slots"])
            return dict(usage)

        async def apply(mutation):
            usage["slots"] += mutation.deltas[0].amount

        plan = await execute_mutations(
            (
                operation("release", deltas=(BudgetDelta("slots", -2),)),
                operation("acquire", deltas=(BudgetDelta("slots", 2),), after=("release",)),
            ),
            observe=observe,
            apply=apply,
            budgets=(Budget("slots", 4, 0, 4),),
            observe_budgets=counters,
            max_parallelism=2,
        )
        assert plan.batches == (("release",), ("acquire",))
        assert reads == [4, 2]
        assert usage == {"slots": 4}

    asyncio.run(scenario())
