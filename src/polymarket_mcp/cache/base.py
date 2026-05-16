"""
Abstract base class for cache backends.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class CacheBackend(ABC):
    """
    Abstract cache interface used by all cache backends.

    All implementations must be safe to use from async code.
    TTL values are in seconds.
    """

    @abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        """Return cached value for *key*, or ``None`` if missing / expired."""

    @abstractmethod
    async def set(self, key: str, value: Any, ttl_seconds: int = 30) -> None:
        """Store *value* under *key* with an expiry of *ttl_seconds* seconds."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the entry for *key* (no-op if key does not exist)."""

    @abstractmethod
    async def clear(self, namespace: str = "") -> None:
        """
        Remove all entries whose key starts with *namespace*.

        Pass an empty string (the default) to clear **all** entries.
        """
