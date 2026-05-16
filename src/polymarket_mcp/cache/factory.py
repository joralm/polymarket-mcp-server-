"""
Cache backend factory.

Reads Redis configuration from environment variables to decide which backend
to use:

- If ``REDIS_URL`` is set (or can be derived from ``REDIS_HOST`` +
  ``REDIS_PORT``) **and** the ``redis`` package is installed, a
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
_REDIS_HOST_ENV = "REDIS_HOST"
_REDIS_PORT_ENV = "REDIS_PORT"


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
    url = _resolve_redis_url(redis_url)

    if not url:
        logger.debug(
            "Redis cache not configured (set REDIS_URL or REDIS_HOST+REDIS_PORT) – "
            "using in-memory cache backend"
        )
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


def _resolve_redis_url(redis_url: str | None) -> str:
    """Resolve Redis URL from explicit value or environment configuration."""
    if redis_url is not None:
        return redis_url

    url = os.environ.get(_REDIS_URL_ENV, "").strip()
    if url:
        return url

    host = os.environ.get(_REDIS_HOST_ENV, "").strip()
    port = os.environ.get(_REDIS_PORT_ENV, "").strip()

    if host and port:
        if not port.isdigit():
            logger.warning(
                "Ignoring Redis config: REDIS_PORT must be numeric when using REDIS_HOST/REDIS_PORT"
            )
            return ""
        return f"redis://{host}:{port}/0"

    if host or port:
        logger.warning(
            "Incomplete Redis config: set REDIS_URL, or set both REDIS_HOST and REDIS_PORT"
        )
    return ""


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
