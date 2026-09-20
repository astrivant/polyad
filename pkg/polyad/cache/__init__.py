"""
Redis-compatible shared caches for Polyad operator replicas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad.cache.interfaces import CacheBackend as CacheBackend

if TYPE_CHECKING:
    from typing import Any

    from polyad.cache.redis import Cache as Cache
    from polyad.cache.redis import cache_url as cache_url

__all__ = (
    "Cache",
    "CacheBackend",
    "cache_url",
)


def __getattr__(name: str) -> Any:
    """
    Load the Redis driver only when its concrete cache or endpoint helper is used.

    Args:
        name (str): Requested public package attribute.

    Returns:
        Any: Concrete cache class or configuration helper.

    Raises:
        AttributeError: The name is not a public cache export.
    """
    if name in {"Cache", "cache_url"}:
        from polyad.cache import redis

        return getattr(redis, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
