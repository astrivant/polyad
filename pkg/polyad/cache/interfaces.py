"""
Describe bounded transient JSON caching independently of its Redis transport.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


class CacheBackend(ABC):
    """
    Namespace transient values, require expiry and release owned connections.
    """

    @abstractmethod
    async def get(self, key: str) -> Any:
        """
        Read a transient JSON value; misses return None.

        Args:
            key (str): Cache key within this operator namespace.

        Returns:
            Any: Decoded value, or None when the entry is absent.
        """
        ...

    @abstractmethod
    async def set(self, key: str, value: Any, *, ttl_seconds: int) -> None:
        """
        Store a JSON value with an explicit bounded lifetime.

        Args:
            key (str): Cache key within this operator namespace.
            value (Any): JSON-serializable transient data.
            ttl_seconds (int): Positive entry lifetime in seconds.

        Returns:
            None: No return value.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """
        Release connections after the replica's cache users have joined.

        Returns:
            None: No return value.
        """
        ...
