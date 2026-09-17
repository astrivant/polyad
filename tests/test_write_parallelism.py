"""
Exercise dependency-aware parallel writers, validators and refreshed reconciliation workers.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from polyad.operator.coordination.contracts import capture_decision
from polyad.operator.coordination.dispatch import DispatchGraph
from polyad.operator.coordination.queue import RefreshQueue
from polyad.operator.coordination.validation import ValidationQueue, ValidationSettings
from polyad.operator.coordination.write_queue import WriteConflict
from polyad.operator.reconciliation.mutations import execute_mutations
from polyad_types.resources import Mutation, Scope
from tests.test_operator import resource
from tests.test_write_contracts import patch, transport, until


def operation(name, *, after=(), complete=True, field_only=False):
    """
    Declare actual whole-object Kubernetes transport effects for a planned operation.
    """
    path = ("kubernetes", "apps/v1", "Deployment", "test", name)
    return Mutation(name, writes=(Scope(path + (("spec",) if field_only else ())),), after=after, effects_complete=complete)


def test_connected_diamond_executes_ready_siblings_in_parallel():
    """
    A connected work graph exposes parallel B/C writers between its A and D barriers.
    """

    async def run():
        api, _ = transport(*(resource("Deployment", name) for name in "abcd"))
        api.write_lock = DispatchGraph(2)
        api.before_write = AsyncMock()
        original = api.client.call_api.side_effect
        both = threading.Barrier(2, timeout=3)
        completed = set()
        peak = []

        def write(path, method, **kwargs):
            name = path.rsplit("/", 1)[1]
            if method == "PATCH":
                if name in "bc":
                    assert "a" in completed and "d" not in completed
                    both.wait()
                if name == "d":
                    assert {"a", "b", "c"} <= completed
                peak.append(api.writes.snapshot()["inFlight"])
            result = original(path, method, **kwargs)
            if method == "PATCH":
                completed.add(name)
            return result

        api.client.call_api.side_effect = write

        async def apply(item):
            await api.request("PATCH", "Deployment", "test", item.name, patch(item.name))

        plan = await execute_mutations(
            (operation("d", after=("b", "c")), operation("c", after=("a",)), operation("a"), operation("b", after=("a",))),
            observe=AsyncMock(return_value={}),
            apply=apply,
            max_parallelism=2,
        )
        assert set(plan.batches[1]) == {"b", "c"}
        assert max(peak) == 2 and completed == set("abcd")
        assert api.before_write.await_count == 4
        assert not api.write_lock.graph and api.writes.snapshot()["total"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["ordinary", "separate_plans", "field_only", "incomplete"])
def test_unproven_transport_independence_remains_serial(mode):
    """
    Multiple slots do not grant parallelism to unknown effects or unrelated plan approvals.
    """

    async def run():
        api, _ = transport(*(resource("Deployment", name) for name in "ab"))
        api.write_lock = DispatchGraph(2)
        original = api.client.call_api.side_effect
        started, release = threading.Event(), threading.Event()
        writes = []

        def write(path, method, **kwargs):
            if method == "PATCH":
                writes.append(path.rsplit("/", 1)[1])
                if len(writes) == 1:
                    started.set()
                    assert release.wait(3)
            return original(path, method, **kwargs)

        api.client.call_api.side_effect = write

        async def apply(item):
            await api.request("PATCH", "Deployment", "test", item.name, patch(item.name))

        mutations = tuple(operation(name, complete=mode != "incomplete", field_only=mode == "field_only") for name in "ab")
        if mode == "ordinary":
            task = asyncio.gather(*(apply(item) for item in mutations))
        elif mode == "separate_plans":
            task = asyncio.gather(*(execute_mutations((item,), observe=AsyncMock(return_value={}), apply=apply) for item in mutations))
        else:
            task = asyncio.create_task(execute_mutations(mutations, observe=AsyncMock(return_value={}), apply=apply, max_parallelism=2))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            await asyncio.sleep(0.02)
            assert writes == ["a"] and api.writes.snapshot()["inFlight"] == 1
        finally:
            release.set()
            await task
        assert writes == ["a", "b"]

    asyncio.run(run())


def test_observed_dependency_overrides_incorrect_planner_independence():
    """
    A captured read/write overlap adds a runtime ordering edge and rejects the stale successor.
    """

    async def run():
        api, _ = transport(*(resource("Deployment", name) for name in "ab"))
        api.write_lock = DispatchGraph(2)

        async def apply(item):
            with capture_decision(api, ("Graph", "test", item.name)):
                if item.name == "b":
                    await api.get("Deployment", "test", "a")
                await api.request("PATCH", "Deployment", "test", item.name, patch(item.name))

        async with api.write_lock:
            plan = asyncio.create_task(
                execute_mutations(
                    (operation("a"), operation("b")),
                    observe=AsyncMock(return_value={}),
                    apply=apply,
                    max_parallelism=2,
                )
            )
            await until(lambda: len(api.validations.pending) == 2)
            tokens = {receipt.intent.target[2]: token for token, receipt in api.validations.pending.items()}
            assert api.write_lock.graph.has_edge(tokens["a"], tokens["b"])
        with pytest.raises(WriteConflict):
            await plan
        assert [call.args[0].rsplit("/", 1)[1] for call in api.client.call_api.call_args_list if call.args[1] == "PATCH"] == ["a"]
        assert api.on_write_drift.await_count

    asyncio.run(run())


def test_validation_workers_overlap_reads_without_exceeding_limit():
    """
    Producer and dispatcher share one bounded pool of authoritative candidate reads.
    """

    async def run():
        api, _ = transport(*(resource("Deployment", name) for name in "abcd"))
        api.validations = ValidationQueue(ValidationSettings(interval=0.01, window=3, burst=4, workers=2))
        original = api.client.call_api.side_effect
        lock = threading.Lock()
        release, two = threading.Event(), threading.Event()
        active, maximum = 0, 0

        def read(path, method, **kwargs):
            nonlocal active, maximum
            if method == "GET":
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                    if active == 2:
                        two.set()
                try:
                    assert release.wait(3)
                finally:
                    with lock:
                        active -= 1
            return original(path, method, **kwargs)

        api.client.call_api.side_effect = read
        async with api.write_lock:
            tasks = [asyncio.create_task(api.request("PATCH", "Deployment", "test", name, patch(name))) for name in "abcd"]
            try:
                assert await asyncio.to_thread(two.wait, 3)
                assert maximum == 2
            finally:
                release.set()
            await until(lambda: all(item.fresh() for item in api.validations.pending.values()))
        await asyncio.gather(*tasks)
        assert maximum == 2

    asyncio.run(run())


def test_reconciliation_workers_overlap_distinct_keys_and_coalesce_active_followups():
    """
    A second worker handles B while A runs; new hints for A wait for its first attempt.
    """

    async def run():
        entered = {name: asyncio.Event() for name in "ab"}
        release = asyncio.Event()
        calls = []
        active = set()

        async def reconcile(key):
            assert key not in active
            active.add(key)
            calls.append(key[2])
            entered[key[2]].set()
            await release.wait()
            active.remove(key)

        queue = RefreshQueue(reconcile, workers=2)
        queue.start()
        a, b = ("Graph", "test", "a"), ("Graph", "test", "b")
        try:
            first = asyncio.create_task(queue.submit(a))
            await asyncio.wait_for(entered["a"].wait(), 2)
            rest = [asyncio.create_task(queue.submit(key)) for key in (a, a, b)]
            await asyncio.wait_for(entered["b"].wait(), 2)
            assert calls == ["a", "b"]
            release.set()
            await asyncio.gather(first, *rest)
            assert calls == ["a", "b", "a"]
        finally:
            release.set()
            await queue.stop()

    asyncio.run(run())


def test_cancelling_a_parallel_plan_joins_both_transports_before_releasing_slots():
    """
    Repeated cancellation cannot leave either independent write running outside its plan.
    """

    async def run():
        api, _ = transport(*(resource("Deployment", name) for name in "ab"))
        api.write_lock = DispatchGraph(2)
        original = api.client.call_api.side_effect
        entered = {name: threading.Event() for name in "ab"}
        release = threading.Event()

        def write(path, method, **kwargs):
            if method == "PATCH":
                entered[path.rsplit("/", 1)[1]].set()
                assert release.wait(3)
            return original(path, method, **kwargs)

        api.client.call_api.side_effect = write

        async def apply(item):
            await api.request("PATCH", "Deployment", "test", item.name, patch(item.name))

        task = asyncio.create_task(
            execute_mutations(
                tuple(operation(name) for name in "ab"),
                observe=AsyncMock(return_value={}),
                apply=apply,
                max_parallelism=2,
            )
        )
        try:
            for event in entered.values():
                assert await asyncio.to_thread(event.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert len(api.write_lock.active) == 2 and api.writes.snapshot()["inFlight"] == 2
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not api.write_lock.graph and api.writes.snapshot()["total"] == 0

    asyncio.run(run())
