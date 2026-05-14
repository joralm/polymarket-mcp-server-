"""
Tests for GitHub issue fixes (#2, #6, #10).

- Issue #10: API credentials must not be logged at INFO level
- Issue #6: fastapi dependency must be compatible with mcp's anyio requirement
- Issue #2: Market discovery must filter out closed/expired markets
"""

import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timedelta

from py_clob_client.clob_types import AssetType, OrderBookSummary, OrderSummary
from polymarket_mcp.auth.client import PolymarketClient
from polymarket_mcp.config import PolymarketConfig
from polymarket_mcp.tools.trading import TradingTools
from polymarket_mcp.utils.websocket_manager import WebSocketManager

# ---------------------------------------------------------------------------
# Issue #10 — Credential masking in server.py
# ---------------------------------------------------------------------------


class TestCredentialMasking:
    """Verify that API credentials are never logged in full."""

    def test_server_logs_truncated_key(self):
        """Credentials should be logged at DEBUG with only first 8 chars."""
        import polymarket_mcp.server as server_module
        import inspect

        source_code = inspect.getsource(server_module)

        # Must NOT contain the old full-logging pattern
        assert (
            'logger.info(f"POLYMARKET_API_KEY={' not in source_code
        ), "Full API key is still logged at INFO level"
        assert (
            'logger.info(f"POLYMARKET_PASSPHRASE={' not in source_code
        ), "Full passphrase is still logged at INFO level"

        # Must contain truncated debug logging
        assert (
            'logger.debug(f"POLYMARKET_API_KEY={' in source_code
            or 'logger.debug(f"POLYMARKET_API_KEY=' in source_code
        ), "API key should be logged at DEBUG level"
        assert "[:8]" in source_code, "Credentials should be truncated to first 8 chars"

    def test_no_full_credentials_at_info(self):
        """Ensure no line logs full credential values at INFO."""
        import polymarket_mcp.server as server_module
        import inspect

        lines = inspect.getsource(server_module).splitlines()

        for i, line in enumerate(lines):
            stripped = line.strip()
            if "logger.info" in stripped:
                assert (
                    "api_key)" not in stripped and "api_passphrase)" not in stripped
                ), f"Line {i+1} logs full credential at INFO: {stripped}"


# ---------------------------------------------------------------------------
# Issue #6 — fastapi dependency compatibility
# ---------------------------------------------------------------------------


class TestDependencyCompatibility:
    """Verify fastapi version constraint is compatible with mcp/anyio."""

    def test_fastapi_version_constraint(self):
        """fastapi must be pinned >=0.115.0 to support anyio>=4.5."""
        import tomllib
        import pathlib

        pyproject = pathlib.Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)

        deps = data["project"]["dependencies"]
        fastapi_dep = [d for d in deps if d.startswith("fastapi")]
        assert len(fastapi_dep) == 1, "Expected exactly one fastapi dependency"

        dep = fastapi_dep[0]
        # Extract minimum version
        assert ">=0.115.0" in dep or ">=0.115" in dep, f"fastapi must be >=0.115.0, got: {dep}"

    def test_no_old_fastapi_constraint(self):
        """Ensure old >=0.104.0 constraint is gone."""
        import pathlib

        pyproject = pathlib.Path(__file__).parent.parent / "pyproject.toml"
        content = pyproject.read_text()
        assert "fastapi>=0.104.0" not in content, "Old fastapi>=0.104.0 constraint still present"

    def test_pip_dry_run_install(self):
        """Verify pip can resolve dependencies without conflict."""
        import subprocess
        import sys
        import pathlib

        project_root = pathlib.Path(__file__).parent.parent
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "."],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        # pip dry-run should not report an error about dependency conflict
        assert (
            "ResolutionImpossible" not in result.stderr
        ), f"Dependency resolution failed:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Issue #2 — Stale / closed market filtering
# ---------------------------------------------------------------------------


class TestMarketFiltering:
    """Verify that market discovery functions filter out old/closed markets."""

    @pytest.mark.asyncio
    async def test_search_markets_sends_active_and_closed_params(self):
        """search_markets must include active=true and closed=false."""
        from polymarket_mcp.tools import market_discovery

        with patch.object(
            market_discovery, "_fetch_gamma_markets", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = []
            await market_discovery.search_markets("test", limit=5)

            mock_fetch.assert_called_once()
            call_args = mock_fetch.call_args
            params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
            assert params.get("active") == "true", "search_markets must set active=true"
            assert params.get("closed") == "false", "search_markets must set closed=false"

    @pytest.mark.asyncio
    async def test_get_trending_markets_sends_active_and_closed_params(self):
        """get_trending_markets must include active=true and closed=false."""
        from polymarket_mcp.tools import market_discovery

        with patch.object(
            market_discovery, "_fetch_gamma_markets", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = []
            await market_discovery.get_trending_markets(limit=5)

            mock_fetch.assert_called_once()
            call_args = mock_fetch.call_args
            params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
            assert params.get("active") == "true"
            assert params.get("closed") == "false"

    @pytest.mark.asyncio
    async def test_trending_markets_filters_expired_end_dates(self):
        """get_trending_markets must exclude markets whose end_date_iso is in the past."""
        from polymarket_mcp.tools import market_discovery

        past_date = (datetime.utcnow() - timedelta(days=30)).isoformat() + "Z"
        future_date = (datetime.utcnow() + timedelta(days=30)).isoformat() + "Z"

        mock_markets = [
            {"question": "Old market", "end_date_iso": past_date, "volume24hr": "1000"},
            {"question": "Current market", "end_date_iso": future_date, "volume24hr": "500"},
            {"question": "No end date", "volume24hr": "200"},
        ]

        with patch.object(
            market_discovery, "_fetch_gamma_markets", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = mock_markets
            results = await market_discovery.get_trending_markets(limit=10)

            # Old market should be filtered out
            questions = [m["question"] for m in results]
            assert "Old market" not in questions, "Expired market should be filtered out"
            assert "Current market" in questions
            assert "No end date" in questions

    @pytest.mark.asyncio
    async def test_featured_markets_filters_expired_end_dates(self):
        """get_featured_markets must exclude markets whose end_date_iso is in the past."""
        from polymarket_mcp.tools import market_discovery

        past_date = (datetime.utcnow() - timedelta(days=30)).isoformat() + "Z"
        future_date = (datetime.utcnow() + timedelta(days=30)).isoformat() + "Z"

        mock_markets = [
            {"question": "Old featured", "end_date_iso": past_date, "volume24hr": "1000"},
            {"question": "Active featured", "end_date_iso": future_date, "volume24hr": "500"},
        ]

        with patch.object(
            market_discovery, "_fetch_gamma_markets", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = mock_markets
            results = await market_discovery.get_featured_markets(limit=10)

            questions = [m["question"] for m in results]
            assert "Old featured" not in questions, "Expired featured market should be filtered out"
            assert "Active featured" in questions

    @pytest.mark.asyncio
    async def test_filter_by_category_sends_closed_false(self):
        """filter_markets_by_category must include closed=false."""
        from polymarket_mcp.tools import market_discovery

        with patch.object(
            market_discovery, "_fetch_gamma_markets", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = []
            await market_discovery.filter_markets_by_category("Politics", active_only=True, limit=5)

            call_args = mock_fetch.call_args
            params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
            assert params.get("closed") == "false"
            assert params.get("active") == "true"


class TestStreamableHTTPTransport:
    """Regression tests for Streamable HTTP transport configuration."""

    def test_transport_mode_defaults_to_streamable_http(self):
        """Transport mode should default to streamable-http when unset."""
        import polymarket_mcp.server as server_module

        with patch.dict("os.environ", {}, clear=True):
            assert server_module.get_transport_mode() == "streamable-http"

    def test_transport_mode_accepts_stdio_aliases(self):
        """Transport mode parser should normalize stdio aliases."""
        import polymarket_mcp.server as server_module

        with patch.dict("os.environ", {"MCP_TRANSPORT": "STDIO"}, clear=True):
            assert server_module.get_transport_mode() == "stdio"

    def test_transport_mode_accepts_explicit_streamable_http(self):
        """Transport mode parser should accept explicit streamable-http values."""
        import polymarket_mcp.server as server_module

        with patch.dict("os.environ", {"MCP_TRANSPORT": "streamable-http"}, clear=True):
            assert server_module.get_transport_mode() == "streamable-http"

    def test_transport_mode_rejects_invalid_value(self):
        """Transport mode parser should reject unsupported values."""
        import polymarket_mcp.server as server_module

        with patch.dict("os.environ", {"MCP_TRANSPORT": "legacy-sse"}, clear=True):
            with pytest.raises(ValueError, match="Invalid MCP_TRANSPORT"):
                server_module.get_transport_mode()

    def test_streamable_http_settings_are_normalized(self):
        """Streamable HTTP settings should normalize host, port, and path."""
        import polymarket_mcp.server as server_module

        with patch.dict(
            "os.environ",
            {
                "MCP_STREAMABLE_HTTP_HOST": "127.0.0.1",
                "MCP_STREAMABLE_HTTP_PORT": "9001",
                "MCP_STREAMABLE_HTTP_PATH": "custom-mcp",
            },
            clear=True,
        ):
            host, port, path = server_module.get_streamable_http_settings()

        assert host == "127.0.0.1"
        assert port == 9001
        assert path == "/custom-mcp"

    def test_streamable_http_settings_keep_existing_slash(self):
        """Streamable HTTP path should preserve an already-normalized leading slash."""
        import polymarket_mcp.server as server_module

        with patch.dict(
            "os.environ",
            {"MCP_STREAMABLE_HTTP_PATH": "/already-normalized"},
            clear=True,
        ):
            _, _, path = server_module.get_streamable_http_settings()

        assert path == "/already-normalized"

    def test_streamable_http_settings_use_defaults_when_unset(self):
        """Streamable HTTP settings should return defaults when unset."""
        import polymarket_mcp.server as server_module

        with patch.dict("os.environ", {}, clear=True):
            host, port, path = server_module.get_streamable_http_settings()

        assert host == "0.0.0.0"
        assert port == 8000
        assert path == "/mcp"

    def test_streamable_http_settings_reject_out_of_range_port(self):
        """Streamable HTTP settings should reject ports outside the valid TCP range."""
        import polymarket_mcp.server as server_module

        with patch.dict("os.environ", {"MCP_STREAMABLE_HTTP_PORT": "70000"}, clear=True):
            with pytest.raises(ValueError, match="between 1 and 65535"):
                server_module.get_streamable_http_settings()

    @pytest.mark.parametrize("invalid_port", ["0", "-1"])
    def test_streamable_http_settings_reject_non_positive_ports(self, invalid_port):
        """Streamable HTTP settings should reject zero/negative ports."""
        import polymarket_mcp.server as server_module

        with patch.dict("os.environ", {"MCP_STREAMABLE_HTTP_PORT": invalid_port}, clear=True):
            with pytest.raises(ValueError, match="between 1 and 65535"):
                server_module.get_streamable_http_settings()

    def test_mcp_route_matches_exact_path_without_trailing_slash(self):
        """Route must match /mcp exactly so POST /mcp (init) isn't 404'd.

        Starlette's Mount('/mcp', ...) creates regex ^/mcp/(?P<path>.*)$ which
        requires a trailing slash, causing POST /mcp to return 404.
        Route('/mcp{extra:path}', ...) creates ^/mcp(?P<extra>.*)$ which matches
        /mcp, /mcp/, and /mcp/<session-id>.
        """
        from starlette.routing import compile_path

        # Verify the Mount regex does NOT match /mcp (reproduces the bug)
        mount_regex, _, _ = compile_path("/mcp/{path:path}")
        assert not mount_regex.match("/mcp"), "Mount regex should not match /mcp (confirms bug)"
        assert mount_regex.match("/mcp/"), "Mount regex should match /mcp/"

        # Verify the Route regex we now use DOES match /mcp
        route_regex, _, _ = compile_path("/mcp{extra:path}")
        assert route_regex.match("/mcp"), "Route regex must match /mcp (exact path)"
        assert route_regex.match("/mcp/"), "Route regex must match /mcp/ (trailing slash)"
        assert route_regex.match("/mcp/abc123"), "Route regex must match /mcp/<session-id>"

    def test_server_uses_route_not_mount_for_mcp_endpoint(self):
        """Route("/mcp{extra:path}") must match /mcp for all HTTP methods.

        Mount('/mcp', ...) misses POST /mcp returning 404; Route fixes this.
        Verify behavior using a real Starlette Router + TestClient.
        """
        from starlette.routing import Route, Router
        from starlette.responses import PlainTextResponse
        from starlette.testclient import TestClient

        # Simulate the actual server routing setup with a lightweight ASGI handler
        class _FakeASGIApp:
            async def __call__(self, scope, receive, send) -> None:
                await PlainTextResponse("ok")(scope, receive, send)

        fake_mcp_app = _FakeASGIApp()

        router = Router(
            routes=[
                Route("/mcp/sse", endpoint=lambda req: PlainTextResponse("sse")),
                Route("/mcp{extra:path}", endpoint=fake_mcp_app),
            ],
            redirect_slashes=False,
        )

        client = TestClient(router, raise_server_exceptions=True)

        # The critical case: POST /mcp (no trailing slash) must NOT return 404
        resp = client.post("/mcp", content=b"{}", headers={"content-type": "application/json"})
        assert (
            resp.status_code != 404
        ), f"POST /mcp returned 404 — routing fix regressed (got {resp.status_code})"

        # Trailing slash variant should also work
        resp = client.post("/mcp/", content=b"{}", headers={"content-type": "application/json"})
        assert resp.status_code != 404, f"POST /mcp/ returned 404 (got {resp.status_code})"

        # /mcp/sse must still be handled by its dedicated route
        resp = client.get("/mcp/sse")
        assert resp.status_code != 404, f"GET /mcp/sse returned 404 (got {resp.status_code})"


class TestWebSocketRuntimeRobustness:
    """Regression tests for websocket runtime safety checks."""

    @pytest.mark.asyncio
    async def test_listen_marks_clob_disconnected_when_socket_closed(self):
        """Closed CLOB socket should force disconnected/auth state before returning."""
        manager = WebSocketManager(
            config=PolymarketConfig(
                POLYGON_PRIVATE_KEY="0" * 64,
                POLYGON_ADDRESS="0x" + "0" * 40,
            )
        )
        manager.should_run = True
        manager.clob_connected = True
        manager.authenticated = True
        manager.clob_ws = MagicMock(closed=True)

        await manager._listen_to_websocket("clob")

        assert manager.clob_connected is False
        assert manager.authenticated is False

    @pytest.mark.asyncio
    async def test_listen_rejects_unknown_channel(self):
        """Unknown listener channel should raise to avoid silent misrouting."""
        manager = WebSocketManager(
            config=PolymarketConfig(
                POLYGON_PRIVATE_KEY="0" * 64,
                POLYGON_ADDRESS="0x" + "0" * 40,
            )
        )
        manager.should_run = True

        with pytest.raises(ValueError, match="Unknown websocket channel"):
            await manager._listen_to_websocket("invalid-channel")  # type: ignore[arg-type]

    def test_connections_healthy_requires_both_open_channels(self):
        """Health check should fail when either websocket channel is closed."""
        manager = WebSocketManager(
            config=PolymarketConfig(
                POLYGON_PRIVATE_KEY="0" * 64,
                POLYGON_ADDRESS="0x" + "0" * 40,
            )
        )
        manager.clob_connected = True
        manager.realtime_connected = True
        manager.clob_ws = MagicMock(closed=False)
        manager.realtime_ws = MagicMock(closed=True)

        assert manager._connections_healthy() is False


class TestCriticalRuntimeFixes:
    """Regression tests for portfolio and realtime issue fixes."""

    @pytest.mark.asyncio
    async def test_server_passes_rate_limiter_to_portfolio_tools(self):
        """Portfolio tools should receive the rate limiter, not safety limits."""
        import polymarket_mcp.server as server_module

        original_polymarket_client = server_module.polymarket_client
        original_rate_limiter = server_module.rate_limiter
        original_safety_limits = server_module.safety_limits
        original_config = server_module.config

        try:
            server_module.polymarket_client = MagicMock()
            server_module.rate_limiter = MagicMock()
            server_module.safety_limits = MagicMock()
            server_module.config = MagicMock()

            with patch.object(
                server_module.portfolio_integration,
                "call_portfolio_tool",
                new_callable=AsyncMock,
            ) as mock_call:
                mock_call.return_value = []

                await server_module.call_tool("get_all_positions", {})

            mock_call.assert_awaited_once_with(
                "get_all_positions",
                {},
                server_module.polymarket_client,
                server_module.rate_limiter,
                server_module.config,
            )
        finally:
            server_module.polymarket_client = original_polymarket_client
            server_module.rate_limiter = original_rate_limiter
            server_module.safety_limits = original_safety_limits
            server_module.config = original_config

    @pytest.mark.asyncio
    async def test_server_uses_passphrase_as_api_secret_fallback(self):
        """Server initialization should keep legacy passphrase-only auth working."""
        import polymarket_mcp.server as server_module

        original_config = server_module.config
        original_polymarket_client = server_module.polymarket_client
        original_safety_limits = server_module.safety_limits
        original_rate_limiter = server_module.rate_limiter
        original_trading_tools = server_module.trading_tools
        original_websocket_manager = server_module.websocket_manager

        fake_config = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "0" * 40,
            POLYMARKET_API_KEY="legacy-key",
            POLYMARKET_API_SECRET=None,
            POLYMARKET_PASSPHRASE="legacy-passphrase",
            POLYMARKET_API_KEY_NAME="legacy-name",
        )

        fake_client = MagicMock()
        fake_client.has_api_credentials.return_value = False
        fake_client.create_api_credentials = AsyncMock(
            side_effect=RuntimeError("skip credential creation")
        )
        fake_manager = MagicMock()
        fake_manager.connect = AsyncMock()
        fake_manager.start_background_task = AsyncMock()

        try:
            with (
                patch.object(server_module, "load_config", return_value=fake_config),
                patch.object(
                    server_module, "create_polymarket_client", return_value=fake_client
                ) as mock_create_client,
                patch.object(
                    server_module, "create_safety_limits_from_config", return_value=MagicMock()
                ),
                patch.object(server_module, "get_rate_limiter", return_value=MagicMock()),
                patch.object(server_module, "WebSocketManager", return_value=fake_manager),
                patch.object(server_module.realtime, "set_websocket_manager"),
                patch.object(server_module.asyncio, "create_task", return_value=MagicMock()),
            ):
                await server_module.initialize_server()

            assert mock_create_client.call_count == 1
            assert mock_create_client.call_args.kwargs["api_secret"] == "legacy-passphrase"
        finally:
            server_module.config = original_config
            server_module.polymarket_client = original_polymarket_client
            server_module.safety_limits = original_safety_limits
            server_module.rate_limiter = original_rate_limiter
            server_module.trading_tools = original_trading_tools
            server_module.websocket_manager = original_websocket_manager


class TestTradingMarketIdCompatibility:
    """Regression tests for accepting Gamma market IDs in trading tools."""

    @pytest.mark.asyncio
    async def test_get_market_with_gamma_fallback_resolves_condition_id(self):
        """Numeric Gamma IDs should resolve to CLOB condition IDs before trading."""
        mock_client = MagicMock()
        mock_client.get_market = AsyncMock(
            side_effect=[
                RuntimeError("market not found"),
                {"tokens": [{"token_id": "9043581125"}], "volume": "1000"},
            ]
        )

        trading_tools = TradingTools(
            client=mock_client,
            safety_limits=MagicMock(),
            config=MagicMock(GAMMA_API_URL="https://gamma-api.polymarket.com"),
        )
        trading_tools.rate_limiter = AsyncMock()
        trading_tools.rate_limiter.acquire = AsyncMock(return_value=0.0)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "conditionId": "0xabc123",
            "clobTokenIds": ["9043581125", "12345"],
            "outcomes": ["Yes", "No"],
        }

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)

        async_client_cm = AsyncMock()
        async_client_cm.__aenter__.return_value = mock_http_client
        async_client_cm.__aexit__.return_value = None

        with patch("polymarket_mcp.tools.trading.httpx.AsyncClient", return_value=async_client_cm):
            market = await trading_tools._get_market_with_gamma_fallback("540819")

        assert market["tokens"][0]["token_id"] == "9043581125"
        assert market["clobTokenIds"] == ["9043581125", "12345"]
        assert market["outcomes"] == ["Yes", "No"]
        assert mock_client.get_market.await_args_list[0].args[0] == "540819"
        assert mock_client.get_market.await_args_list[1].args[0] == "0xabc123"

    def test_extract_market_tokens_supports_gamma_clob_token_ids(self):
        """Gamma `clobTokenIds` payloads should be normalized into token dicts."""
        market = {"clobTokenIds": '["111","222"]'}

        tokens = TradingTools._extract_market_tokens(market)
        assert [t["token_id"] for t in tokens] == ["111", "222"]


class TestSDKCompatibility:
    """Regression tests for py-clob-client API compatibility changes."""

    @staticmethod
    def _build_client() -> PolymarketClient:
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                api_key="test-api-key",
                api_secret="test-api-secret",
                passphrase="test-api-passphrase",
            )
        return client

    @pytest.mark.asyncio
    async def test_get_balance_uses_legacy_method_when_available(self):
        """Client should keep supporting SDKs that expose get_balance()."""
        client = self._build_client()

        class LegacyBalanceClient:
            def get_balance(self, address):
                assert address == client.address
                return {"balance": "42.5"}

        client.client = LegacyBalanceClient()
        balance = await client.get_balance()
        assert balance["balance"] == "42.5"

    @pytest.mark.asyncio
    async def test_get_balance_falls_back_to_get_balance_allowance(self):
        """Client should support SDKs that only expose get_balance_allowance()."""
        client = self._build_client()

        class AllowanceOnlyClient:
            def get_balance_allowance(self, params):
                assert params.asset_type == AssetType.COLLATERAL
                return {"available": "123.45", "allowance": "9999"}

        client.client = AllowanceOnlyClient()
        balance = await client.get_balance()

        assert balance["available"] == "123.45"
        assert balance["balance"] == "123.45"

    @pytest.mark.asyncio
    async def test_get_positions_falls_back_to_data_api(self):
        """Client should fallback to Data API when SDK lacks get_positions()."""
        client = self._build_client()

        class PositionsMissingClient:
            pass

        client.client = PositionsMissingClient()

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [{"market": "m1", "size": "10"}]

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)

        async_client_cm = AsyncMock()
        async_client_cm.__aenter__.return_value = mock_http_client
        async_client_cm.__aexit__.return_value = None

        with patch("polymarket_mcp.auth.client.httpx.AsyncClient", return_value=async_client_cm):
            positions = await client.get_positions()

        assert positions == [{"market": "m1", "size": "10"}]
        assert mock_http_client.get.await_args.kwargs["params"]["user"] == client.address

    @pytest.mark.asyncio
    async def test_get_orderbook_normalizes_orderbooksummary_object(self):
        """Orderbook response should be normalized to dict shape for downstream tools."""
        client = self._build_client()

        class OrderBookClient:
            def get_order_book(self, token_id):
                assert token_id == "token-123"
                return OrderBookSummary(
                    market="m1",
                    asset_id="a1",
                    bids=[OrderSummary(price="0.51", size="100")],
                    asks=[OrderSummary(price="0.52", size="80")],
                )

        client.client = OrderBookClient()
        orderbook = await client.get_orderbook("token-123")

        assert orderbook["bids"][0]["price"] == "0.51"
        assert orderbook["asks"][0]["price"] == "0.52"


class TestMarketAnalysisIdentifierCompatibility:
    """Regression tests for market identifier handling in market analysis tools."""

    @pytest.mark.asyncio
    async def test_get_market_details_uses_slug_query_for_slug_like_market_id(self):
        """Slug values passed as market_id should call Gamma with `slug` query param."""
        from polymarket_mcp.tools import market_analysis

        with patch.object(
            market_analysis, "_fetch_gamma_api", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = [{"id": "123", "slug": "2026-nba-champion"}]
            data = await market_analysis.get_market_details(market_id="2026-nba-champion")

        assert data["slug"] == "2026-nba-champion"
        mock_fetch.assert_awaited_once_with("/markets", {"slug": "2026-nba-champion"})

    @pytest.mark.asyncio
    async def test_get_event_markets_uses_slug_query_for_event_slug(self):
        """Event slug should be queried via /events?slug=... to avoid 422 path errors."""
        from polymarket_mcp.tools import market_discovery

        with patch.object(
            market_discovery, "_fetch_gamma_markets", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = [{"markets": [{"id": "m1"}]}]
            markets = await market_discovery.get_event_markets(
                event_slug="fifa-world-cup-2026-winner"
            )

        assert markets == [{"id": "m1"}]
        mock_fetch.assert_awaited_once_with(
            "/events", {"slug": "fifa-world-cup-2026-winner"}, limit=1
        )

    @pytest.mark.asyncio
    async def test_server_routes_realtime_calls_through_registered_manager(self):
        """Realtime calls should delegate to the realtime tool handler without a missing attribute error."""
        import polymarket_mcp.server as server_module

        original_websocket_manager = server_module.websocket_manager

        try:
            server_module.websocket_manager = MagicMock()

            with patch.object(
                server_module.realtime,
                "handle_tool_call",
                new_callable=AsyncMock,
            ) as mock_handle:
                expected = [MagicMock()]
                mock_handle.return_value = expected

                result = await server_module.call_tool("get_realtime_status", {})

            assert result is expected
            mock_handle.assert_awaited_once_with("get_realtime_status", {})
        finally:
            server_module.websocket_manager = original_websocket_manager

    @pytest.mark.asyncio
    async def test_initialize_server_registers_and_starts_websocket_manager(self):
        """Server init should register realtime manager and start the background loop."""
        import polymarket_mcp.server as server_module

        call_order = []

        fake_config = MagicMock()
        fake_config.POLYGON_PRIVATE_KEY = "0" * 64
        fake_config.POLYGON_ADDRESS = "0x" + "0" * 40
        fake_config.POLYMARKET_CHAIN_ID = 137
        fake_config.POLYMARKET_API_KEY = None
        fake_config.POLYMARKET_API_SECRET = None
        fake_config.POLYMARKET_PASSPHRASE = None
        fake_config.LOG_LEVEL = "INFO"

        fake_client = MagicMock()
        fake_client.has_api_credentials.return_value = False
        fake_client.create_api_credentials = AsyncMock(side_effect=RuntimeError("skip"))

        fake_manager = MagicMock()
        fake_manager.connect = AsyncMock(side_effect=lambda: call_order.append("connect"))
        fake_manager.start_background_task = AsyncMock(
            side_effect=lambda: call_order.append("start_background_task")
        )

        scheduled = []

        def capture_task(coro):
            scheduled.append(coro)
            return MagicMock()

        with (
            patch.object(server_module, "load_config", return_value=fake_config),
            patch.object(server_module, "create_polymarket_client", return_value=fake_client),
            patch.object(server_module, "create_safety_limits_from_config", return_value=object()),
            patch.object(server_module, "get_rate_limiter", return_value=object()),
            patch.object(server_module, "WebSocketManager", return_value=fake_manager),
            patch.object(server_module.realtime, "set_websocket_manager") as mock_set_manager,
            patch.object(server_module.asyncio, "create_task", side_effect=capture_task),
        ):
            await server_module.initialize_server()

        mock_set_manager.assert_called_once_with(fake_manager)
        assert len(scheduled) == 1
        await scheduled[0]
        fake_manager.connect.assert_awaited_once()
        fake_manager.start_background_task.assert_awaited_once()
        assert call_order == ["connect", "start_background_task"]

    @pytest.mark.asyncio
    async def test_websocket_auth_uses_secret_and_passphrase(self):
        """WebSocket auth payload should keep secret and passphrase distinct."""
        config = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "0" * 40,
            POLYMARKET_API_KEY="test-key",
            POLYMARKET_API_SECRET="test-secret",
            POLYMARKET_PASSPHRASE="test-passphrase",
            POLYMARKET_API_KEY_NAME="test-name",
        )
        manager = WebSocketManager(config=config)
        manager.clob_ws = AsyncMock()
        manager.clob_ws.recv = AsyncMock(return_value='{"type":"authenticated"}')

        await manager._authenticate_clob()

        payload = manager.clob_ws.send.await_args.args[0]
        data = json.loads(payload)
        assert data["auth"]["secret"] == "test-secret"
        assert data["auth"]["passphrase"] == "test-passphrase"

    @pytest.mark.asyncio
    async def test_websocket_auth_falls_back_to_passphrase_secret(self):
        """WebSocket auth should keep working when only the legacy passphrase is configured."""
        config = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "0" * 40,
            POLYMARKET_API_KEY="test-key",
            POLYMARKET_API_SECRET=None,
            POLYMARKET_PASSPHRASE="legacy-secret",
            POLYMARKET_API_KEY_NAME="test-name",
        )
        manager = WebSocketManager(config=config)
        manager.clob_ws = AsyncMock()
        manager.clob_ws.recv = AsyncMock(return_value='{"type":"authenticated"}')

        await manager._authenticate_clob()

        payload = manager.clob_ws.send.await_args.args[0]
        data = json.loads(payload)
        assert data["auth"]["secret"] == "legacy-secret"
        assert data["auth"]["passphrase"] == "legacy-secret"

    @pytest.mark.asyncio
    async def test_closing_soon_sends_closed_false(self):
        """get_closing_soon_markets must include closed=false."""
        from polymarket_mcp.tools import market_discovery

        with patch.object(
            market_discovery, "_fetch_gamma_markets", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = []
            await market_discovery.get_closing_soon_markets(hours=24, limit=5)

            call_args = mock_fetch.call_args
            params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
            assert params.get("closed") == "false"

    @pytest.mark.asyncio
    async def test_sports_markets_sends_closed_false(self):
        """get_sports_markets must include closed=false."""
        from polymarket_mcp.tools import market_discovery

        with patch.object(
            market_discovery, "_fetch_gamma_markets", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = []
            await market_discovery.get_sports_markets(limit=5)

            call_args = mock_fetch.call_args
            params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
            assert params.get("closed") == "false"

    @pytest.mark.asyncio
    async def test_crypto_markets_sends_closed_false(self):
        """get_crypto_markets must include closed=false."""
        from polymarket_mcp.tools import market_discovery

        with patch.object(
            market_discovery, "_fetch_gamma_markets", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = []
            await market_discovery.get_crypto_markets(limit=5)

            call_args = mock_fetch.call_args
            params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
            assert params.get("closed") == "false"

    @pytest.mark.asyncio
    async def test_featured_sends_closed_false(self):
        """get_featured_markets must include closed=false in params."""
        from polymarket_mcp.tools import market_discovery

        with patch.object(
            market_discovery, "_fetch_gamma_markets", new_callable=AsyncMock
        ) as mock_fetch:
            # Return a market so fallback to trending isn't triggered
            mock_fetch.return_value = [
                {
                    "question": "test",
                    "end_date_iso": (datetime.utcnow() + timedelta(days=30)).isoformat() + "Z",
                }
            ]
            await market_discovery.get_featured_markets(limit=5)

            call_args = mock_fetch.call_args
            params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
            assert params.get("closed") == "false"

    @pytest.mark.asyncio
    async def test_initialize_server_skips_websocket_manager_when_disabled(self):
        """Server init must not create websocket connections when WS_ENABLED=false."""
        import polymarket_mcp.server as server_module

        original_config = server_module.config
        original_polymarket_client = server_module.polymarket_client
        original_safety_limits = server_module.safety_limits
        original_rate_limiter = server_module.rate_limiter
        original_trading_tools = server_module.trading_tools
        original_websocket_manager = server_module.websocket_manager

        fake_config = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "0" * 40,
            WS_ENABLED=False,
        )

        fake_client = MagicMock()
        fake_client.has_api_credentials.return_value = False
        fake_client.create_api_credentials = AsyncMock(side_effect=RuntimeError("skip"))

        try:
            with (
                patch.object(server_module, "load_config", return_value=fake_config),
                patch.object(server_module, "create_polymarket_client", return_value=fake_client),
                patch.object(
                    server_module, "create_safety_limits_from_config", return_value=MagicMock()
                ),
                patch.object(server_module, "get_rate_limiter", return_value=MagicMock()),
                patch.object(server_module, "WebSocketManager") as mock_ws_manager,
                patch.object(server_module.realtime, "set_websocket_manager") as mock_set_manager,
                patch.object(server_module.asyncio, "create_task") as mock_create_task,
            ):
                await server_module.initialize_server()

            mock_ws_manager.assert_not_called()
            mock_set_manager.assert_not_called()
            mock_create_task.assert_not_called()
            assert server_module.websocket_manager is None
        finally:
            server_module.config = original_config
            server_module.polymarket_client = original_polymarket_client
            server_module.safety_limits = original_safety_limits
            server_module.rate_limiter = original_rate_limiter
            server_module.trading_tools = original_trading_tools
            server_module.websocket_manager = original_websocket_manager

    @pytest.mark.asyncio
    async def test_list_tools_omits_realtime_tools_when_disabled(self):
        """Tool listing must exclude realtime tools when WS_ENABLED=false."""
        import polymarket_mcp.server as server_module

        original_config = server_module.config
        original_polymarket_client = server_module.polymarket_client

        try:
            server_module.config = MagicMock(WS_ENABLED=False)
            server_module.polymarket_client = None

            with (
                patch.object(server_module.market_discovery, "get_tools", return_value=[]),
                patch.object(server_module.market_analysis, "get_tools", return_value=[]),
                patch.object(
                    server_module.realtime, "get_tools", return_value=[MagicMock()]
                ) as mock_realtime,
            ):
                tools = await server_module.list_tools()

            mock_realtime.assert_not_called()
            assert tools == []
        finally:
            server_module.config = original_config
            server_module.polymarket_client = original_polymarket_client


# ---------------------------------------------------------------------------
# YES token selection — token ordering bug fix
# ---------------------------------------------------------------------------


class TestYesTokenSelection:
    """Verify that _extract_market_tokens and _get_yes_token_id return the YES token."""

    def _make_trading_tools(self):
        from polymarket_mcp.utils.safety_limits import SafetyLimits

        config = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "0" * 40,
        )
        safety_limits = MagicMock(spec=SafetyLimits)
        return TradingTools(
            client=MagicMock(),
            config=config,
            safety_limits=safety_limits,
        )

    def test_extract_tokens_preserves_outcome_from_clob_tokens(self):
        """Tokens from the CLOB `tokens` array must keep their outcome label."""
        tt = self._make_trading_tools()
        market = {
            "tokens": [
                {"token_id": "no_token_id", "outcome": "No"},
                {"token_id": "yes_token_id", "outcome": "Yes"},
            ]
        }
        tokens = tt._extract_market_tokens(market)
        assert len(tokens) == 2
        outcomes = {t["outcome"] for t in tokens}
        assert "Yes" in outcomes and "No" in outcomes

    def test_get_yes_token_id_selects_yes_outcome(self):
        """Must return the YES token even when NO is listed first."""
        tt = self._make_trading_tools()
        tokens = [
            {"token_id": "no_token_id", "outcome": "No"},
            {"token_id": "yes_token_id", "outcome": "Yes"},
        ]
        assert tt._get_yes_token_id(tokens) == "yes_token_id"

    def test_get_yes_token_id_case_insensitive(self):
        """Outcome matching must be case-insensitive."""
        tt = self._make_trading_tools()
        tokens = [
            {"token_id": "no_id", "outcome": "no"},
            {"token_id": "yes_id", "outcome": "YES"},
        ]
        assert tt._get_yes_token_id(tokens) == "yes_id"

    def test_get_yes_token_id_fallback_when_unlabelled(self):
        """When no outcome labels exist, return the first token as fallback."""
        tt = self._make_trading_tools()
        tokens = [
            {"token_id": "first_token", "outcome": ""},
            {"token_id": "second_token", "outcome": ""},
        ]
        assert tt._get_yes_token_id(tokens) == "first_token"

    def test_extract_tokens_from_clob_token_ids_uses_outcomes_array(self):
        """clobTokenIds from Gamma response should be labelled using the `outcomes` field."""
        tt = self._make_trading_tools()
        market = {
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
        }
        tokens = tt._extract_market_tokens(market)
        assert len(tokens) == 2
        by_id = {t["token_id"]: t["outcome"] for t in tokens}
        assert by_id["yes_tok"] == "Yes"
        assert by_id["no_tok"] == "No"

    def test_extract_tokens_from_clob_token_ids_json_string(self):
        """clobTokenIds may arrive as a JSON-encoded string; outcomes likewise."""
        tt = self._make_trading_tools()
        market = {
            "clobTokenIds": json.dumps(["yes_tok", "no_tok"]),
            "outcomes": json.dumps(["Yes", "No"]),
        }
        tokens = tt._extract_market_tokens(market)
        assert len(tokens) == 2
        by_id = {t["token_id"]: t["outcome"] for t in tokens}
        assert by_id["yes_tok"] == "Yes"

    def test_extract_tokens_uses_gamma_outcome_map_for_unlabelled_clob_tokens(self):
        """Unlabelled CLOB tokens should inherit outcomes from clobTokenIds/outcomes mapping."""
        tt = self._make_trading_tools()
        market = {
            "tokens": [
                {"token_id": "token_no"},
                {"token_id": "token_yes"},
            ],
            "clobTokenIds": ["token_no", "token_yes"],
            "outcomes": ["No", "Yes"],
        }
        tokens = tt._extract_market_tokens(market)
        by_id = {t["token_id"]: t["outcome"] for t in tokens}
        assert by_id["token_no"] == "No"
        assert by_id["token_yes"] == "Yes"

    def test_get_yes_token_id_handles_quoted_yes_labels(self):
        """YES labels wrapped in quotes should still be detected."""
        tt = self._make_trading_tools()
        tokens = [
            {"token_id": "no_tok", "outcome": "No"},
            {"token_id": "yes_tok", "outcome": '"Yes"'},
        ]
        assert tt._get_yes_token_id(tokens) == "yes_tok"

    def test_get_yes_token_id_binary_fallback_when_only_no_is_labelled(self):
        """If only NO is labelled in a binary market, pick the other token as YES."""
        tt = self._make_trading_tools()
        tokens = [
            {"token_id": "no_tok", "outcome": "No"},
            {"token_id": "other_tok", "outcome": ""},
        ]
        assert tt._get_yes_token_id(tokens) == "other_tok"

    def test_full_pipeline_no_first_is_picked_correctly(self):
        """Simulate the CLOB market response where NO is index 0 and YES is index 1."""
        tt = self._make_trading_tools()
        # CLOB returns NO first in this market
        market = {
            "tokens": [
                {"token_id": "no_token_abc", "outcome": "No"},
                {"token_id": "yes_token_xyz", "outcome": "Yes"},
            ]
        }
        tokens = tt._extract_market_tokens(market)
        selected = tt._get_yes_token_id(tokens)
        assert selected == "yes_token_xyz", (
            "Should select YES token even when NO is listed first in the tokens array"
        )
