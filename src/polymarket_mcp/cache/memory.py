"""
In-memory cache backend.

Thread-safe, async-compatible, TTL-based in-process dictionary cache.
This is the default backend used when Redis is not configured.
"""

import logging
from time import monotonic
from typing import Any, Dict, Optional, Tuple

from .base import CacheBackend

logger = logging.getLogger(__name__)


class MemoryCache(CacheBackend):
    """
    Simple in-process dict cache with per-entry TTL.

    Compatible with the legacy ``PortfolioDataCache`` behaviour; the same
    30-second default TTL is preserved.
    """

    def __init__(self, default_ttl_seconds: int = 30) -> None:
        self.default_ttl_seconds = default_ttl_seconds
        # key -> (value, expiry_monotonic_timestamp)
        self._store: Dict[str, Tuple[Any, float]] = {}

    # ------------------------------------------------------------------
    # CacheBackend interface
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: Any, ttl_seconds: int = 30) -> None:
        self._store[key] = (value, monotonic() + ttl_seconds)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def clear(self, namespace: str = "") -> None:
        if not namespace:
            self._store.clear()
        else:
            keys_to_delete = [k for k in self._store if k.startswith(namespace)]
            for k in keys_to_delete:
                del self._store[k]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _purge_expired(self) -> None:
        """Remove all expired entries (housekeeping helper, not called automatically)."""
        now = monotonic()
        expired = [k for k, (_, exp) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]
