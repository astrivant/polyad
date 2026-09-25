"""
Persist optional, cluster-qualified graph observations in PostgreSQL.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from polyad.operator.adapters.interfaces import StateBackend
from polyad.operator.lifecycle.health import credential_token
from polyad.sql import record_cipher, statement
from polyad.transport.pools import register
from polyad.transport.settings import postgres_options

if TYPE_CHECKING:
    from typing import Any

__all__ = (
    "StateStore",
    "state_document",
)


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
    if obj["kind"] in {"Graph", "PolyGraph", "ReplicaGroup", "GraphPolicy", "OperatorPool", "RemoteScale"}:
        document["spec"] = obj.get("spec", {})
    return document


class StateStore(StateBackend):
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
        self.cipher = record_cipher()
        self.application = "polyad-" + hashlib.sha256(scope.encode()).hexdigest()[:24]
        options = postgres_options("state")
        options["kwargs"]["application_name"] = self.application
        self.pool = AsyncConnectionPool(
            dsn,
            open=False,
            check=AsyncConnectionPool.check_connection,
            **options,
        )
        register(self.pool, "state", "postgresql")
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
                await connection.execute(statement("advisory-lock.sql"), (782341, 1))
                await connection.execute(statement("state/schema.sql"))
                cursor = await connection.execute(statement("state/schema-version.sql"))
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
            cursor = await connection.execute(statement("state/begin-scan.sql"))
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
                statement("state/save-namespace.sql"),
                (*identity, started, Jsonb(self.cipher.encrypt(snapshot, "polyad_namespace_state", identity) if self.cipher else snapshot)),
            )
            if await cursor.fetchone() is None:
                return False

            # The row lock above serializes replacements, including a now-empty namespace.
            await connection.execute(statement("state/delete-graphs.sql"), identity)
            async with connection.cursor() as cursor:
                await cursor.executemany(
                    statement("state/insert-graph.sql"),
                    [
                        (
                            *identity,
                            obj["kind"],
                            obj["metadata"]["name"],
                            obj["metadata"]["uid"],
                            obj["metadata"]["resourceVersion"],
                            started,
                            Jsonb(
                                self.cipher.encrypt(
                                    state_document(obj),
                                    "polyad_graph_state",
                                    (
                                        *identity,
                                        obj["kind"],
                                        obj["metadata"]["name"],
                                        obj["metadata"]["uid"],
                                        obj["metadata"]["resourceVersion"],
                                    ),
                                )
                                if self.cipher
                                else state_document(obj)
                            ),
                        )
                        for obj in objects
                    ],
                )
        self.last_success = time.monotonic()
        return True

    async def record_event(self, payload: dict[str, Any]) -> None:
        """
        Archive approved observations with deduplication and bounded retention cleanup.

        Args:
            payload (dict[str, Any]): Public observation or topology event, excluding workload manifests and Secrets.

        Returns:
            None: Committed before live delivery; failed archive writes are retried during reconciliation.
        """
        if os.environ.get("POLYAD_POSTGRES_EVENTS_ENABLED", "true").lower() != "true":
            return
        days = int(os.environ.get("POLYAD_POSTGRES_EVENTS_RETENTION_DAYS", "30"))
        if not 1 <= days <= 3650:
            raise ValueError("PostgreSQL event retention must be between 1 and 3650 days")
        await self.start()
        identity = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async with self.pool.connection() as connection:
            await connection.execute(
                statement("state/insert-event.sql"),
                (
                    self.scope,
                    identity,
                    Jsonb(self.cipher.encrypt(payload, "polyad_event_history", (self.scope, identity)) if self.cipher else payload),
                ),
            )
            await connection.execute(
                statement("state/prune-events.sql"),
                (self.scope, self.scope, days),
            )

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
                    statement("state/connections.sql"),
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
