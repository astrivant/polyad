"""
Construct bounded Redis pools when a cache-backed feature is enabled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from redis import ConnectionPool
from redis.asyncio import ConnectionPool as AsyncConnectionPool
from redis.asyncio.connection import parse_url as parse_async_url
from redis.asyncio.retry import Retry as AsyncRetry
from redis.backoff import NoBackoff
from redis.connection import parse_url
from redis.retry import Retry

from polyad.transport.pools import register
from polyad.transport.settings import settings

if TYPE_CHECKING:
    from typing import Any


def pool(url: str, name: str, *, asynchronous: bool = False, **options: Any) -> Any:
    """
    Apply validated administrator limits after parsing Secret-supplied URLs.

    Args:
        url (str): Redis endpoint, potentially including credentials and TLS options.
        name (str): Cache, lanes or rateLimits configuration group.
        asynchronous (bool): Select the event-loop driver instead of the synchronous driver.
        **options (Any): Additional consumer options such as response decoding.

    Returns:
        Any: Registered driver pool with write retries disabled and tuning taking precedence.
    """
    value = settings(name)
    parsed: dict[str, Any] = dict((parse_async_url if asynchronous else parse_url)(url))
    parsed.update(options)
    parsed.update(
        max_connections=value["maxConnections"],
        socket_connect_timeout=value["connectTimeoutSeconds"],
        socket_timeout=value["socketTimeoutSeconds"],
        retry=(AsyncRetry if asynchronous else Retry)(NoBackoff(), 0),
    )
    result = (AsyncConnectionPool if asynchronous else ConnectionPool)(**parsed)
    register(result, name, "redis")
    return result
