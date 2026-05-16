"""
Cache backend factory.

Reads ``REDIS_URL`` from the environment to decide which backend to use:

- If ``REDIS_URL`` is set **and** the ``redis`` package is installed, a
  ``RedisCache`` is returned after a connectivity probe.  If the probe
  fails the factory logs a warning and falls back to ``MemoryCache``.
- Otherwise ``MemoryCache`` is returned (the safe default).
"""

import logging
import os

from .base import CacheBackend
from .memory import MemoryCache

logger = logging.getLogger(__name__)

# Environment variable that activates Redis caching
_REDIS_URL_ENV = "REDIS_URL"


def create_cache(redis_url: str | None = None) -> CacheBackend:
    """
    Build and return the best available cache backend.

    Parameters
    ----------
    redis_url:
        Explicit Redis URL. When *None* the value of the ``REDIS_URL``
        environment variable is used. Pass an empty string ``""`` to force
        the in-memory backend regardless of the environment.

    Returns
    -------
    CacheBackend
        A ``RedisCache`` if Redis is configured and reachable, otherwise a
        ``MemoryCache``.
    """
    url = redis_url if redis_url is not None else os.environ.get(_REDIS_URL_ENV, "")

    if not url:
        logger.debug("REDIS_URL not set – using in-memory cache backend")
        return MemoryCache()

    try:
        from .redis_cache import RedisCache

        cache = RedisCache(url=url)
        logger.info("Redis cache backend initialised (url=%s)", _redact_url(url))
        return cache
    except ImportError:
        logger.warning(
            "REDIS_URL is set but the 'redis' package is not installed. "
            "Falling back to in-memory cache. "
            "Install with: pip install 'polymarket-mcp[redis]'"
        )
        return MemoryCache()
    except Exception as exc:
        logger.warning(
            "Failed to initialise Redis cache (url=%s): %s – falling back to in-memory cache",
            _redact_url(url),
            exc,
        )
        return MemoryCache()


def _redact_url(url: str) -> str:
    """Strip credentials from a Redis URL for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        if parsed.password:
            # Replace password with ***
            netloc = f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname or ""
            if parsed.username:
                netloc = f"{parsed.username}:***@{netloc}"
            parsed = parsed._replace(netloc=netloc)
        return urlunparse(parsed)
    except Exception:
        return "<redis-url>"
