"""
Cache abstraction layer for Polymarket MCP Server.

Provides a unified cache interface with two backends:
- MemoryCache: in-process dict-based cache (default, always available)
- RedisCache: Redis-backed cache (optional, enabled via REDIS_URL or
  REDIS_HOST+REDIS_PORT env vars)

Usage::

    from polymarket_mcp.cache import create_cache

    cache = create_cache()          # auto-detects backend from Redis env vars
    await cache.set("key", value, ttl_seconds=30)
    value = await cache.get("key")
    await cache.delete("key")
    await cache.clear("namespace:")
"""

from .base import CacheBackend
from .memory import MemoryCache
from .factory import create_cache

__all__ = ["CacheBackend", "MemoryCache", "create_cache"]
