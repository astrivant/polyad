"""
Persist optional credential verification records without copying bearer secrets to a cache.
"""

from __future__ import annotations

import hashlib
import json
import os
from threading import Lock
from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from polyad.operator.lifecycle.health import credential_token
from polyad.sql import statement
from polyad_types.codec import to_dict

if TYPE_CHECKING:
    from polyad_types.auth import APIKey


class CredentialStore:
    """
    Require database authorization when optional durable credential storage is enabled.
    """

    def __init__(self, dsn: str, scope: str) -> None:
        """
        Create a bounded synchronous pool for Flask and outbound request workers.

        Args:
            dsn (str): Secret-supplied connection string, never persisted or logged.
            scope (str): Control-plane identity within this database.
        """
        self.scope = scope
        self.lock = Lock()
        self.initialized = False
        self.pool = ConnectionPool(
            dsn,
            min_size=0,
            max_size=2,
            timeout=5,
            kwargs={
                "connect_timeout": 5,
                "options": "-c statement_timeout=5000 -c lock_timeout=4000",
                "application_name": "polyad-" + hashlib.sha256(scope.encode()).hexdigest()[:24],
            },
        )

    @classmethod
    def from_environment(cls) -> CredentialStore | None:
        """
        Use a separately mounted database credential only when explicitly configured.

        Returns:
            CredentialStore | None: Optional durable authorization store.
        """
        path = os.environ.get("POLYAD_AUTH_DATABASE_DSN_FILE")
        if not path:
            return None
        return cls(
            credential_token("AUTH_DATABASE", setting="DSN").strip(),
            os.environ.get("POLYAD_STATE_SCOPE", os.environ.get("POLYAD_NAMESPACE", "default")),
        )

    def permitted(self, group: str, key: APIKey, token: str) -> bool:
        """
        Record a one-way verifier and honor durable per-lane revocation.

        Args:
            group (str): Services or operators credential group.
            key (APIKey): Mounted nonsecret policy for this credential revision.
            token (str): High-entropy bearer value held only in process memory.

        Returns:
            bool: Whether the database permits this mounted credential lane.
        """
        policy = to_dict(key)
        digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
        verifier = hashlib.sha256(token.encode()).hexdigest()
        identity = (self.scope, group, key.name)
        with self.lock:
            if not self.initialized:
                with self.pool.connection() as connection:
                    connection.execute(statement("advisory-lock.sql"), (782341, 2))
                    connection.execute(statement("authentication/schema.sql"))
                self.initialized = True
        with self.pool.connection() as connection:
            connection.execute(statement("authentication/ensure-lane.sql"), identity)
            row = connection.execute(statement("authentication/lane-disabled.sql"), identity).fetchone()
            if row is None or row[0]:
                return False
            connection.execute(
                statement("authentication/record-key.sql"),
                (*identity, verifier, digest, Jsonb(policy)),
            )
        return True

    def close(self) -> None:
        """
        Release database connections when the listener stops.

        Returns:
            None: No credentials are retained in shared cache storage.
        """
        self.pool.close()
