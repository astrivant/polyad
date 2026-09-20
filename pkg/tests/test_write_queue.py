"""
Exercise conflicts, stale decisions and cancellation in the concrete Kubernetes write backlog.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
from unittest.mock import Mock

import pytest

from polyad.operator.adapters.kubernetes import API
from polyad.operator.coordination.write_queue import WriteConflict, write_intent


def adapter(*, replicas=3):
    """
    Build a real queue adapter backed by a deterministic revisioned Kubernetes transport.
    """
    api = API.__new__(API)
    api.cluster = "local"
    api.client = Mock()
    state = {"metadata": {"name": "workers", "uid": "original", "resourceVersion": "10"}, "spec": {"replicas": replicas}}

    def transport(path, method, **kwargs):
        if method in {"GET", "HEAD"}:
            return copy.deepcopy(state)
        if method == "DELETE":
            return None
        state["metadata"]["resourceVersion"] = str(int(state["metadata"]["resourceVersion"]) + 1)
        state["spec"].update(kwargs["body"].get("spec", {}))
        return copy.deepcopy(state)

    api.client.call_api.side_effect = transport
    return api, state


def scale(replicas, **metadata):
    """
    Describe an absolute replica target computed from one observed object revision.
    """
    return {"metadata": {"uid": "original", "resourceVersion": "10", **metadata}, "spec": {"replicas": replicas}}


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet", "ReplicaGroup", "OperatorPool", "Dragonfly"])
def test_opposing_pending_scales_require_reconciliation_without_writes(kind, caplog):
    """
    Refuse both stale alternatives instead of choosing arrival order or summing adjustments.
    """

    async def run():
        api, state = adapter()
        async with api.write_lock:
            up = asyncio.create_task(api.request("PATCH", kind, "test", "workers", scale(4)))
            down = asyncio.create_task(api.request("PATCH", kind, "test", "workers", scale(2)))
            await asyncio.sleep(0)
            assert api.writes.snapshot()["queued"] == 2
            assert api.writes.snapshot()["inFlight"] == 0
            api.client.call_api.assert_not_called()
        results = await asyncio.gather(up, down, return_exceptions=True)
        assert all(isinstance(result, WriteConflict) and result.status == 409 for result in results)
        assert {result.conflict_reason for result in results} == {"overlapping_pending_writes"}
        api.client.call_api.assert_not_called()
        assert state["spec"]["replicas"] == 3
        assert api.writes.snapshot()["total"] == 0
        assert not api.pending_writes.entries and not api.pending_writes.conflicted and not api.pending_writes.targets

        # A fresh reconciliation may now choose a single authoritative target.
        await api.request("PATCH", kind, "test", "workers", scale(2))
        assert state["spec"]["replicas"] == 2
        assert api.client.call_api.call_count == 1

    with caplog.at_level(logging.WARNING, logger="polyad"):
        asyncio.run(run())
    records = [record for record in caplog.records if getattr(record, "event_name", "") == "polyad.kubernetes.write_deferred"]
    assert len(records) == 2
    assert all(record.polyad_attributes["polyad.decision.reason"] == "overlapping_pending_writes" for record in records)


@pytest.mark.parametrize("other", ["delete", "replace", "status", "create"])
def test_conflicts_include_object_lifecycle_and_status_writes(other):
    """
    Whole-object conflicts include status revisions, deletion and named creation.
    """

    async def run():
        api, _ = adapter()
        method, name, body, status = "PUT", "workers", scale(5), False
        if other == "delete":
            method, body = "DELETE", {"preconditions": {"uid": "original", "resourceVersion": "10"}}
        elif other == "status":
            method, body, status = "PATCH", {"metadata": {"resourceVersion": "10"}, "status": {"ready": True}}, True
        elif other == "create":
            method, name, body = "POST", "", {"metadata": {"name": "workers"}, "spec": {"replicas": 5}}
        async with api.write_lock:
            tasks = [
                asyncio.create_task(api.request("PATCH", "Deployment", "test", "workers", scale(4))),
                asyncio.create_task(api.request(method, "Deployment", "test", name, body, status=status)),
            ]
            await asyncio.sleep(0)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, WriteConflict) for result in results)
        api.client.call_api.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    "change,reason",
    [("uid", "queued_write_uid_changed"), ("revision", "queued_write_revision_changed"), ("missing", "queued_write_target_absent")],
)
def test_delayed_write_rechecks_its_original_fences(change, reason):
    """
    A queue delay cannot silently rebase an old adjustment onto an updated or replaced target.
    """

    async def run():
        api, state = adapter()
        async with api.write_lock:
            task = asyncio.create_task(api.request("PATCH", "Deployment", "test", "workers", scale(4)))
            await asyncio.sleep(0)
            if change == "missing":
                api.client.call_api.side_effect = lambda *args, **kwargs: None
            else:
                state["metadata"]["uid" if change == "uid" else "resourceVersion"] = "replacement"
        with pytest.raises(WriteConflict) as error:
            await task
        assert error.value.conflict_reason == reason
        assert [call.args[1] for call in api.client.call_api.call_args_list] == ["GET"]
        assert api.writes.snapshot()["total"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("method", ["PATCH", "PUT", "DELETE", "POST"])
def test_delayed_unfenced_updates_and_existing_creates_do_not_dispatch(method):
    """
    Blind queued updates and creates of an existing name require refreshed intent.
    """

    async def run():
        api, _ = adapter()
        body = {"metadata": {"name": "workers"}, "spec": {"replicas": 4}}
        async with api.write_lock:
            task = asyncio.create_task(api.request(method, "Deployment", "test", "" if method == "POST" else "workers", body))
            await asyncio.sleep(0)
        with pytest.raises(WriteConflict):
            await task
        assert all(call.args[1] == "GET" for call in api.client.call_api.call_args_list)
        assert api.writes.snapshot()["total"] == 0

    asyncio.run(run())


def test_matching_delayed_fence_is_checked_before_guard_and_dispatch():
    """
    Recheck state and ownership while the write is still queued and preserve its body snapshot.
    """

    async def run():
        api, state = adapter()
        checks = []

        async def guard():
            checks.append(api.writes.snapshot())
            assert api.client.call_api.call_args.args[1] == "GET"

        api.before_write = guard
        body = scale(4)
        async with api.write_lock:
            task = asyncio.create_task(api.request("PATCH", "Deployment", "test", "workers", body))
            await asyncio.sleep(0)
            body["spec"]["replicas"] = 99
        await task
        assert len(checks) == 1 and checks[0]["queued"] == 1 and checks[0]["inFlight"] == 0
        assert state["spec"]["replicas"] == 4
        assert [call.args[1] for call in api.client.call_api.call_args_list] == ["GET", "PATCH"]

    asyncio.run(run())


def test_conflict_arriving_during_ownership_check_prevents_dispatch():
    """
    The first lock holder remains pending until asynchronous authorization finishes.
    """

    async def run():
        api, _ = adapter()
        started, release = asyncio.Event(), asyncio.Event()

        async def guard():
            started.set()
            await release.wait()

        api.before_write = guard
        first = asyncio.create_task(api.request("PATCH", "Deployment", "test", "workers", scale(4)))
        await started.wait()
        second = asyncio.create_task(api.request("PATCH", "Deployment", "test", "workers", scale(2)))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, WriteConflict) for result in results)
        api.client.call_api.assert_not_called()

    asyncio.run(run())


def test_cancelled_pending_write_releases_its_conflict_identity():
    """
    Cancellation before dispatch leaves no phantom intent to reject a later valid request.
    """

    async def run():
        api, state = adapter()
        async with api.write_lock:
            task = asyncio.create_task(api.request("PATCH", "Deployment", "test", "workers", scale(4)))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert api.writes.snapshot()["total"] == 0
        assert not api.pending_writes.entries and not api.pending_writes.targets
        await api.request("PATCH", "Deployment", "test", "workers", scale(2))
        assert state["spec"]["replicas"] == 2

    asyncio.run(run())


def test_repeated_cancellation_does_not_abandon_inflight_scale():
    """
    An opposing pending write waits for transport, then rejects its stale original revision.
    """

    async def run():
        api, state = adapter()
        original = api.client.call_api.side_effect
        started, release = threading.Event(), threading.Event()

        def blocked(path, method, **kwargs):
            if method == "PATCH":
                started.set()
                assert release.wait(5)
            return original(path, method, **kwargs)

        api.client.call_api.side_effect = blocked
        first = asyncio.create_task(api.request("PATCH", "Deployment", "test", "workers", scale(4)))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            second = asyncio.create_task(api.request("PATCH", "Deployment", "test", "workers", scale(2)))
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)
            assert api.writes.snapshot()["inFlight"] == 1
            assert api.writes.snapshot()["queued"] == 1
            assert not first.done() and not second.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(WriteConflict) as error:
                await second
            assert error.value.conflict_reason == "queued_write_revision_changed"
            assert state["spec"]["replicas"] == 4
            assert [call.args[1] for call in api.client.call_api.call_args_list] == ["PATCH", "GET"]
            assert api.writes.snapshot()["total"] == 0
        finally:
            release.set()

    asyncio.run(run())


@pytest.mark.parametrize("different", ["name", "namespace", "kind", "cluster"])
def test_disjoint_targets_do_not_conflict(different):
    """
    Resource identities include kind, namespace and adapter cluster as well as name.
    """

    async def run():
        api, _ = adapter()
        other = adapter()[0] if different == "cluster" else api

        # Separate target state is irrelevant here; each GET proves the same expected fence.
        api.client.call_api.side_effect = lambda *args, **kwargs: {"metadata": {"uid": "original", "resourceVersion": "10"}}
        other.client.call_api.side_effect = api.client.call_api.side_effect
        async with api.write_lock:
            first = asyncio.create_task(api.request("PATCH", "Deployment", "test", "workers", scale(4)))
            second = asyncio.create_task(
                other.request(
                    "PATCH",
                    "StatefulSet" if different == "kind" else "Deployment",
                    "other" if different == "namespace" else "test",
                    "other" if different == "name" else "workers",
                    scale(2),
                )
            )
            await asyncio.sleep(0)
        await asyncio.gather(first, second)
        assert api.writes.snapshot()["total"] == 0
        assert not api.pending_writes.entries

    asyncio.run(run())


def test_conflict_logs_never_expose_secret_bodies(caplog):
    """
    Queue conflict diagnosis contains resource identities and reasons, not credentials.
    """

    async def run():
        api, _ = adapter()
        async with api.write_lock:
            tasks = [
                asyncio.create_task(
                    api.request("PATCH", "Secret", "test", "auth", {"metadata": {"resourceVersion": "10"}, "data": {"key": value}})
                )
                for value in ("private-first", "private-second")
            ]
            await asyncio.sleep(0)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, WriteConflict) for result in results)
        api.client.call_api.assert_not_called()

    with caplog.at_level(logging.DEBUG, logger="polyad"):
        asyncio.run(run())
    assert "private-first" not in caplog.text and "private-second" not in caplog.text
    assert "write_deferred" in {getattr(record, "event_name", "").removeprefix("polyad.kubernetes.") for record in caplog.records}


def test_identical_pending_writes_share_one_dispatch_and_independent_responses():
    """
    Equivalent intent shares one queue entry and one acknowledgement without returning a shared mutable result.
    """

    async def run():
        api, state = adapter()
        async with api.write_lock:
            tasks = [asyncio.create_task(api.request("PATCH", "Deployment", "test", "workers", scale(4))) for _ in range(2)]
            await asyncio.sleep(0)
        first, second = await asyncio.gather(*tasks, return_exceptions=True)
        assert first["spec"]["replicas"] == 4
        assert second == first and second is not first
        first["spec"]["replicas"] = 99
        assert second["spec"]["replicas"] == 4
        assert state["spec"]["replicas"] == 4
        assert [call.args[1] for call in api.client.call_api.call_args_list] == ["GET", "PATCH"]
        assert not api.pending_writes.targets

    asyncio.run(run())


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_delayed_create_and_delete_preserve_absence_semantics(method):
    """
    Absent targets permit named creation and retain idempotent fenced deletion.
    """

    async def run():
        api, _ = adapter()
        api.client.call_api.side_effect = lambda *args, **kwargs: None
        body = {"metadata": {"name": "workers"}} if method == "POST" else {"preconditions": {"uid": "original"}}
        async with api.write_lock:
            task = asyncio.create_task(api.request(method, "Deployment", "test", "" if method == "POST" else "workers", body))
            await asyncio.sleep(0)
        await task
        assert [call.args[1] for call in api.client.call_api.call_args_list] == ["GET", method]
        assert api.writes.snapshot()["total"] == 0
        assert not api.pending_writes.targets

    asyncio.run(run())


@pytest.mark.parametrize(
    "case",
    [
        ("GET", "Deployment", "test", "workers", None, {}),
        ("POST", "TokenReview", "", "", {"spec": {"token": "private"}}, {}),
        ("POST", "SubjectAccessReview", "", "", {"spec": {}}, {}),
        ("POST", "Deployment", "test", "", {"metadata": {"generateName": "worker-"}}, {}),
        ("PATCH", "Deployment", "test", "workers", scale(4), {"query": [("dryRun", "All")]}),
    ],
)
def test_requests_without_persistent_named_effects_are_not_compared(case):
    """
    Authentication, dry runs, generated names and reads do not invalidate named intent.
    """
    method, kind, namespace, name, body, options = case
    assert write_intent(method, kind, namespace, name, body, **options) is None
