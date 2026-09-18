"""
Verify connection budgets, occupancy telemetry and one autoscaler per target.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from weakref import WeakKeyDictionary

import pytest
from flask import Flask
from redis.connection import Connection

from polyad.api.http.limits import RateLimitPolicy, install_limits
from polyad.auth.lanes import Lanes
from polyad.cache import Cache
from polyad.operator.observability.pressure import collect, demand
from polyad.transport import pools
from polyad.transport.redis import pool
from polyad.transport.settings import DEFAULTS, postgres_options, settings
from tests.test_chart import render


@pytest.fixture(autouse=True)
def isolated_pools(monkeypatch):
    """
    Keep pool observations local to each test and leave process environment unchanged.
    """
    monkeypatch.setattr(pools, "_pools", WeakKeyDictionary())
    monkeypatch.delenv("POLYAD_CONNECTION_SETTINGS", raising=False)


@pytest.mark.parametrize(
    "value",
    [
        {"lanes": {"maxConnections": True}},
        {"cache": {"maxConnections": 0}},
        {"cache": {"maxConnections": 1.5}},
        {"cache": {"maxConnections": 4097}},
        {"rateLimits": {"socketTimeoutSeconds": float("nan")}},
        {"kubernetes": {"readTimeoutSeconds": 61}},
        {"state": {"minConnections": 3, "maxConnections": 2}},
        {"authentication": {"connectTimeoutSeconds": 0.5}},
        {"authentication": {"statementTimeoutMilliseconds": 0}},
        {"lanes": {"unrecognized": 1}},
        {"unrecognized": {}},
        [],
    ],
)
def test_invalid_environment_budgets_fail_before_connecting(monkeypatch, value):
    """
    Reject invalid direct environment input as well as Helm-validated input.
    """
    monkeypatch.setenv("POLYAD_CONNECTION_SETTINGS", json.dumps(value))
    with pytest.raises(ValueError):
        settings("cache")


def test_live_consumers_use_configured_pools_and_url_cannot_override_budgets(monkeypatch):
    """
    Exercise all Redis consumers without opening network sockets.
    """
    configured = {
        "cache": {"maxConnections": 12, "socketTimeoutSeconds": 3.5},
        "lanes": {"maxConnections": 7, "connectTimeoutSeconds": 1.5},
        "rateLimits": {"maxConnections": 4, "socketTimeoutSeconds": 1.25},
    }
    monkeypatch.setenv("POLYAD_CONNECTION_SETTINGS", json.dumps(configured))
    url = "rediss://user:password@example.invalid:6379/3?max_connections=999&socket_timeout=999"
    with patch("polyad.auth.lanes.Thread"):
        lanes = Lanes(url, "test")
    limiter = install_limits(Flask(__name__), RateLimitPolicy(namespace="test", storage_uri=url))
    cache = Cache(url, "test")
    assert cache.client.connection_pool.max_connections == 12
    assert cache.client.connection_pool.connection_kwargs["socket_timeout"] == 3.5
    assert lanes.client.connection_pool.max_connections == 7
    assert lanes.client.connection_pool.connection_kwargs["socket_connect_timeout"] == 1.5
    assert limiter.storage.storage.auto_close_connection_pool is True
    assert limiter.storage.storage.connection_pool.max_connections == 4
    assert limiter.storage.storage.connection_pool.connection_kwargs["socket_timeout"] == 1.25
    for client in (cache.client, lanes.client, limiter.storage.storage):
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["host"] == "example.invalid" and kwargs["db"] == 3
        assert kwargs["retry"]._retries == 0
    assert "password" not in json.dumps(pools.snapshot())
    lanes.client.close()
    limiter.storage.storage.close()
    asyncio.run(cache.close())


def test_database_consumers_preserve_timeout_and_application_settings(monkeypatch):
    """
    Pass independent state/authentication settings to the actual pool constructors.
    """
    from polyad.auth.store import CredentialStore
    from polyad.operator.adapters.postgresql import StateStore

    monkeypatch.setenv(
        "POLYAD_CONNECTION_SETTINGS",
        json.dumps({"state": {"maxConnections": 7, "poolTimeoutSeconds": 2.5}, "authentication": {"maxConnections": 4}}),
    )
    for cls, name, maximum in ((StateStore, "state", 7), (CredentialStore, "authentication", 4)):
        options = postgres_options(name)
        target = "polyad.operator.adapters.postgresql.AsyncConnectionPool" if name == "state" else "polyad.auth.store.ConnectionPool"
        with patch(target) as constructor:
            cls("postgresql://secret", "scope")
        call = constructor.call_args.kwargs
        assert call["max_size"] == maximum
        assert call["timeout"] == options["timeout"]
        assert call["kwargs"]["options"] == "-c statement_timeout=5000 -c lock_timeout=4000"
        assert call["kwargs"]["application_name"].startswith("polyad-")


def test_pool_demand_ignores_idle_connections_and_tracks_checkouts(monkeypatch):
    """
    Releasing actual Redis pool checkouts removes pressure even though sockets are retained.
    """

    class LocalConnection(Connection):
        def connect(self):
            pass

        def can_read(self):
            return False

    monkeypatch.setenv("POLYAD_CONNECTION_SETTINGS", '{"lanes":{"maxConnections":2}}')
    connection_pool = pool("redis://localhost", "lanes", connection_class=LocalConnection)
    first = connection_pool.get_connection()
    second = connection_pool.get_connection()
    assert pools.snapshot()["lanes"] == {"inUse": 2, "limit": 2, "waiting": 0}
    assert pools.pressure(pools.snapshot()) == 1
    connection_pool.release(first)
    connection_pool.release(second)
    assert pools.pressure(pools.snapshot()) == 0
    assert len(connection_pool._available_connections) == 2


def test_async_and_postgres_pool_statistics_include_queued_demand():
    """
    Observe event-loop checkouts and database waiters without making database reads.
    """

    class DatabasePool:
        def get_stats(self):
            return {"pool_size": 4, "pool_available": 1, "pool_max": 4, "requests_waiting": 2}

    async def scenario():
        database = DatabasePool()
        pools.register(database, "state", "postgresql")
        cache = pool("redis://localhost", "cache", asynchronous=True)
        cache.ensure_connection = AsyncMock()
        connection = await cache.get_connection()
        assert pools.snapshot()["cache"]["inUse"] == 1
        assert pools.pressure(pools.snapshot()) == 1.25
        await cache.release(connection)
        assert pools.snapshot()["cache"]["inUse"] == 0

    asyncio.run(scenario())


def test_connection_demand_is_summed_once_scoped_and_fails_closed():
    """
    Reject missing reports and exclude remote workers from local replica decisions.
    """

    async def scenario():
        entries = [
            {
                "component": "executor",
                "inFlight": 0,
                "requestsPerSecond": 0,
                "connectionPools": {"cache": {"inUse": 8, "waiting": 0, "limit": 10}},
            },
            {
                "component": "executor",
                "inFlight": 0,
                "requestsPerSecond": 0,
                "connectionPools": {"cache": {"inUse": 6, "waiting": 0, "limit": 10}},
            },
            {
                "component": "executor",
                "inFlight": 0,
                "requestsPerSecond": 0,
                "remoteWorker": True,
                "connectionPools": {"cache": {"inUse": 10, "waiting": 0, "limit": 10}},
            },
        ]
        shared = SimpleNamespace(
            prefix="test", client=SimpleNamespace(zrange=AsyncMock(return_value=["a", "b", "remote"]), mget=AsyncMock())
        )
        shared.client.mget.return_value = [json.dumps(entry) for entry in entries]
        snapshot = {"components": await collect(shared)}
        assert demand(snapshot, "executor", "connectionPressure") == pytest.approx(1.4)
        shared.client.mget.return_value[1] = None
        with pytest.raises(ValueError, match="stale"):
            demand({"components": await collect(shared)}, "executor", "connectionPressure")
        del entries[1]["connectionPools"]
        shared.client.mget.return_value = [json.dumps(entry) for entry in entries]
        with pytest.raises(ValueError, match="stale"):
            demand({"components": await collect(shared)}, "executor", "connectionPressure")

    asyncio.run(scenario())


def test_chart_defaults_match_runtime_and_reach_all_process_templates():
    """
    Carry the complete JSON projection into dense, split and observer containers.
    """
    configurations = [
        (),
        ("ha=true", "architecture.mode=Distributed", "api.enabled=true", "metrics.enabled=true"),
        ("observer.enabled=true", "observer.existingSecret=observer", "global.multiCluster.clusterName=local"),
    ]
    for configuration in configurations:
        objects = render(*configuration, "operator.connections.lanes.maxConnections=17")
        templates = [
            obj["spec"]["template"]
            for obj in objects
            if obj["kind"] in {"Deployment", "Daemon"} and obj["metadata"]["name"].startswith("test-")
        ]
        checked = 0
        for template in templates:
            for container in template.get("spec", template).get("containers", []):
                env = {entry["name"]: entry.get("value") for entry in container.get("env", [])}
                if "POLYAD_CONNECTION_SETTINGS" not in env:
                    continue
                projected = json.loads(env["POLYAD_CONNECTION_SETTINGS"])
                assert projected == {**DEFAULTS, "lanes": {**DEFAULTS["lanes"], "maxConnections": 17}}
                checked += 1
        assert checked >= (4 if "architecture.mode=Distributed" in configuration else 2 if "observer.enabled=true" in configuration else 1)


def test_keda_connection_scaling_replaces_hpa_and_preserves_cpu_memory():
    """
    Use one scaling owner with consistent replica ceilings and authenticated metrics.
    """
    objects = render(
        "ha=true",
        "operator.autoscaling.enabled=true",
        "operator.autoscaling.connections.enabled=true",
        "operator.autoscaling.targetMemoryUtilizationPercentage=80",
        "metrics.authentication.enabled=true",
        "metrics.authentication.existingSecret=metrics",
        "keda.authentication.enabled=true",
    )
    assert not any(obj["kind"] == "HorizontalPodAutoscaler" and obj["metadata"]["name"] == "test-polyad" for obj in objects)
    scaler = next(obj for obj in objects if obj["kind"] == "ScaledObject" and obj["metadata"]["name"] == "test-polyad")
    assert scaler["spec"]["minReplicaCount"] == 2 and scaler["spec"]["maxReplicaCount"] == 8
    assert "name" not in scaler["spec"]["advanced"]["horizontalPodAutoscalerConfig"]
    assert "annotations" not in scaler["metadata"]
    triggers = scaler["spec"]["triggers"]
    assert [trigger["type"] for trigger in triggers] == ["metrics-api", "cpu", "memory"]
    assert triggers[0]["metricType"] == "AverageValue"
    assert triggers[0]["metadata"]["targetValue"] == "0.7"
    assert triggers[0]["metadata"]["url"].endswith("/dense/connectionPressure")
    assert triggers[0]["authenticationRef"]["name"] == "test-polyad-metrics"
    baseline = render("ha=true", "operator.autoscaling.enabled=true")
    assert any(obj["kind"] == "HorizontalPodAutoscaler" and obj["metadata"]["name"] == "test-polyad" for obj in baseline)


def test_distributed_connection_scaling_uses_existing_graph_constrained_targets():
    """
    Add pressure to each component's existing ReplicaGroup ScaledObject.
    """
    objects = render(
        "ha=true",
        "architecture.mode=Distributed",
        "architecture.autoscaling=true",
        "api.enabled=true",
        "operator.autoscaling.connections.enabled=true",
    )
    scalers = [
        obj
        for obj in objects
        if obj["kind"] == "ScaledObject" and obj["metadata"]["name"] in {"test-gateway", "test-executor", "test-telemetry"}
    ]
    assert len(scalers) == 3
    for scaler in scalers:
        assert scaler["spec"]["scaleTargetRef"]["kind"] == "ReplicaGroup"
        assert sum(trigger["metadata"].get("url", "").endswith("/connectionPressure") for trigger in scaler["spec"]["triggers"]) == 1


@pytest.mark.parametrize(
    "setting",
    [
        "operator.connections.state.maxConnections=0",
        "operator.connections.authentication.minConnections=3",
        "operator.connections.lanes.socketTimeoutSeconds=0",
        "operator.autoscaling.connections.enabled=true",
    ],
)
def test_chart_rejects_invalid_connection_budgets(setting):
    """
    Fail Helm rendering for impossible capacities and scaling without an owner.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(setting)


def test_kubernetes_limits_configure_adapter_without_mutating_remote_credentials(monkeypatch):
    """
    Reuse remote credentials while keeping transport budgets and write ordering independent.
    """
    from kubernetes.client import Configuration

    from polyad.operator.adapters.kubernetes import API

    monkeypatch.setenv(
        "POLYAD_CONNECTION_SETTINGS",
        '{"kubernetes":{"poolSize":7,"connectTimeoutSeconds":1.5,"readTimeoutSeconds":8}}',
    )
    configuration = Configuration()
    original_size = configuration.connection_pool_maxsize
    with patch("polyad.operator.adapters.kubernetes.client.ApiClient") as constructor:
        adapter = API(configuration=configuration)
        projected = constructor.call_args.kwargs["configuration"]
        assert projected.connection_pool_maxsize == 7
        assert configuration.connection_pool_maxsize == original_size
    adapter.client = Mock()
    adapter.client.call_api.return_value = {"items": []}
    asyncio.run(adapter.request("GET", "ConfigMap", "test"))
    assert adapter.client.call_api.call_args.kwargs["_request_timeout"] == (1.5, 8)
    assert adapter.work_graph.max_in_flight == 1


def test_connection_metrics_are_published_on_existing_routes():
    """
    Serve pool gauges and fresh KEDA values through the single metrics application.
    """
    from polyad.api.metrics.builder import MetricsAPIBuilder
    from polyad.metrics.store import MetricsStore
    from tests.test_metrics_api import snapshot

    data = snapshot()
    data["connectionPools"] = {"lanes": {"inUse": 16, "limit": 32, "waiting": 0}}
    data["components"] = {
        "fresh": True,
        "roles": {"dense": {"requestsPerSecond": 0, "inFlight": 0, "connectionFresh": True, "connectionPressure": 1.5}},
    }
    store = MetricsStore()
    store.publish(data)
    client = MetricsAPIBuilder().with_store(store).build().test_client()
    assert client.get("/v1/components/dense/connectionPressure").json == {"value": 1.5, "fresh": True}
    rendered = client.get("/metrics").text
    assert "polyad_connection_pool_in_use{" in rendered
    assert 'pool="lanes"' in rendered
    assert "polyad_component_connection_pressure{" in rendered
    data["components"]["fresh"] = False
    store.publish(data)
    assert client.get("/v1/components/dense/connectionPressure").status_code == 503
