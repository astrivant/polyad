"""
Exercise cache scale admission, ownership fences and unavailable connection samples.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kubernetes.client.exceptions import ApiException

from polyad.metrics.builder import MetricsAPIBuilder
from polyad.metrics.store import MetricsStore
from polyad.operator.coordination import dragonfly
from polyad.operator.coordination.leases import Coordinator, NotOwner
from tests.test_coordination import LeaseAPI
from tests.test_metrics_api import samples, snapshot
from tests.test_operator import resource
from tests.test_root_control_plane import ManagementAPI


def cache_api(current=2, desired=4):
    """
    Supply a healthy upstream cache and its owned StatefulSet independently of scale intent.
    """
    pool = resource("DragonflyPool", "queue", {"replicas": desired, "minReplicas": 2, "maxReplicas": 5})
    cache = resource("Dragonfly", "queue", {"replicas": current})
    cache["apiVersion"] = "dragonflydb.io/v1alpha1"
    cache["status"] = {"phase": "ready"}
    sts = resource("StatefulSet", "queue", {"replicas": current, "selector": {"matchLabels": {"app": "queue"}}})
    sts["metadata"]["ownerReferences"] = [{"uid": cache["metadata"]["uid"], "controller": True}]
    sts["status"] = {
        "replicas": current,
        "readyReplicas": current,
        "observedGeneration": 1,
        "currentRevision": "v1",
        "updateRevision": "v1",
    }
    return ManagementAPI(pool, cache, sts)


@pytest.mark.parametrize(("current", "desired", "next_count"), [(2, 5, 3), (5, 2, 4), (2, 2, 2)])
def test_replica_steps_wait_for_observed_readiness(current, desired, next_count):
    """
    Only upstream replica intent changes, and another step waits for observed convergence.
    """

    async def scenario():
        api = cache_api(current, desired)
        await dragonfly.reconcile(api, "test", "queue")
        assert api.objects[("Dragonfly", "test", "queue")]["spec"]["replicas"] == next_count
        status = api.objects[("DragonflyPool", "test", "queue")]["status"]
        assert status["replicas"] == current
        assert status["readyReplicas"] == current
        assert status["labelSelector"] == "app=queue"
        assert status["phase"] == ("Ready" if current == desired else "WaitingForReplication")
        calls = list(api.calls)
        await dragonfly.reconcile(api, "test", "queue")
        assert api.calls == calls
        assert all(kind != "StatefulSet" for _, kind, _ in api.calls)

    asyncio.run(scenario())


@pytest.mark.parametrize("condition", ["unready", "generation", "rolling", "replication", "revision", "desired"])
def test_incomplete_replication_or_rollout_holds_scale_intent(condition):
    """
    A fresh read must confirm Pod counts, upstream readiness and rollout completion.
    """

    async def scenario():
        api = cache_api()
        cache = api.objects[("Dragonfly", "test", "queue")]
        sts = api.objects[("StatefulSet", "test", "queue")]
        if condition == "unready":
            sts["status"]["readyReplicas"] = 1
        elif condition == "generation":
            sts["metadata"]["generation"] = 2
        elif condition == "rolling":
            cache["status"]["isRollingUpdate"] = True
        elif condition == "replication":
            cache["status"]["phase"] = "configuring-replication"
        elif condition == "revision":
            sts["status"]["updateRevision"] = "v2"
        else:
            sts["spec"]["replicas"] = 3
        await dragonfly.reconcile(api, "test", "queue")
        assert api.calls == [("PATCH", "DragonflyPool", "queue")]
        assert api.objects[("DragonflyPool", "test", "queue")]["status"]["phase"] == "WaitingForReplication"

    asyncio.run(scenario())


def test_invalid_bounds_foreign_children_and_conflicts_reject_mutations():
    """
    Invalid intent, replaced owners and edits between reads and writes cannot scale the cache.
    """

    async def scenario():
        api = cache_api(desired=1)
        with pytest.raises(ValueError, match="bounds"):
            await dragonfly.reconcile(api, "test", "queue")
        assert not api.calls
        api = cache_api()
        api.objects[("StatefulSet", "test", "queue")]["metadata"]["ownerReferences"][0]["uid"] = "replaced"
        with pytest.raises(ValueError, match="own"):
            await dragonfly.reconcile(api, "test", "queue")
        assert not api.calls
        api = cache_api()
        request = api.request

        async def concurrent(method, kind, namespace, name="", body=None, **kwargs):
            if kind == "Dragonfly":
                api.objects[(kind, namespace, name)]["metadata"]["resourceVersion"] = "2"
            return await request(method, kind, namespace, name, body, **kwargs)

        api.request = concurrent
        with pytest.raises(ApiException) as failure:
            await dragonfly.reconcile(api, "test", "queue")
        assert failure.value.status == 409
        assert api.objects[("Dragonfly", "test", "queue")]["spec"]["replicas"] == 2

    asyncio.run(scenario())


def test_cache_scaler_rechecks_lease_before_every_write(monkeypatch):
    """
    Dedicated lease ownership remains mandatory even after a successful claim.
    """

    async def scenario():
        coordinator = Coordinator(LeaseAPI(), "test", "owner")
        shared = SimpleNamespace(ping=AsyncMock())
        closed = []

        def api_factory(before_write):
            return SimpleNamespace(before_write=before_write, client=SimpleNamespace(close=lambda: closed.append(True)))

        async def attempt(api, namespace, name):
            await api.before_write()
            coordinator.api.objects[("Lease", "test", "polyad-cache-queue")]["spec"]["holderIdentity"] = "successor"
            with pytest.raises(NotOwner):
                await api.before_write()
            raise asyncio.CancelledError

        monkeypatch.setattr(dragonfly, "API", api_factory)
        monkeypatch.setattr(dragonfly, "reconcile", attempt)
        with pytest.raises(asyncio.CancelledError):
            await dragonfly.run(coordinator, shared, "queue")
        assert closed == [True]
        assert shared.ping.await_count == 2

    asyncio.run(scenario())


def test_primary_connection_metrics_fail_closed_and_recover():
    """
    Successful scrapes are cached; outages and demoted primaries never become zero demand.
    """

    async def scenario():
        client = SimpleNamespace(
            info=AsyncMock(return_value={"role": "master", "connected_clients": 125}),
            connection_pool=SimpleNamespace(disconnect=AsyncMock()),
        )
        shared = SimpleNamespace(client=client)
        store = MetricsStore()
        app = MetricsAPIBuilder(store, token="secret").build().test_client()
        headers = {"Authorization": "Bearer secret"}
        data = snapshot()
        for fail in (False, True, False):
            client.info.side_effect = ConnectionError() if fail else None
            data["dragonfly"] = await dragonfly.connections(shared)
            store.publish(data)
            response = app.get("/v1/dragonfly/connections", headers=headers)
            assert response.status_code == (503 if fail else 200)
            if not fail:
                assert response.json == {"value": 125, "fresh": True}
            series = [sample.value for sample in samples(store) if sample.name == "polyad_dragonfly_connections"]
            assert series == ([] if fail else [125])
        client.info.return_value = {"role": "slave", "connected_clients": 2}
        assert await dragonfly.connections(shared) == {"enabled": True, "fresh": False}
        client.connection_pool.disconnect.assert_awaited_once()
        assert app.get("/v1/dragonfly/connections").status_code == 401
        before = client.info.await_count
        assert app.get("/v1/dragonfly/connections", headers=headers).status_code == 200
        assert client.info.await_count == before
        store.published = (0, *store.published[1:])
        assert app.get("/v1/dragonfly/connections", headers=headers).status_code == 503

    asyncio.run(scenario())
