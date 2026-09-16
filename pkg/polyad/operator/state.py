"""
Persist optional, cluster-qualified graph observations in PostgreSQL.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from polyad.operator.health import credential_token

if TYPE_CHECKING:
    from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS polyad_state_version (version integer PRIMARY KEY);
INSERT INTO polyad_state_version VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS polyad_graph_state (
    scope text NOT NULL, cluster text NOT NULL, namespace text NOT NULL,
    kind text NOT NULL, name text NOT NULL, uid text NOT NULL,
    resource_version text NOT NULL, observed_at timestamptz NOT NULL,
    document jsonb NOT NULL,
    PRIMARY KEY (scope, cluster, namespace, kind, name)
);
CREATE TABLE IF NOT EXISTS polyad_namespace_state (
    scope text NOT NULL, cluster text NOT NULL, namespace text NOT NULL,
    observed_at timestamptz NOT NULL, snapshot jsonb NOT NULL,
    PRIMARY KEY (scope, cluster, namespace)
);
"""


def state_document(obj: dict[str, Any]) -> dict[str, Any]:
    """
    Store graph intent, rules and tracked status without copying workload credentials.

    Args:
        obj (dict[str, Any]): Observed Polyad resource, never a Kubernetes Secret.

    Returns:
        dict[str, Any]: Identity, graph specification and observed controller parameters.
    """
    meta = obj["metadata"]
    document = {
        "apiVersion": obj.get("apiVersion", "polyad.astrivant.com/v1alpha1"),
        "kind": obj["kind"],
        "metadata": {
            key: meta[key]
            for key in ("name", "namespace", "uid", "generation", "resourceVersion", "ownerReferences", "deletionTimestamp")
            if key in meta
        },
        "status": obj.get("status", {}),
    }
    # Workload/Resource/Composition definitions may embed Secrets or plaintext env.
    # Their identity and observed parameters belong here; their manifests remain in Kubernetes.
    if obj["kind"] in {"Graph", "PolyGraph", "ReplicaGroup", "GraphRule", "OperatorPool", "RemoteScale"}:
        document["spec"] = obj.get("spec", {})
    return document


class StateStore:
    """
    Commit complete inventories atomically and reject superseded scans across HA replicas.
    """

    def __init__(self, dsn: str, scope: str) -> None:
        """
        Bound database connections and query latency independently of Kubernetes leases.

        Args:
            dsn (str): PostgreSQL connection string from a mounted Secret.
            scope (str): Root control-plane identity within a shared database.
        """
        self.scope = scope
        self.application = "polyad-" + hashlib.sha256(scope.encode()).hexdigest()[:24]
        self.pool = AsyncConnectionPool(
            dsn,
            min_size=0,
            max_size=2,
            timeout=5,
            open=False,
            kwargs={
                "connect_timeout": 5,
                "application_name": self.application,
                "options": "-c statement_timeout=5000 -c lock_timeout=4000",
            },
            check=AsyncConnectionPool.check_connection,
        )
        self.lock = asyncio.Lock()
        self.initialized = False
        self.last_success = 0.0

    @classmethod
    def from_environment(cls, namespace: str) -> StateStore | None:
        """
        Leave database connections entirely disabled unless explicitly configured.

        Args:
            namespace (str): Default root identity for the optional database.

        Returns:
            StateStore | None: Configured store, or None for the default deployment.
        """
        if os.environ.get("POLYAD_POSTGRES_ENABLED", "false").lower() != "true":
            return None
        dsn = credential_token("POSTGRES", setting="DSN").strip()
        if not dsn:
            raise ValueError("PostgreSQL enablement requires a DSN Secret")
        return cls(dsn, os.environ.get("POLYAD_STATE_SCOPE", namespace))

    async def start(self) -> None:
        """
        Serialize the initial schema transaction across all processes using this database.

        Returns:
            None: Failed initialization can be retried on the next observation.
        """
        async with self.lock:
            if self.initialized:
                return
            await self.pool.open()
            async with self.pool.connection() as connection:
                await connection.execute("SELECT pg_advisory_xact_lock(782341, 1)")
                await connection.execute(SCHEMA)
                cursor = await connection.execute("SELECT version FROM polyad_state_version")
                if await cursor.fetchall() != [(1,)]:
                    raise RuntimeError("unsupported Polyad database schema version")
            self.initialized = True

    async def begin(self) -> Any:
        """
        Obtain a database-clock ticket before reading Kubernetes for a complete scan.

        Returns:
            Any: Server timestamp used to reject observations begun before a committed scan.
        """
        await self.start()
        async with self.pool.connection() as connection:
            cursor = await connection.execute("SELECT clock_timestamp()")
            row = await cursor.fetchone()
            assert row is not None
            return row[0]

    async def save(self, cluster: str, namespace: str, started: Any, objects: list[dict[str, Any]], snapshot: dict[str, Any]) -> bool:
        """
        Replace one complete namespace observation, including removals and tracked parameters.

        Args:
            cluster (str): Registered cluster identity; local deployments use local.
            namespace (str): Namespace scanned in that cluster.
            started (Any): Database timestamp acquired before the scan.
            objects (list[dict[str, Any]]): All objects from a successful complete scan.
            snapshot (dict[str, Any]): Inventory and queue observations, including freshness fields.

        Returns:
            bool: Whether this observation replaced the previous complete scan.
        """
        identity = self.scope, cluster, namespace
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """INSERT INTO polyad_namespace_state VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (scope, cluster, namespace) DO UPDATE
                SET observed_at = EXCLUDED.observed_at, snapshot = EXCLUDED.snapshot
                WHERE polyad_namespace_state.observed_at <= EXCLUDED.observed_at
                RETURNING observed_at""",
                (*identity, started, Jsonb(snapshot)),
            )
            if await cursor.fetchone() is None:
                return False
            # The row lock above serializes replacements, including a now-empty namespace.
            await connection.execute("DELETE FROM polyad_graph_state WHERE scope = %s AND cluster = %s AND namespace = %s", identity)
            async with connection.cursor() as cursor:
                await cursor.executemany(
                    "INSERT INTO polyad_graph_state VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            *identity,
                            obj["kind"],
                            obj["metadata"]["name"],
                            obj["metadata"]["uid"],
                            obj["metadata"]["resourceVersion"],
                            started,
                            Jsonb(state_document(obj)),
                        )
                        for obj in objects
                    ],
                )
        self.last_success = time.monotonic()
        return True

    async def connections(self) -> dict[str, Any]:
        """
        Sample all root-operator sessions on the primary without scrape-time database access.

        Returns:
            dict[str, Any]: Global connection count, or an explicitly unavailable sample.
        """
        try:
            await self.start()
            async with self.pool.connection() as connection:
                cursor = await connection.execute(
                    """SELECT count(*) FROM pg_stat_activity
                    WHERE datname = current_database() AND application_name = %s
                    AND backend_type = 'client backend'""",
                    (self.application,),
                )
                row = await cursor.fetchone()
                assert row is not None
            return {"enabled": True, "fresh": True, "connections": row[0], "stateFresh": time.monotonic() - self.last_success < 30}
        except Exception:
            return {"enabled": True, "fresh": False, "connections": None, "stateFresh": False}

    async def close(self) -> None:
        """
        Release database connections without deleting durable state.

        Returns:
            None: No return value.
        """
        await self.pool.close()
