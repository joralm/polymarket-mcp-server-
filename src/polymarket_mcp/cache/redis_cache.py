"""
Redis cache backend.

Uses the ``redis`` async client (``redis[hiredis]`` optional extra).
Serialises values as JSON so any JSON-serialisable Python object can be stored.

The class is imported lazily so the package remains importable even when the
``redis`` library is not installed.  Callers should use the factory
(``polymarket_mcp.cache.create_cache``) rather than instantiating this class
directly.
"""

import json
import logging
from typing import Any, Optional

from .base import CacheBackend

logger = logging.getLogger(__name__)


class RedisCache(CacheBackend):
    """
    Redis-backed cache backend.

    Parameters
    ----------
    url:
        Redis connection URL, e.g. ``redis://localhost:6379/0``.
    default_ttl_seconds:
        Fallback TTL when callers do not supply one (default: 30 s).
    socket_timeout:
        Per-operation timeout in seconds used to avoid blocking on a slow
        or unavailable Redis server (default: 2 s).
    """

    def __init__(
        self,
        url: str,
        default_ttl_seconds: int = 30,
        socket_timeout: float = 2.0,
    ) -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError as exc:
            raise ImportError(
                "The 'redis' package is required for RedisCache. "
                "Install it with: pip install 'polymarket-mcp[redis]'"
            ) from exc

        self.url = url
        self.default_ttl_seconds = default_ttl_seconds
        self._client = aioredis.from_url(
            url,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_timeout,
            decode_responses=True,
        )

    # ------------------------------------------------------------------
    # CacheBackend interface
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Optional[Any]:
        try:
            raw = await self._client.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            logger.warning("RedisCache.get failed for key=%s: %s", key, exc)
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int = 30) -> None:
        try:
            serialised = json.dumps(value)
            await self._client.setex(key, ttl_seconds, serialised)
        except Exception as exc:
            logger.warning("RedisCache.set failed for key=%s: %s", key, exc)

    async def delete(self, key: str) -> None:
        try:
            await self._client.delete(key)
        except Exception as exc:
            logger.warning("RedisCache.delete failed for key=%s: %s", key, exc)

    async def clear(self, namespace: str = "") -> None:
        try:
            if not namespace:
                await self._client.flushdb()
            else:
                pattern = f"{namespace}*"
                cursor = 0
                while True:
                    cursor, keys = await self._client.scan(cursor, match=pattern, count=200)
                    if keys:
                        await self._client.delete(*keys)
                    if cursor == 0:
                        break
        except Exception as exc:
            logger.warning("RedisCache.clear failed for namespace=%r: %s", namespace, exc)

    async def ping(self) -> bool:
        """Return ``True`` if the Redis server is reachable, ``False`` otherwise."""
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        """Close the underlying connection pool."""
        try:
            await self._client.aclose()
        except Exception:
            pass
