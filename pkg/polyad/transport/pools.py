"""
Sample live pool occupancy without network I/O or credential-bearing labels.
"""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from typing import Any

_lock = Lock()
_pools: WeakKeyDictionary[Any, tuple[str, str]] = WeakKeyDictionary()


def register(pool: Any, name: str, driver: str) -> None:
    """
    Track a pool only while its owning client remains alive.

    Args:
        pool (Any): Redis or psycopg pool with local occupancy statistics.
        name (str): Fixed consumer category, never a URL, DSN or credential.
        driver (str): Redis or PostgreSQL statistics adapter.

    Returns:
        None: No return value.
    """
    with _lock:
        _pools[pool] = (name, driver)


def snapshot() -> dict[str, dict[str, int]]:
    """
    Sample checked-out connections, queued requests and configured capacity.

    Returns:
        dict[str, dict[str, int]]: Per-consumer process totals; idle connections consume no demand.
    """
    with _lock:
        pools = list(_pools.items())
    result: dict[str, dict[str, int]] = {}
    for pool, (name, driver) in pools:
        if driver == "redis":
            # redis-py exposes its pool membership locally; never enumerate members
            # or expose connection objects, which contain credentials.
            active = len(pool._in_use_connections)
            limit, waiting = pool.max_connections, 0
        else:
            stats = pool.get_stats()
            active = max(0, stats["pool_size"] - stats["pool_available"])
            limit, waiting = stats["pool_max"], stats["requests_waiting"]
        totals = result.setdefault(name, {"inUse": 0, "limit": 0, "waiting": 0})
        for key, value in (("inUse", active), ("limit", limit), ("waiting", waiting)):
            totals[key] += value
    return result


def pressure(pools: dict[str, dict[str, int]]) -> float:
    """
    Express the busiest consumer as an equivalent fraction of one operator replica.

    Args:
        pools (dict[str, dict[str, int]]): Local pool observations, grouped by consumer.

    Returns:
        float: Maximum active-plus-waiting fraction; zero when no pools are enabled.
    """
    return max(((entry["inUse"] + entry["waiting"]) / entry["limit"] for entry in pools.values() if entry["limit"]), default=0.0)
