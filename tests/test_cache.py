"""
Tests for the cache abstraction layer.

Covers:
- MemoryCache: basic get/set/delete/clear, TTL expiry
- RedisCache: basic operations, unavailable-Redis fallback
- factory: REDIS_URL env var selects backend, missing redis package falls back
- portfolio integration: get_all_positions uses cache correctly
"""
import asyncio
import os
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from polymarket_mcp.cache.memory import MemoryCache
from polymarket_mcp.cache.factory import create_cache


# ---------------------------------------------------------------------------
# MemoryCache tests
# ---------------------------------------------------------------------------

class TestMemoryCache:
    """Unit tests for the in-memory cache backend."""

    @pytest.mark.asyncio
    async def test_set_and_get(self):
        cache = MemoryCache()
        await cache.set("key1", {"data": 42}, ttl_seconds=60)
        result = await cache.get("key1")
        assert result == {"data": 42}

    @pytest.mark.asyncio
    async def test_get_missing_key(self):
        cache = MemoryCache()
        result = await cache.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_ttl_expiry(self):
        cache = MemoryCache()
        await cache.set("key_ttl", "value", ttl_seconds=0)
        # With ttl=0 the expiry is set to monotonic() + 0. On the next call
        # monotonic() >= expires_at so the entry is treated as expired.
        # Sleep a tiny bit to ensure the clock has strictly advanced past it.
        await asyncio.sleep(0.01)
        result = await cache.get("key_ttl")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete(self):
        cache = MemoryCache()
        await cache.set("to_delete", "bye", ttl_seconds=60)
        await cache.delete("to_delete")
        assert await cache.get("to_delete") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_is_noop(self):
        cache = MemoryCache()
        # Should not raise
        await cache.delete("does_not_exist")

    @pytest.mark.asyncio
    async def test_clear_all(self):
        cache = MemoryCache()
        await cache.set("a", 1, ttl_seconds=60)
        await cache.set("b", 2, ttl_seconds=60)
        await cache.clear()
        assert await cache.get("a") is None
        assert await cache.get("b") is None

    @pytest.mark.asyncio
    async def test_clear_namespace(self):
        cache = MemoryCache()
        await cache.set("portfolio:pos:1", "v1", ttl_seconds=60)
        await cache.set("portfolio:pos:2", "v2", ttl_seconds=60)
        await cache.set("other:key", "v3", ttl_seconds=60)
        await cache.clear("portfolio:")
        assert await cache.get("portfolio:pos:1") is None
        assert await cache.get("portfolio:pos:2") is None
        # Key outside namespace should still be present
        assert await cache.get("other:key") == "v3"

    @pytest.mark.asyncio
    async def test_overwrite(self):
        cache = MemoryCache()
        await cache.set("k", "first", ttl_seconds=60)
        await cache.set("k", "second", ttl_seconds=60)
        assert await cache.get("k") == "second"

    @pytest.mark.asyncio
    async def test_various_value_types(self):
        cache = MemoryCache()
        for key, value in [
            ("str", "hello"),
            ("int", 42),
            ("float", 3.14),
            ("list", [1, 2, 3]),
            ("dict", {"x": 1}),
            ("none_val", None),
        ]:
            await cache.set(key, value, ttl_seconds=60)
            assert await cache.get(key) == value


# ---------------------------------------------------------------------------
# RedisCache tests (using unittest.mock to avoid a real Redis server)
# ---------------------------------------------------------------------------

class TestRedisCache:
    """Tests for the Redis cache backend (mocked Redis client)."""

    def _make_redis_cache(self, mock_redis_client):
        """Create a RedisCache with an injected mock redis client."""
        from polymarket_mcp.cache.redis_cache import RedisCache

        cache = RedisCache.__new__(RedisCache)
        cache.url = "redis://localhost:6379/0"
        cache.default_ttl_seconds = 30
        cache._client = mock_redis_client
        return cache

    @pytest.mark.asyncio
    async def test_get_hit(self):
        import json
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=json.dumps({"key": "value"}))
        cache = self._make_redis_cache(mock_client)
        result = await cache.get("some_key")
        assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_get_miss(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=None)
        cache = self._make_redis_cache(mock_client)
        result = await cache.get("missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_redis_error_returns_none(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        cache = self._make_redis_cache(mock_client)
        # Should not raise; returns None gracefully
        result = await cache.get("any_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_calls_setex(self):
        import json
        mock_client = AsyncMock()
        mock_client.setex = AsyncMock(return_value=True)
        cache = self._make_redis_cache(mock_client)
        await cache.set("mykey", [1, 2, 3], ttl_seconds=45)
        mock_client.setex.assert_awaited_once_with("mykey", 45, json.dumps([1, 2, 3]))

    @pytest.mark.asyncio
    async def test_set_redis_error_is_silent(self):
        mock_client = AsyncMock()
        mock_client.setex = AsyncMock(side_effect=Exception("timeout"))
        cache = self._make_redis_cache(mock_client)
        # Should not raise
        await cache.set("k", "v", ttl_seconds=30)

    @pytest.mark.asyncio
    async def test_delete(self):
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=1)
        cache = self._make_redis_cache(mock_client)
        await cache.delete("del_key")
        mock_client.delete.assert_awaited_once_with("del_key")

    @pytest.mark.asyncio
    async def test_clear_all_calls_flushdb(self):
        mock_client = AsyncMock()
        mock_client.flushdb = AsyncMock()
        cache = self._make_redis_cache(mock_client)
        await cache.clear()
        mock_client.flushdb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_namespace_uses_scan(self):
        mock_client = AsyncMock()
        # scan returns cursor=0 (done) and some keys
        mock_client.scan = AsyncMock(return_value=(0, ["portfolio:a", "portfolio:b"]))
        mock_client.delete = AsyncMock()
        cache = self._make_redis_cache(mock_client)
        await cache.clear("portfolio:")
        mock_client.delete.assert_awaited_once_with("portfolio:a", "portfolio:b")

    @pytest.mark.asyncio
    async def test_ping_success(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        cache = self._make_redis_cache(mock_client)
        assert await cache.ping() is True

    @pytest.mark.asyncio
    async def test_ping_failure(self):
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(side_effect=Exception("unreachable"))
        cache = self._make_redis_cache(mock_client)
        assert await cache.ping() is False


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------

class TestCreateCache:
    """Tests for the cache factory function."""

    def test_no_redis_url_returns_memory_cache(self):
        """With no REDIS_URL the factory should return MemoryCache."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDIS_URL", None)
            cache = create_cache()
        assert isinstance(cache, MemoryCache)

    def test_empty_redis_url_returns_memory_cache(self):
        cache = create_cache(redis_url="")
        assert isinstance(cache, MemoryCache)

    def test_explicit_empty_string_overrides_env(self):
        """Passing redis_url='' forces memory backend even if env is set."""
        with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}):
            cache = create_cache(redis_url="")
        assert isinstance(cache, MemoryCache)

    def test_redis_import_error_falls_back_to_memory(self):
        """When the redis package is not installed, fall back gracefully."""
        with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}):
            with patch.dict("sys.modules", {"redis": None, "redis.asyncio": None}):
                cache = create_cache()
        assert isinstance(cache, MemoryCache)

    def test_redis_url_returns_redis_cache_when_available(self):
        """When redis is importable, factory returns a RedisCache."""
        try:
            import redis  # noqa: F401
        except ImportError:
            pytest.skip("redis package not installed")

        from polymarket_mcp.cache.redis_cache import RedisCache

        with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}):
            cache = create_cache()
        assert isinstance(cache, RedisCache)

    def test_redis_constructor_error_falls_back_to_memory(self):
        """If RedisCache.__init__ raises for any reason, fall back to MemoryCache."""
        # Patch the module-level import inside the factory's try block
        import polymarket_mcp.cache.redis_cache as redis_module
        original_cls = redis_module.RedisCache

        class BrokenRedisCache:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("unexpected")

        redis_module.RedisCache = BrokenRedisCache
        try:
            with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}):
                cache = create_cache()
            assert isinstance(cache, MemoryCache)
        finally:
            redis_module.RedisCache = original_cls


# ---------------------------------------------------------------------------
# Portfolio integration: cache is used in get_all_positions
# ---------------------------------------------------------------------------

class TestPortfolioCacheIntegration:
    """Verify that get_all_positions uses (and populates) the cache backend."""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.POLYGON_ADDRESS = "0xABCDEF"
        config.effective_funder = "0xABCDEF"
        return config

    @pytest.fixture
    def mock_rate_limiter(self):
        rl = AsyncMock()
        rl.acquire = AsyncMock(return_value=0.0)
        return rl

    @pytest.fixture
    def mock_polymarket_client(self):
        client = AsyncMock()
        client.get_orderbook = AsyncMock(return_value={
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.56", "size": "100"}],
        })
        client.get_funder_address = MagicMock(return_value="0xABCDEF")
        return client

    @pytest.mark.asyncio
    async def test_positions_data_written_to_cache(
        self, mock_polymarket_client, mock_rate_limiter, mock_config
    ):
        """After a fresh fetch the data should be present in the cache."""
        from polymarket_mcp.tools import portfolio as portfolio_module

        # Inject a fresh MemoryCache so this test is isolated
        fresh_cache = MemoryCache(default_ttl_seconds=30)
        original = portfolio_module._portfolio_cache
        portfolio_module._portfolio_cache = fresh_cache

        positions_payload = [
            {
                "asset_id": "tok1",
                "market": "mkt1",
                "market_question": "Will test pass?",
                "outcome": "Yes",
                "size": "10",
                "average_price": "0.5",
            }
        ]

        # Mock the httpx.AsyncClient to return the positions payload
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=positions_payload)

        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.get = AsyncMock(return_value=mock_response)

        try:
            from polymarket_mcp.tools.portfolio import get_all_positions
            with patch("httpx.AsyncClient", return_value=mock_http_client):
                await get_all_positions(mock_polymarket_client, mock_rate_limiter, mock_config)

            # After the call, cache should have the positions data
            cache_key_prefix = "portfolio:positions:0xabcdef"
            found = False
            for key in fresh_cache._store:
                if key.startswith(cache_key_prefix):
                    found = True
                    value, _ = fresh_cache._store[key]
                    assert value == positions_payload
                    break
            assert found, "Expected positions data to be stored in cache"
        finally:
            portfolio_module._portfolio_cache = original

    @pytest.mark.asyncio
    async def test_positions_served_from_cache_no_http(
        self, mock_polymarket_client, mock_rate_limiter, mock_config
    ):
        """When the cache already holds data, no HTTP call should be made."""
        from polymarket_mcp.tools import portfolio as portfolio_module
        from polymarket_mcp.tools.portfolio import get_all_positions

        fresh_cache = MemoryCache(default_ttl_seconds=30)
        original = portfolio_module._portfolio_cache
        portfolio_module._portfolio_cache = fresh_cache

        cached_payload = [
            {
                "asset_id": "tok_cached",
                "market": "mkt_cached",
                "market_question": "Cached market question?",
                "outcome": "No",
                "size": "5",
                "average_price": "0.4",
            }
        ]

        # Pre-populate the cache
        user_addr = "0xabcdef"
        cache_key = f"portfolio:positions:{user_addr}:False:1.0"
        await fresh_cache.set(cache_key, cached_payload, ttl_seconds=30)

        try:
            with patch("httpx.AsyncClient") as mock_http:
                result = await get_all_positions(
                    mock_polymarket_client, mock_rate_limiter, mock_config
                )
            # HTTP client should not have been used
            mock_http.assert_not_called()
            # Result should still be a valid response
            assert len(result) == 1
            assert "Portfolio Positions" in result[0].text or "No positions" in result[0].text
        finally:
            portfolio_module._portfolio_cache = original

    @pytest.mark.asyncio
    async def test_set_portfolio_cache_replaces_backend(self):
        """_set_portfolio_cache() should replace the module-level backend."""
        from polymarket_mcp.tools import portfolio as portfolio_module
        from polymarket_mcp.tools.portfolio import _set_portfolio_cache

        original = portfolio_module._portfolio_cache
        new_cache = MemoryCache()
        try:
            _set_portfolio_cache(new_cache)
            assert portfolio_module._portfolio_cache is new_cache
        finally:
            portfolio_module._portfolio_cache = original
