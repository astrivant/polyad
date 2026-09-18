"""
Read administrator connection budgets without importing optional database drivers.
"""

from __future__ import annotations

import json
import math
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

REDIS_DEFAULTS = {"maxConnections": 32, "connectTimeoutSeconds": 5, "socketTimeoutSeconds": 5}
POSTGRES_DEFAULTS = {
    "minConnections": 0,
    "maxConnections": 2,
    "poolTimeoutSeconds": 5,
    "connectTimeoutSeconds": 5,
    "statementTimeoutMilliseconds": 5000,
    "lockTimeoutMilliseconds": 4000,
}
DEFAULTS = {
    "cache": {**REDIS_DEFAULTS, "maxConnections": 128},
    "lanes": {**REDIS_DEFAULTS, "connectTimeoutSeconds": 2, "socketTimeoutSeconds": 2},
    "rateLimits": REDIS_DEFAULTS,
    "state": POSTGRES_DEFAULTS,
    "authentication": POSTGRES_DEFAULTS,
    "kubernetes": {"poolSize": 32, "connectTimeoutSeconds": 5, "readTimeoutSeconds": 20},
}


def settings(name: str) -> dict[str, Any]:
    """
    Validate Helm's JSON projection, also supporting partial environment overrides.

    Args:
        name (str): Transport consumer name from the chart's operator.connections map.

    Returns:
        dict[str, Any]: Validated numeric settings with per-consumer defaults.
    """
    configured = json.loads(os.environ.get("POLYAD_CONNECTION_SETTINGS", "{}"))
    if not isinstance(configured, dict) or configured.keys() - DEFAULTS.keys():
        raise ValueError("unknown operator connection settings")
    result = {}
    for consumer, defaults in DEFAULTS.items():
        overrides = configured.get(consumer, {})
        if not isinstance(overrides, dict) or overrides.keys() - defaults.keys():
            raise ValueError(f"invalid {consumer} connection settings")
        values = {**defaults, **overrides}
        for key, value in values.items():
            integer = key in {"minConnections", "maxConnections", "poolSize"} or key.endswith("Milliseconds")
            integer = integer or (consumer in {"state", "authentication"} and key == "connectTimeoutSeconds")
            minimum = 0 if key == "minConnections" else 1 if integer else 0.1
            maximum = 4096 if key in {"minConnections", "maxConnections", "poolSize"} else 300000 if key.endswith("Milliseconds") else 60
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not minimum <= value <= maximum
                or (integer and not isinstance(value, int))
            ):
                raise ValueError(f"{consumer}.{key} must be {'an integer' if integer else 'a number'} between {minimum} and {maximum}")
        if values.get("minConnections", 0) > values.get("maxConnections", 4096):
            raise ValueError(f"{consumer}.minConnections must not exceed maxConnections")
        result[consumer] = values
    return result[name]


def postgres_options(name: str) -> dict[str, Any]:
    """
    Translate a PostgreSQL consumer's budgets into psycopg pool arguments.

    Args:
        name (str): State or authentication pool name.

    Returns:
        dict[str, Any]: Pool sizing, acquisition and server timeout options.
    """
    value = settings(name)
    return {
        "min_size": value["minConnections"],
        "max_size": value["maxConnections"],
        "timeout": value["poolTimeoutSeconds"],
        "kwargs": {
            "connect_timeout": value["connectTimeoutSeconds"],
            "options": f"-c statement_timeout={value['statementTimeoutMilliseconds']} -c lock_timeout={value['lockTimeoutMilliseconds']}",
        },
    }
