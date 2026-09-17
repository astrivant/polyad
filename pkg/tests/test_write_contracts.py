"""
Exercise dependency drift, bounded look-ahead, watch invalidation and targeted recovery.
"""

from __future__ import annotations

import asyncio
import copy
import shutil
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from kubernetes.client.exceptions import ApiException

from polyad.compiler.registry import RESOURCE_TYPES
from polyad.operator.adapters.kubernetes import API
from polyad.operator.coordination.contracts import capture_decision, expires_before
from polyad.operator.coordination.validation import ValidationQueue, ValidationSettings, invalidate
from polyad.operator.coordination.write_queue import WriteConflict
from tests.test_operator import resource


def transport(*objects):
    """
    Use the real adapter and queue with a deterministic, versioned Kubernetes transport.
    """
    api = API.__new__(API)
    api.cluster = "test-cluster"
    api.max_pending_writes = 8
    api.validations = ValidationQueue(ValidationSettings(interval=0.01, window=2, burst=2))
    api.on_write_drift = AsyncMock()
    state = {(obj["kind"], obj["metadata"]["namespace"], obj["metadata"]["name"]): copy.deepcopy(obj) for obj in objects}
    plurals = {descriptor.plural: kind for kind, descriptor in RESOURCE_TYPES.items()}

    def request(path, method, **kwargs):
        parts = path.split("/")
        offset = parts.index("namespaces")
        namespace, kind = parts[offset + 1], plurals[parts[offset + 2]]
        name = parts[offset + 3] if len(parts) > offset + 3 else ""
        if method == "GET":
            if name:
                if (kind, namespace, name) not in state:
                    raise ApiException(status=404)
                return copy.deepcopy(state[(kind, namespace, name)])
            items = [copy.deepcopy(obj) for (k, ns, _), obj in state.items() if k == kind and ns == namespace]
            for selector, expression in kwargs.get("query_params", []):
                assert selector == "labelSelector"
                label, value = expression.split("=", 1)
                items = [obj for obj in items if obj["metadata"].get("labels", {}).get(label) == value]
            return {"items": items}
        body = kwargs.get("body") or {}
        key = kind, namespace, name or body["metadata"]["name"]
        if method == "POST":
            if key in state:
                raise ApiException(status=409)
            state[key] = copy.deepcopy(body)
            state[key]["metadata"].update(uid="new-" + key[2], resourceVersion="1")
        elif key not in state:
            raise ApiException(status=404)
        elif method == "DELETE":
            state[key]["metadata"]["deletionTimestamp"] = "now"
            return {"kind": "Status", "status": "Success"}
        else:
            obj = state[key]
            if obj["metadata"]["resourceVersion"] != body["metadata"]["resourceVersion"]:
                raise ApiException(status=409)
            obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
            for field in ("spec", "status"):
                if field in body:
                    obj.setdefault(field, {}).update(body[field])
        return copy.deepcopy(state[key])

    api.client = Mock()
    api.client.call_api.side_effect = request
    return api, state


def patch(name, replicas=4):
    """
    Build an update fenced to the original observed version.
    """
    return {"metadata": {"uid": "uid-" + name, "resourceVersion": "1"}, "spec": {"replicas": replicas}}


async def until(predicate):
    """
    Wait for a deterministic queue state without relying on transport scheduling order.
    """
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def test_dependency_deadline_expires_a_fresh_receipt_without_an_event(monkeypatch):
    """
    Queue delay cannot extend a TTL connection or throughput sample's original validity.
    """
    now = [100.0]
    monkeypatch.setattr("polyad.operator.coordination.contracts.time", SimpleNamespace(time=lambda: now[0]))

    async def run():
        api, _ = transport(resource("Deployment", "target"))
        async with api.write_lock:
            with capture_decision(api, ("Graph", "test", "origin")):
                expires_before(datetime.fromtimestamp(101, UTC))
                task = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            await until(lambda: api.validations.pending and all(item.fresh() for item in api.validations.pending.values()))
            now[0] = 102
        with pytest.raises(WriteConflict) as error:
            await task
        assert error.value.conflict_reason == "dependency_deadline_elapsed"
        assert all(call.args[1] == "GET" for call in api.client.call_api.call_args_list)
        api.on_write_drift.assert_awaited_once_with(("Graph", "test", "origin"))

    asyncio.run(run())


@pytest.mark.parametrize("drift", ["missing", "replacement", "readiness", "spec"])
def test_dependency_drift_discards_write_and_refreshes_affected_graphs(drift):
    """
    The unchanged target is insufficient when a downstream dependency has drifted.
    """

    async def run():
        api, state = transport(resource("Graph", "dependency"), resource("Deployment", "target"))
        async with api.write_lock:
            with capture_decision(api, ("Graph", "test", "origin")):
                await api.get("Graph", "test", "dependency")
                task = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            await until(lambda: api.validations.pending and all(item.fresh() for item in api.validations.pending.values()))
            dep = state[("Graph", "test", "dependency")]
            if drift == "missing":
                del state[("Graph", "test", "dependency")]
            elif drift == "replacement":
                dep["metadata"]["uid"] = "replacement"
            elif drift == "readiness":
                dep["status"] = {"ready": False}
            else:
                dep["spec"]["replicas"] = 10
            invalidate(api, ("Graph", "test", "dependency"))
        with pytest.raises(WriteConflict) as error:
            await task
        assert error.value.conflict_reason == "dependency_state_changed"
        assert all(call.args[1] == "GET" for call in api.client.call_api.call_args_list)
        assert {call.args[0] for call in api.on_write_drift.await_args_list} == {
            ("Graph", "test", "origin"),
            ("Graph", "test", "dependency"),
        }
        assert api.writes.snapshot()["total"] == 0 and not api.validations.pending

    asyncio.run(run())


@pytest.mark.parametrize("observation", ["collection", "absence"])
def test_new_resources_invalidate_collection_membership_and_expected_absence(observation):
    """
    A contract covers negative observations and newly introduced rules or children too.
    """

    async def run():
        api, state = transport(resource("Deployment", "target"))
        async with api.write_lock:
            with capture_decision(api, ("Graph", "test", "origin")):
                await api.request("GET", "GraphRule", "test", "new" if observation == "absence" else "")
                task = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            await until(lambda: api.validations.pending and all(item.fresh() for item in api.validations.pending.values()))
            state[("GraphRule", "test", "new")] = resource("GraphRule", "new")
            invalidate(api, ("GraphRule", "test", "new"))
        with pytest.raises(WriteConflict, match="409"):
            await task
        assert all(call.args[1] == "GET" for call in api.client.call_api.call_args_list)

    asyncio.run(run())


def test_background_walks_third_and_fourth_items_and_dispatch_reuses_receipts():
    """
    Bounded bursts walk the queue while the first slot is occupied, without duplicate reads at dispatch.
    """

    async def run():
        names = ["one", "two", "three", "four"]
        api, _ = transport(*(resource("Deployment", name) for name in names))
        async with api.write_lock:
            tasks = [asyncio.create_task(api.request("PATCH", "Deployment", "test", name, patch(name))) for name in names]
            await until(lambda: len(api.validations.pending) == 4 and all(item.fresh() for item in api.validations.pending.values()))
            assert [call.args[0].rsplit("/", 1)[1] for call in api.client.call_api.call_args_list] == names
        await asyncio.gather(*tasks)
        methods = [call.args[1] for call in api.client.call_api.call_args_list]
        assert methods.count("GET") == 4 and methods.count("PATCH") == 4
        assert not api.validations.pending and api.validations.task.done()

    asyncio.run(run())


def test_receipt_expiry_requires_a_new_read_even_without_watch_events(monkeypatch):
    """
    Lost notifications cannot extend a cached validation beyond its configured window.
    """
    now = [0.0]
    monkeypatch.setattr("polyad.operator.coordination.validation.time", SimpleNamespace(monotonic=lambda: now[0]))

    async def run():
        api, _ = transport(resource("Deployment", "target"))
        api.validations = ValidationQueue(ValidationSettings(interval=1, window=2, burst=2))
        async with api.write_lock:
            task = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            await until(lambda: api.validations.pending and all(item.fresh() for item in api.validations.pending.values()))
            assert api.client.call_api.call_count == 1
            now[0] = 3.0
        await task
        assert [call.args[1] for call in api.client.call_api.call_args_list] == ["GET", "GET", "PATCH"]

    asyncio.run(run())


def test_earlier_write_invalidates_later_dependency_validation():
    """
    Queue progress cannot reuse an approval made before an earlier write changed its inputs.
    """

    async def run():
        api, _ = transport(resource("Deployment", "first"), resource("Deployment", "second"))
        async with api.write_lock:
            first = asyncio.create_task(api.request("PATCH", "Deployment", "test", "first", patch("first")))
            with capture_decision(api, ("Graph", "test", "origin")):
                await api.get("Deployment", "test", "first")
                second = asyncio.create_task(api.request("PATCH", "Deployment", "test", "second", patch("second")))
            await until(lambda: len(api.validations.pending) == 2 and all(item.fresh() for item in api.validations.pending.values()))
        await first
        with pytest.raises(WriteConflict):
            await second
        writes = [call.args[0].rsplit("/", 1)[1] for call in api.client.call_api.call_args_list if call.args[1] == "PATCH"]
        assert writes == ["first"]

    asyncio.run(run())


def test_unnamed_write_invalidates_cached_collection_dependencies():
    """
    An unknown target cannot preserve a later approval through a generated-name creation.
    """

    async def run():
        api, state = transport(resource("Deployment", "target"))
        original = api.client.call_api.side_effect

        def generated(path, method, **kwargs):
            if method == "POST":
                obj = resource("Graph", "generated")
                state[("Graph", "test", "generated")] = obj
                return copy.deepcopy(obj)
            return original(path, method, **kwargs)

        api.client.call_api.side_effect = generated
        async with api.write_lock:
            first = asyncio.create_task(api.request("POST", "Graph", "test", body={"metadata": {"generateName": "graph-"}}))
            with capture_decision(api, ("Graph", "test", "origin")):
                await api.request("GET", "Graph", "test")
                second = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            await until(lambda: len(api.validations.pending) == 2 and all(item.fresh() for item in api.validations.pending.values()))
        await first
        with pytest.raises(WriteConflict) as error:
            await second
        assert error.value.conflict_reason == "dependency_state_changed"
        assert not any(call.args[1] == "PATCH" for call in api.client.call_api.call_args_list)

    asyncio.run(run())


def test_target_disappearing_during_transport_requests_targeted_recovery():
    """
    A server-side PATCH 404 after validation is retryable and never treated as successful deletion.
    """

    async def run():
        target = resource("Deployment", "target")
        target["metadata"]["ownerReferences"] = [
            {"apiVersion": "polyad.astrivant.com/v1alpha1", "kind": "Graph", "name": "owner", "controller": True}
        ]
        api, state = transport(target)
        original = api.client.call_api.side_effect

        def disappear(path, method, **kwargs):
            if method == "PATCH":
                state.clear()
            return original(path, method, **kwargs)

        api.client.call_api.side_effect = disappear
        with capture_decision(api, ("Graph", "test", "origin")):
            await api.get("Deployment", "test", "target")
            with pytest.raises(WriteConflict) as error:
                await api.request("PATCH", "Deployment", "test", "target", patch("target"))
        assert error.value.conflict_reason == "write_target_disappeared"
        assert {call.args[0] for call in api.on_write_drift.await_args_list} == {("Graph", "test", "origin"), ("Graph", "test", "owner")}
        assert api.writes.snapshot()["total"] == 0

    asyncio.run(run())


def test_contract_advances_own_acknowledged_effects_in_collection():
    """
    Several intentional writes can complete without mistaking earlier acknowledged effects for drift.
    """

    async def run():
        api, state = transport(resource("Deployment", "first"), resource("Deployment", "second"))
        with capture_decision(api, ("Graph", "test", "origin")):
            await api.request("GET", "Deployment", "test")
            await api.request("PATCH", "Deployment", "test", "first", patch("first"))
            await api.request("PATCH", "Deployment", "test", "second", patch("second"))
        assert all(obj["spec"]["replicas"] == 4 for obj in state.values())
        api.on_write_drift.assert_not_awaited()

    asyncio.run(run())


def test_queue_capacity_refuses_overflow_without_retaining_payloads():
    """
    One active validation and one waiter are the default admitted capacity.
    """

    async def run():
        api, _ = transport(*(resource("Deployment", name) for name in ("one", "two", "three")))
        api.max_pending_writes = 1
        async with api.write_lock:
            first = asyncio.create_task(api.request("PATCH", "Deployment", "test", "one", patch("one")))
            second = asyncio.create_task(api.request("PATCH", "Deployment", "test", "two", patch("two")))
            await until(lambda: len(api.validations.pending) == 2)
            with pytest.raises(WriteConflict) as error:
                await api.request("PATCH", "Deployment", "test", "three", patch("three"))
            assert error.value.status == 429 and error.value.conflict_reason == "write_queue_full"
            assert len(api.validations.pending) == api.writes.snapshot()["total"] == 2
        await asyncio.gather(first, second)
        assert not any(call.args[0].endswith("/three") for call in api.client.call_api.call_args_list)

    asyncio.run(run())


def test_kopf_notifications_invalidate_receipts_and_publish_only_graph_owners(monkeypatch):
    """
    Native resource watch events wake validation without becoming native reconciliation requests.
    """
    from polyad.operator.lifecycle import handlers

    async def run():
        api, state = transport(resource("Deployment", "dependency"), resource("Deployment", "target"))
        monkeypatch.setattr(handlers, "controller", SimpleNamespace(api=api))
        monkeypatch.setattr(handlers, "role", lambda: "dense")
        publish = AsyncMock()
        monkeypatch.setattr(handlers, "publish", publish)
        body = state[("Deployment", "test", "dependency")]
        body["metadata"]["ownerReferences"] = [
            {"apiVersion": "polyad.astrivant.com/v1alpha1", "kind": "Graph", "name": "owner", "controller": True}
        ]
        async with api.write_lock:
            with capture_decision(api, ("Graph", "test", "origin")):
                await api.get("Deployment", "test", "dependency")
                task = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            await until(lambda: api.validations.pending and all(item.fresh() for item in api.validations.pending.values()))
            body["status"] = {"readyReplicas": 0}
            await handlers.handle("test", "dependency", body, type="MODIFIED")
            assert not next(iter(api.validations.pending.values())).fresh()
            publish.assert_awaited_once_with(("Graph", "test", "owner"))
        with pytest.raises(WriteConflict):
            await task

    asyncio.run(run())


def test_cancelled_waiter_joins_its_background_validation_read():
    """
    Repeated cancellation cannot strand validation transports or their producer task.
    """

    async def run():
        api, _ = transport(resource("Deployment", "target"))
        original = api.client.call_api.side_effect
        started, release = threading.Event(), threading.Event()

        def blocked(path, method, **kwargs):
            started.set()
            assert release.wait(3)
            return original(path, method, **kwargs)

        api.client.call_api.side_effect = blocked
        async with api.write_lock:
            task = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            try:
                assert await asyncio.to_thread(started.wait, 3)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not api.validations.pending and api.validations.task.done()
        assert api.writes.snapshot()["total"] == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "settings", [{"interval": 0}, {"window": 61}, {"interval": 3, "window": 2}, {"burst": 0}, {"burst": 129}, {"burst": True}]
)
def test_validation_settings_reject_invalid_budgets(settings):
    """
    Invalid freshness or burst limits cannot silently produce unbounded work.
    """
    with pytest.raises(ValueError):
        ValidationSettings(**settings)


def test_identical_pending_decisions_join_even_when_capacity_is_full():
    """
    Duplicate intake consumes neither another backlog slot nor another validation burst entry.
    """

    async def run():
        api, _ = transport(resource("Deployment", "target"))
        api.max_pending_writes = 0
        async with api.write_lock:
            first = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            await until(lambda: len(api.validations.pending) == 1)
            followers = [asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target"))) for _ in range(8)]
            await asyncio.sleep(0)
            assert len(api.pending_calls) == api.writes.snapshot()["total"] == 1
        results = await asyncio.gather(first, *followers)
        assert all(result == results[0] for result in results)
        assert sum(call.args[1] == "PATCH" for call in api.client.call_api.call_args_list) == 1
        assert not api.pending_calls

    asyncio.run(run())


def test_identical_patch_with_different_dependency_contracts_is_not_coalesced():
    """
    Equal target bodies do not collapse decisions made against different graph assumptions.
    """

    async def run():
        api, _ = transport(resource("Deployment", "target"), resource("Graph", "left"), resource("Graph", "right"))
        tasks = []
        async with api.write_lock:
            for name in ("left", "right"):
                with capture_decision(api, ("Graph", "test", name)):
                    await api.get("Graph", "test", name)
                    tasks.append(asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target"))))
            await until(lambda: len(api.pending_calls) == 2)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(item, WriteConflict) for item in results) == 1
        assert sum(call.args[1] == "PATCH" for call in api.client.call_api.call_args_list) == 1

    asyncio.run(run())


def test_cancelling_duplicate_waiter_does_not_cancel_admitted_change():
    """
    One caller abandoning a shared result cannot cancel another caller's queued intent.
    """

    async def run():
        api, _ = transport(resource("Deployment", "target"))
        async with api.write_lock:
            first = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            await until(lambda: len(api.pending_calls) == 1)
            duplicate = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            await asyncio.sleep(0)
            duplicate.cancel()
            with pytest.raises(asyncio.CancelledError):
                await duplicate
        await first
        assert sum(call.args[1] == "PATCH" for call in api.client.call_api.call_args_list) == 1

    asyncio.run(run())


def test_cancelled_admitted_change_returns_followers_for_reconciliation():
    """
    Cancelling the original caller returns retryable conflicts, never cancellation of another queue consumer.
    """

    async def run():
        api, _ = transport(resource("Deployment", "target"))
        async with api.write_lock:
            first = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            await until(lambda: len(api.pending_calls) == 1)
            duplicate = asyncio.create_task(api.request("PATCH", "Deployment", "test", "target", patch("target")))
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(WriteConflict) as error:
                await duplicate
            assert error.value.conflict_reason == "shared_write_cancelled"
        assert not api.pending_calls and not api.validations.pending

    asyncio.run(run())


@pytest.mark.parametrize("profile", ["ha=false", "ha=true", "ha=true,distributed"])
@pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm")
def test_chart_projects_validation_budgets_and_watch_permissions(profile):
    """
    Every operator deployment receives typed queue settings and native dependency watch access.
    """
    from tests.test_chart import render

    settings = (
        ["ha=true", "architecture.mode=Distributed", "metrics.enabled=true", "api.enabled=true"]
        if profile.endswith("distributed")
        else [profile]
    )
    objects = render(
        *settings,
        "operator.writeQueue.maxPending=4",
        "operator.writeQueue.validationBurst=3",
        "operator.writeQueue.maxInFlight=2",
        "operator.writeQueue.validationWorkers=3",
        "operator.writeQueue.reconciliationWorkers=4",
    )
    deployments = [obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"].startswith("test-polyad")]
    assert deployments
    for deployment in deployments:
        container = next(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "operator")
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["POLYAD_WRITE_QUEUE_MAX_PENDING"] == "4"
        assert env["POLYAD_WRITE_VALIDATION_BURST"] == "3"
        assert env["POLYAD_WRITE_VALIDATION_INTERVAL_SECONDS"] == "1"
        assert env["POLYAD_WRITE_VALIDATION_WINDOW_SECONDS"] == "5"
        assert env["POLYAD_WRITE_MAX_IN_FLIGHT"] == "2"
        assert env["POLYAD_WRITE_VALIDATION_WORKERS"] == "3"
        assert env["POLYAD_RECONCILIATION_WORKERS"] == "4"
    role = next(item for item in objects if item["kind"] == "Role" and item["metadata"]["name"] == "test-polyad")
    assert all("watch" in rule["verbs"] for rule in role["rules"] if {"deployments", "jobs", "pods", "services"} & set(rule["resources"]))


def test_new_demand_revises_the_decision_through_fresh_reconciliation():
    """
    Changed input replaces stale scaling intent with a newly validated decision, not arrival-order authority.
    """

    async def run():
        api, state = transport(resource("Graph", "demand", {"desired": 4}), resource("Deployment", "target"))

        async def decide():
            with capture_decision(api, ("Graph", "test", "demand")):
                observed = await api.get("Graph", "test", "demand")
                return await api.request("PATCH", "Deployment", "test", "target", patch("target", observed["spec"]["desired"]))

        async with api.write_lock:
            old = asyncio.create_task(decide())
            await until(lambda: len(api.pending_calls) == 1)
            state[("Graph", "test", "demand")]["spec"]["desired"] = 2
            revised = asyncio.create_task(decide())
            await until(lambda: len(api.pending_calls) == 2)
        results = await asyncio.gather(old, revised, return_exceptions=True)
        assert all(isinstance(result, WriteConflict) for result in results)
        assert all(call.args[1] == "GET" for call in api.client.call_api.call_args_list)
        assert (await decide())["spec"]["replicas"] == 2
        assert sum(call.args[1] == "PATCH" for call in api.client.call_api.call_args_list) == 1

    asyncio.run(run())
