"""
Verify optional durable state, HA inventory ordering and connection-based scaling signals.
"""

from __future__ import annotations

import asyncio
import copy
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from polyad.metrics.builder import MetricsAPIBuilder
from polyad.metrics.store import MetricsStore
from polyad.operator.state import StateStore, state_document
from tests.test_metrics_api import snapshot
from tests.test_operator import resource


def test_postgresql_is_disabled_without_opt_in(monkeypatch):
    """
    A DSN alone must not open connections or enable state storage.
    """
    monkeypatch.delenv("POLYAD_POSTGRES_ENABLED", raising=False)
    monkeypatch.setenv("POLYAD_POSTGRES_DSN", "postgresql://unused")
    assert StateStore.from_environment("test") is None
    monkeypatch.setenv("POLYAD_POSTGRES_ENABLED", "true")
    monkeypatch.setenv("POLYAD_POSTGRES_DSN", "")
    with pytest.raises(ValueError, match="DSN Secret"):
        StateStore.from_environment("test")


def test_state_preserves_graph_parameters_and_excludes_embedded_credentials():
    """
    Graph intent and observed workload signals survive without copying raw workload manifests.
    """
    graph = resource("Graph", "pipeline", {"nodes": [], "rules": ["budget"]})
    graph["status"] = {"workloads": {"consumer": {"values": {"throughput": 42}}}, "structuralRules": [{"cheeger": 1}]}
    stored = state_document(graph)
    assert stored["spec"] == graph["spec"]
    assert stored["status"] == graph["status"]
    workload = resource("Daemon", "sensitive", {"template": {"spec": {"env": {"PASSWORD": "not-persisted"}}}})
    workload["metadata"]["annotations"] = {"last-applied": "not-persisted"}
    assert "not-persisted" not in str(state_document(workload))


def test_superseded_scan_cannot_replace_inventory():
    """
    An older HA scan must leave both the newer namespace and graph rows untouched.
    """

    async def scenario():
        store = StateStore("postgresql://unused", "test")
        connection = AsyncMock()
        connection.execute.return_value.fetchone.return_value = None
        pool = MagicMock()
        pool.connection.return_value.__aenter__ = AsyncMock(return_value=connection)
        pool.connection.return_value.__aexit__ = AsyncMock(return_value=None)
        store.pool = pool
        assert not await store.save("west", "apps", "older", [], {})
        assert connection.execute.await_count == 1
        assert "observed_at <= EXCLUDED.observed_at" in connection.execute.call_args.args[0]

    asyncio.run(scenario())


def test_connection_metrics_fail_closed_and_remain_cached():
    """
    KEDA reads a global connection count from operator memory and cannot turn failure into zero.
    """
    store = MetricsStore()
    app = MetricsAPIBuilder().with_store(store).with_bearer_token("metrics-token").build().test_client()
    path = "/v1/postgresql/connections"
    headers = {"Authorization": "Bearer metrics-token"}
    assert app.get(path).status_code == 401
    for postgres in ({}, {"enabled": True, "fresh": False, "stateFresh": False, "connections": None}):
        store.publish({**snapshot(), "postgresql": postgres})
        assert app.get(path, headers=headers).status_code == 503
    store.publish({**snapshot(), "postgresql": {"enabled": True, "fresh": True, "stateFresh": True, "connections": 61}})
    assert app.get(path, headers=headers).json == {"value": 61, "fresh": True}
    assert b"polyad_postgresql_connections" in store.read()[0]


@pytest.mark.skipif(not os.environ.get("POLYAD_TEST_POSTGRES_DSN"), reason="requires a PostgreSQL test database")
def test_postgresql_commits_state_rejects_old_scans_and_removes_deleted_graphs():
    """
    Exercise real transactions, primary session counting, restart reads and failed-write rollback.
    """

    async def scenario():
        scope = "test-" + uuid.uuid4().hex
        store = StateStore(os.environ["POLYAD_TEST_POSTGRES_DSN"], scope)
        other = StateStore(os.environ["POLYAD_TEST_POSTGRES_DSN"], scope)
        try:
            await asyncio.gather(store.start(), other.start())
            old = await store.begin()
            newer = await other.begin()
            graph = resource("Graph", "pipeline", {"nodes": [], "rules": ["budget"]})
            graph["status"] = {"workloads": {"consumer": {"values": {"throughput": 42}}}}
            assert await other.save("west", "test", newer, [graph], {"parameters": {"throughput": 42}})
            assert not await store.save("west", "test", old, [], {})
            async with store.pool.connection() as connection:
                cursor = await connection.execute("SELECT document FROM polyad_graph_state WHERE scope = %s", (scope,))
                rows = await cursor.fetchall()
                assert rows == [(state_document(graph),)]
            # SQL deletion and a broken insert must roll back together.
            invalid = copy.deepcopy(graph)
            del invalid["metadata"]["uid"]
            with pytest.raises(KeyError):
                await store.save("west", "test", await store.begin(), [invalid], {})
            async with other.pool.connection() as connection:
                cursor = await connection.execute("SELECT count(*) FROM polyad_graph_state WHERE scope = %s", (scope,))
                assert (await cursor.fetchone())[0] == 1
            assert (await store.connections())["connections"] >= 2
            assert await store.save("east", "test", await store.begin(), [graph], {})
            assert await store.save("west", "test", await store.begin(), [], {})
            await other.close()
            other = StateStore(os.environ["POLYAD_TEST_POSTGRES_DSN"], scope)
            await other.start()
            async with other.pool.connection() as connection:
                cursor = await connection.execute("SELECT cluster FROM polyad_graph_state WHERE scope = %s", (scope,))
                assert await cursor.fetchall() == [("east",)]
        finally:
            async with store.pool.connection() as connection:
                for table in ("polyad_graph_state", "polyad_namespace_state"):
                    await connection.execute(f"DELETE FROM {table} WHERE scope = %s", (scope,))
            await store.close()
            await other.close()

    asyncio.run(scenario())
