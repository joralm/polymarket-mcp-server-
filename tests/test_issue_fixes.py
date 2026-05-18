"""
Tests for GitHub issue fixes (#2, #6, #10).

- Issue #10: API credentials must not be logged at INFO level
- Issue #6: fastapi dependency must be compatible with mcp's anyio requirement
- Issue #2: Market discovery must filter out closed/expired markets
"""

import pytest
import json
import os
import logging
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timedelta

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, AssetType, OrderBookSummary, OrderSummary
from py_clob_client_v2.exceptions import PolyApiException
from polymarket_mcp.auth.client import MIN_ALLOWANCE_THRESHOLD, PolymarketClient
from polymarket_mcp.config import PolymarketConfig, load_config
from polymarket_mcp.tools.portfolio import get_portfolio_value, _extract_cash_balance
from polymarket_mcp.tools.trading import TradingTools
from polymarket_mcp.utils.usdc import scale_usdc_atomic_units
from polymarket_mcp.utils.websocket_manager import WebSocketManager

# ---------------------------------------------------------------------------
# Issue #10 — Credential masking in server.py
# ---------------------------------------------------------------------------


class TestCredentialMasking:
    """Verify that API credentials are never logged in full."""

    def test_server_logs_truncated_key(self):
        """API key presence should be logged at DEBUG level, never at INFO.

        No part of the credential value should be emitted to log files, which
        might be aggregated to external systems. The log must be at DEBUG (not
        INFO) so it is not emitted in production deployments by default.
        """
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

        # Must contain a DEBUG log that references POLYMARKET_API_KEY
        assert (
            "logger.debug" in source_code and "POLYMARKET_API_KEY" in source_code
        ), "API key presence should be logged at DEBUG level"

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

    def test_parse_market_end_datetime_supports_iso_and_timestamp(self):
        """End-date parser should normalize both ISO-Z strings and unix timestamps to UTC."""
        from polymarket_mcp.tools.market_discovery import _parse_market_end_datetime

        iso_dt = _parse_market_end_datetime("2026-07-31T12:00:00Z")
        ts_dt = _parse_market_end_datetime(1785499200)  # 2026-07-31T12:00:00+00:00

        assert iso_dt is not None and iso_dt.tzinfo is not None
        assert ts_dt is not None and ts_dt.tzinfo is not None
        assert iso_dt == ts_dt

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
    async def test_server_prefers_relayer_triplet_when_present(self):
        """Server initialization should prioritize POLYMARKET_RELAYER_* over legacy aliases."""
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
            POLYMARKET_API_SECRET="legacy-secret",
            POLYMARKET_PASSPHRASE="legacy-passphrase",
            POLYMARKET_RELAYER_KEY="relayer-key",
            POLYMARKET_RELAYER_SECRET="relayer-secret",
            POLYMARKET_RELAYER_PASSPHRASE="relayer-passphrase",
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
            assert mock_create_client.call_args.kwargs["api_key"] == "relayer-key"
            assert mock_create_client.call_args.kwargs["api_secret"] == "relayer-secret"
            assert mock_create_client.call_args.kwargs["passphrase"] == "relayer-passphrase"
            assert mock_create_client.call_args.kwargs["host"] == fake_config.CLOB_API_URL
        finally:
            server_module.config = original_config
            server_module.polymarket_client = original_polymarket_client
            server_module.safety_limits = original_safety_limits
            server_module.rate_limiter = original_rate_limiter
            server_module.trading_tools = original_trading_tools
            server_module.websocket_manager = original_websocket_manager


class TestWalletConfigFlow:
    """Regression tests for MetaMask wallet configuration flow."""

    def test_test_fixture_sets_mainnet_env_defaults(self):
        assert os.environ.get("POLYMARKET_ENV") == "mainnet"
        assert os.environ.get("POLYMARKET_CHAIN_ID") == "137"
        assert os.environ.get("CLOB_API_URL") == "https://clob.polymarket.com"
        assert os.environ.get("GAMMA_API_URL") == "https://gamma-api.polymarket.com"
        assert os.environ.get("POLYMARKET_GEOBLOCK_URL") == "https://polymarket.com/api/geoblock"

    def test_config_demo_mode_does_not_inject_wallet_defaults(self):
        cfg = PolymarketConfig(DEMO_MODE=True)

        assert cfg.POLYGON_PRIVATE_KEY == ""
        assert cfg.POLYGON_ADDRESS == ""

    def test_config_rejects_unsupported_signature_type(self):
        with pytest.raises(
            ValueError,
            match=r"must be 0 \(EOA direct mode\), 2 \(POLY_PROXY/proxy wallet\), or 3",
        ):
            PolymarketConfig(
                POLYGON_PRIVATE_KEY="0" * 64,
                POLYGON_ADDRESS="0x" + "1" * 40,
                POLYMARKET_SIGNATURE_TYPE=1,
            )

    def test_config_accepts_eoa_signature_flow(self):
        cfg = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "1" * 40,
            POLYMARKET_SIGNATURE_TYPE=0,
        )
        assert cfg.POLYMARKET_SIGNATURE_TYPE == 0

    def test_config_accepts_poly_proxy_signature_flow(self):
        cfg = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "1" * 40,
            POLYMARKET_SIGNATURE_TYPE=2,
        )
        assert cfg.POLYMARKET_SIGNATURE_TYPE == 2

    @pytest.mark.asyncio
    async def test_web_dashboard_initializes_client_with_wallet_and_api_credentials(self):
        import polymarket_mcp.web.app as web_app_module

        fake_config = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "1" * 40,
        )

        with (
            patch.object(web_app_module, "load_config", return_value=fake_config),
            patch.object(web_app_module, "create_polymarket_client") as mock_create,
            patch.object(
                web_app_module, "create_safety_limits_from_config", return_value=MagicMock()
            ),
        ):
            await web_app_module.load_mcp_config()

        assert mock_create.call_args.kwargs["private_key"] == fake_config.POLYGON_PRIVATE_KEY
        assert mock_create.call_args.kwargs["address"] == fake_config.POLYGON_ADDRESS
        assert mock_create.call_args.kwargs["chain_id"] == fake_config.POLYMARKET_CHAIN_ID
        assert (
            mock_create.call_args.kwargs["signature_type"] == fake_config.POLYMARKET_SIGNATURE_TYPE
        )
        assert mock_create.call_args.kwargs["funder"] == fake_config.effective_funder
        assert mock_create.call_args.kwargs["host"] == fake_config.CLOB_API_URL

    @pytest.mark.asyncio
    async def test_web_dashboard_demo_mode_skips_wallet_client_initialization(self):
        import polymarket_mcp.web.app as web_app_module

        fake_config = PolymarketConfig(DEMO_MODE=True)

        with (
            patch.object(web_app_module, "load_config", return_value=fake_config),
            patch.object(web_app_module, "create_polymarket_client") as mock_create,
            patch.object(
                web_app_module, "create_safety_limits_from_config", return_value=MagicMock()
            ),
        ):
            await web_app_module.load_mcp_config()

        mock_create.assert_not_called()

    def test_config_applies_testnet_defaults_from_environment_switch(self):
        cfg = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "1" * 40,
            POLYMARKET_ENV="testnet",
            POLYMARKET_TEST_CHAIN_ID=80002,
            CLOB_API_TEST_URL="https://clob-testnet.polytest.cloud",
            GAMMA_API_TEST_URL="https://gcomm-api.polytest.cloud",
        )

        assert cfg.POLYMARKET_CHAIN_ID == 80002
        assert cfg.CLOB_API_URL == "https://clob-testnet.polytest.cloud"
        assert cfg.GAMMA_API_URL == "https://gcomm-api.polytest.cloud"

    def test_config_ignores_mainnet_variables_when_testnet_selected(self):
        cfg = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "1" * 40,
            POLYMARKET_ENV="testnet",
            POLYMARKET_CHAIN_ID=137,
            CLOB_API_URL="https://custom-clob.example",
            GAMMA_API_URL="https://custom-gamma.example",
        )

        assert cfg.polymarket_ready is False
        assert cfg.POLYMARKET_CHAIN_ID is None
        assert cfg.CLOB_API_URL is None
        assert cfg.GAMMA_API_URL is None
        assert "POLYMARKET_TEST_CHAIN_ID" in (cfg.polymarket_config_error or "")

    def test_load_config_reads_testnet_environment_variables(self):
        original = {
            key: os.environ.get(key)
            for key in (
                "POLYMARKET_ENV",
                "POLYMARKET_CHAIN_ID",
                "POLYMARKET_TEST_CHAIN_ID",
                "CLOB_API_URL",
                "GAMMA_API_URL",
                "CLOB_API_TEST_URL",
                "GAMMA_API_TEST_URL",
                "POLYMARKET_GEOBLOCK_URL",
                "POLYGON_PRIVATE_KEY",
                "POLYGON_ADDRESS",
            )
        }

        os.environ["POLYMARKET_ENV"] = "testnet"
        os.environ.pop("POLYMARKET_CHAIN_ID", None)
        os.environ.pop("CLOB_API_URL", None)
        os.environ.pop("GAMMA_API_URL", None)
        os.environ["POLYMARKET_TEST_CHAIN_ID"] = "80002"
        os.environ["CLOB_API_TEST_URL"] = "https://clob-testnet.polytest.cloud"
        os.environ["GAMMA_API_TEST_URL"] = "https://gcomm-api.polytest.cloud"
        os.environ["POLYMARKET_GEOBLOCK_URL"] = "https://polymarket.com/api/geoblock"
        os.environ["POLYGON_PRIVATE_KEY"] = "0" * 64
        os.environ["POLYGON_ADDRESS"] = "0x" + "1" * 40

        try:
            cfg = load_config()
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        assert cfg.polymarket_ready is True
        assert cfg.POLYMARKET_ENV == "testnet"
        assert cfg.POLYMARKET_CHAIN_ID == 80002
        assert cfg.CLOB_API_URL == "https://clob-testnet.polytest.cloud"
        assert cfg.GAMMA_API_URL == "https://gcomm-api.polytest.cloud"

    def test_load_config_marks_polymarket_unready_when_env_missing(self):
        original = {
            key: os.environ.get(key)
            for key in (
                "POLYMARKET_ENV",
                "POLYMARKET_CHAIN_ID",
                "POLYMARKET_TEST_CHAIN_ID",
                "CLOB_API_URL",
                "GAMMA_API_URL",
                "CLOB_API_TEST_URL",
                "GAMMA_API_TEST_URL",
                "POLYMARKET_GEOBLOCK_URL",
                "POLYGON_PRIVATE_KEY",
                "POLYGON_ADDRESS",
            )
        }

        for key in (
            "POLYMARKET_ENV",
            "POLYMARKET_CHAIN_ID",
            "POLYMARKET_TEST_CHAIN_ID",
            "CLOB_API_URL",
            "GAMMA_API_URL",
            "CLOB_API_TEST_URL",
            "GAMMA_API_TEST_URL",
        ):
            os.environ.pop(key, None)

        os.environ["POLYMARKET_GEOBLOCK_URL"] = "https://polymarket.com/api/geoblock"
        os.environ["POLYGON_PRIVATE_KEY"] = "0" * 64
        os.environ["POLYGON_ADDRESS"] = "0x" + "1" * 40

        try:
            cfg = load_config()
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        assert cfg.polymarket_ready is False
        assert cfg.POLYMARKET_CHAIN_ID is None
        assert cfg.CLOB_API_URL is None
        assert cfg.GAMMA_API_URL is None
        assert cfg.polymarket_config_error == "POLYMARKET_ENV is not set"

    def test_load_config_allows_missing_geoblock_url(self):
        original = {
            key: os.environ.get(key)
            for key in (
                "POLYMARKET_ENV",
                "POLYMARKET_CHAIN_ID",
                "CLOB_API_URL",
                "GAMMA_API_URL",
                "POLYMARKET_GEOBLOCK_URL",
                "POLYGON_PRIVATE_KEY",
                "POLYGON_ADDRESS",
            )
        }

        os.environ["POLYMARKET_ENV"] = "mainnet"
        os.environ["POLYMARKET_CHAIN_ID"] = "137"
        os.environ["CLOB_API_URL"] = "https://clob.polymarket.com"
        os.environ["GAMMA_API_URL"] = "https://gamma-api.polymarket.com"
        os.environ.pop("POLYMARKET_GEOBLOCK_URL", None)
        os.environ["POLYGON_PRIVATE_KEY"] = "0" * 64
        os.environ["POLYGON_ADDRESS"] = "0x" + "1" * 40

        try:
            cfg = load_config()
            assert cfg.POLYMARKET_GEOBLOCK_URL is None
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


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
                signature_type=2,
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
    async def test_get_balance_legacy_method_uses_funder_address_when_distinct(self):
        """Legacy balance SDK calls must query the funding/deposit wallet, not signer EOA."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                funder="0x" + "2" * 40,
                signature_type=3,
                api_key="test-api-key",
                api_secret="test-api-secret",
                passphrase="test-api-passphrase",
            )

        class MockLegacyBalanceClient:
            def get_balance(self, address):
                assert address == "0x" + "2" * 40
                return {"balance": "42.5"}

        client.client = MockLegacyBalanceClient()
        balance = await client.get_balance()
        assert balance["balance"] == "42.5"

    @pytest.mark.asyncio
    async def test_post_order_maps_maker_address_error_to_actionable_message(self):
        client = self._build_client()

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {
            "error": "maker address not allowed, please use the deposit wallet flow"
        }
        client.client = MagicMock()
        client.client.create_and_post_order.side_effect = PolyApiException(resp=mock_resp)

        with pytest.raises(RuntimeError) as exc_info:
            await client.post_order(
                token_id="123",
                price=0.5,
                size=1,
                side="BUY",
            )

        error_message = str(exc_info.value)
        assert "MetaMask-linked" in error_message
        assert "maker address" in error_message

    @pytest.mark.asyncio
    async def test_post_order_refreshes_and_retries_when_signer_api_key_mismatch(self):
        client = self._build_client()

        mismatch_resp = MagicMock()
        mismatch_resp.status_code = 400
        mismatch_resp.json.return_value = {
            "error": "the order signer address has to be the address of the API KEY"
        }

        client.client = MagicMock()
        client.client.create_and_post_order.side_effect = [
            PolyApiException(resp=mismatch_resp),
            {"orderID": "ord-1", "status": "live"},
        ]
        with patch.object(client, "_refresh_api_credentials") as mock_refresh:
            response = await client.post_order(
                token_id="123",
                price=0.5,
                size=1,
                side="BUY",
            )

        mock_refresh.assert_called_once()
        assert client.client.create_and_post_order.call_count == 2
        assert response["orderID"] == "ord-1"

    @pytest.mark.asyncio
    async def test_post_order_type3_does_not_refresh_legacy_api_keys(self):
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                signature_type=3,
                api_key="legacy-key",
                api_secret="legacy-secret",
                passphrase="legacy-pass",
            )

        mismatch_resp = MagicMock()
        mismatch_resp.status_code = 400
        mismatch_resp.json.return_value = {
            "error": "the order signer address has to be the address of the API KEY"
        }

        client.client = MagicMock()
        client.client.create_and_post_order.side_effect = PolyApiException(resp=mismatch_resp)

        with patch.object(client, "_refresh_api_credentials") as mock_refresh:
            with pytest.raises(RuntimeError, match="Type-3 signer flow"):
                await client.post_order(
                    token_id="123",
                    price=0.5,
                    size=1,
                    side="BUY",
                )

        mock_refresh.assert_not_called()

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
    async def test_get_balance_prefers_available_over_total_balance(self):
        """Canonical `balance` should reflect spendable cash, not locked-inclusive total."""
        client = self._build_client()

        class MixedBalanceClient:
            def get_balance_allowance(self, params):
                assert params.asset_type == AssetType.COLLATERAL
                return {"balance": "1.93", "available": "0.93", "allowance": "1000"}

        client.client = MixedBalanceClient()
        balance = await client.get_balance()

        assert balance["balance"] == "0.93"
        assert balance["available"] == "0.93"
        assert balance["currency"] == "USDC"

    @pytest.mark.asyncio
    async def test_get_balance_parses_currency_formatted_values(self):
        """Balance parser should tolerate mixed currency symbols/labels from SDK variants.

        Some wrappers/localized intermediaries may include display symbols even
        when underlying units are USDC. We canonicalize spendable balance from
        the `available` field.
        """
        client = self._build_client()

        class CurrencyFormattedClient:
            def get_balance_allowance(self, params):
                assert params.asset_type == AssetType.COLLATERAL
                return {"available": "€0.93 USDC", "balance": "$1.93"}

        client.client = CurrencyFormattedClient()
        balance = await client.get_balance()

        assert balance["balance"] == "0.93"
        assert balance["available"] == "0.93"
        assert balance["currency"] == "USDC"

    @pytest.mark.asyncio
    async def test_get_balance_scales_raw_usdc_atomic_units(self):
        """Raw USDC atomic units (6 decimals) should be normalized before formatting."""
        client = self._build_client()

        class AtomicUnitsClient:
            def get_balance_allowance(self, params):
                assert params.asset_type == AssetType.COLLATERAL
                return {"available": "7488975", "balance": "7488975"}

        client.client = AtomicUnitsClient()
        balance = await client.get_balance()

        assert balance["balance"] == "7.488975"
        assert balance["available"] == "7.488975"
        assert balance["currency"] == "USDC"

    @pytest.mark.asyncio
    async def test_get_balance_uses_available_balance_when_available_is_absent(self):
        """Canonical balance should use available_balance when available is missing."""
        client = self._build_client()

        class AvailableBalanceOnlyClient:
            def get_balance_allowance(self, params):
                assert params.asset_type == AssetType.COLLATERAL
                return {"balance": "3.00", "available_balance": "1.25"}

        client.client = AvailableBalanceOnlyClient()
        balance = await client.get_balance()

        assert balance["balance"] == "1.25"
        assert balance["available"] == "1.25"
        assert balance["available_balance"] == "1.25"

    @pytest.mark.asyncio
    async def test_get_balance_uses_total_balance_when_available_fields_absent(self):
        """Canonical balance should fall back to total balance if no available field exists."""
        client = self._build_client()

        class BalanceOnlyClient:
            def get_balance_allowance(self, params):
                assert params.asset_type == AssetType.COLLATERAL
                return {"balance": "2.50"}

        client.client = BalanceOnlyClient()
        balance = await client.get_balance()

        assert balance["balance"] == "2.5"
        assert balance["currency"] == "USDC"

    @pytest.mark.asyncio
    async def test_get_balance_returns_zero_when_fields_missing_or_empty(self):
        """Canonical balance should be zero when payload has no parseable balance fields."""
        client = self._build_client()

        class EmptyBalanceClient:
            def get_balance_allowance(self, params):
                assert params.asset_type == AssetType.COLLATERAL
                return {"available": "", "available_balance": None, "balance": ""}

        client.client = EmptyBalanceClient()
        balance = await client.get_balance()

        assert balance["balance"] == "0.0"
        assert balance["currency"] == "USDC"

    @pytest.mark.asyncio
    async def test_get_positions_falls_back_to_data_api(self):
        """Client should fallback to Data API when SDK lacks get_positions()."""
        client = self._build_client()

        class MockClientWithoutPositionsMethod:
            pass

        client.client = MockClientWithoutPositionsMethod()

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
        assert mock_http_client.get.await_args.kwargs["params"]["user"] == client.funder_address

    @pytest.mark.asyncio
    async def test_get_positions_uses_configured_funder_address(self):
        """Data API position queries must use funder/deposit wallet, not signer EOA."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                funder="0x" + "2" * 40,
                signature_type=3,
                api_key="test-api-key",
                api_secret="test-api-secret",
                passphrase="test-api-passphrase",
            )

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
            await client.get_positions()

        assert mock_http_client.get.await_args.kwargs["params"]["user"] == "0x" + "2" * 40

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

    @pytest.mark.asyncio
    async def test_get_orderbook_sorts_bids_desc_asks_asc(self):
        """Orderbook bids must be sorted highest-first and asks lowest-first.

        Regression test for the bug where asks[0] returned the *worst* (highest)
        ask price instead of the best (lowest) one, causing best_ask=0.99 for a
        YES token genuinely priced around 0.59.
        """
        client = self._build_client()

        class OrderBookClient:
            def get_order_book(self, token_id):
                # Simulate API response with asks in descending order (worst first)
                # and bids in ascending order (worst first) — the problematic case.
                return OrderBookSummary(
                    market="m1",
                    asset_id="a1",
                    bids=[
                        OrderSummary(price="0.01", size="5"),  # worst bid first
                        OrderSummary(price="0.57", size="100"),
                        OrderSummary(price="0.58", size="200"),  # best bid last
                    ],
                    asks=[
                        OrderSummary(price="0.99", size="5"),  # worst ask first
                        OrderSummary(price="0.61", size="100"),
                        OrderSummary(price="0.59", size="200"),  # best ask last
                    ],
                )

        client.client = OrderBookClient()
        orderbook = await client.get_orderbook("token-123")

        bids = orderbook["bids"]
        asks = orderbook["asks"]

        # best bid must be at index 0 (highest bid)
        assert (
            float(bids[0]["price"]) == 0.58
        ), f"Expected best bid 0.58 at index 0, got {bids[0]['price']}"
        # best ask must be at index 0 (lowest ask)
        assert float(asks[0]["price"]) == 0.59, (
            f"Expected best ask 0.59 at index 0, got {asks[0]['price']} — "
            "this is the bug that made suggest_order_price return 0.99 instead of 0.59"
        )

    @pytest.mark.asyncio
    async def test_get_orderbook_already_sorted_unchanged(self):
        """Orderbook that is already correctly sorted should not be reordered."""
        client = self._build_client()

        class OrderBookClient:
            def get_order_book(self, token_id):
                return OrderBookSummary(
                    market="m1",
                    asset_id="a1",
                    bids=[
                        OrderSummary(price="0.58", size="200"),  # best bid first
                        OrderSummary(price="0.57", size="100"),
                        OrderSummary(price="0.01", size="5"),
                    ],
                    asks=[
                        OrderSummary(price="0.59", size="200"),  # best ask first
                        OrderSummary(price="0.61", size="100"),
                        OrderSummary(price="0.99", size="5"),
                    ],
                )

        client.client = OrderBookClient()
        orderbook = await client.get_orderbook("token-123")

        assert float(orderbook["bids"][0]["price"]) == 0.58
        assert float(orderbook["asks"][0]["price"]) == 0.59

    @pytest.mark.asyncio
    async def test_get_balance_auto_refreshes_on_401(self):
        """get_balance() must re-derive API credentials and retry when it receives HTTP 401.

        Regression test for the observed failure:
          'Unauthorized/Invalid api key' (status 401) returned by balance-allowance endpoint
        when the stored API key in env vars is stale.
        """
        from py_clob_client_v2.exceptions import PolyApiException

        client = self._build_client()

        call_count = 0
        refreshed = False

        class FakeResponse:
            status_code = 401

            def json(self):
                return {"error": "Unauthorized/Invalid api key"}

        class BalanceAllowanceClient:
            def get_balance_allowance(self_, params):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # Simulate stale key returning 401 on first call
                    raise PolyApiException(resp=FakeResponse())
                # Second call (after refresh) succeeds
                return {"available": "50.0", "allowance": "1000"}

            def create_or_derive_api_key(self_):
                nonlocal refreshed
                refreshed = True
                return ApiCreds(
                    api_key="refreshed-api-key",
                    api_secret="refreshed-secret",
                    api_passphrase="refreshed-passphrase",
                )

            def set_api_creds(self_, creds):
                pass

        client.client = BalanceAllowanceClient()
        balance = await client.get_balance()

        assert refreshed, "Credentials should have been refreshed on 401"
        assert call_count == 2, f"Balance should have been called twice (got {call_count})"
        assert balance["available"] == "50.0"
        assert balance["balance"] == "50.0"

    @pytest.mark.asyncio
    async def test_get_balance_does_not_retry_on_non_401_error(self):
        """get_balance() must NOT attempt credential refresh for non-401 errors."""
        from py_clob_client_v2.exceptions import PolyApiException

        client = self._build_client()

        call_count = 0

        class FakeResponse403:
            status_code = 403

            def json(self):
                return {"error": "Forbidden"}

        class FailingClient:
            def get_balance_allowance(self_, params):
                nonlocal call_count
                call_count += 1
                raise PolyApiException(resp=FakeResponse403())

            def create_or_derive_api_key(self_):
                pytest.fail("create_or_derive_api_key should not be called for non-401 errors")

        client.client = FailingClient()
        with pytest.raises(PolyApiException) as exc_info:
            await client.get_balance()

        assert exc_info.value.status_code == 403
        assert call_count == 1, "Should not retry on non-401 errors"

    @pytest.mark.asyncio
    async def test_get_balance_refreshes_proxy_credentials_once_when_zero_from_configured_creds(
        self,
    ):
        """Configured legacy creds returning zero should trigger one proxy-mode refresh attempt."""
        client = self._build_client()

        call_count = 0
        refreshed = 0

        class MockBalanceClientReturningZeroThenValue:
            def get_balance_allowance(self_, params):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return {"balance": "0", "available": "0"}
                return {"balance": "1.93", "available": "0.93"}

            def create_or_derive_api_key(self_):
                nonlocal refreshed
                refreshed += 1
                return ApiCreds(
                    api_key="proxy-refresh-key",
                    api_secret="proxy-refresh-secret",
                    api_passphrase="proxy-refresh-passphrase",
                )

            def set_api_creds(self_, creds):
                pass

        client.client = MockBalanceClientReturningZeroThenValue()
        balance = await client.get_balance()

        assert refreshed == 1, "Zero balance with configured creds should trigger one refresh"
        assert call_count == 2, "Balance call should retry once after proxy refresh"
        # Canonical `balance` follows spendable cash (`available`) by design.
        assert balance["balance"] == "0.93"
        assert balance["available"] == "0.93"

    @pytest.mark.asyncio
    async def test_get_balance_does_not_refresh_on_zero_when_creds_not_from_config(self):
        """Auto-derived creds should not loop-refresh when the wallet truly has zero balance."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
            )
        client.api_creds = ApiCreds(
            api_key="runtime-key",
            api_secret="runtime-secret",
            api_passphrase="runtime-pass",
        )

        class MockZeroBalanceClient:
            def get_balance_allowance(self_, params):
                return {"balance": "0", "available": "0"}

            def create_or_derive_api_key(self_):
                pytest.fail("Should not refresh proxy credentials when creds were not configured")

        client.client = MockZeroBalanceClient()
        balance = await client.get_balance()

        assert balance["balance"] == "0.0"


class TestClobClientSignatureType:
    """Regression: ClobClient must be initialized with explicit wallet auth config.

    Without the right signature type / funder combination, balance and position
    queries target the wrong wallet and diverge from the Polymarket UI.
    """

    def test_initialize_client_uses_deposit_wallet_signature_type_by_default(self):
        """ClobClient defaults to signature_type=3 for MetaMask deposit-wallet flow."""
        captured_args = {}

        def fake_clob_init(self_inner, **kwargs):
            captured_args.update(kwargs)
            # Prevent real network activity by leaving attributes unset;
            # the test only cares about what was passed.
            self_inner.host = kwargs.get("host", "")
            self_inner.chain_id = kwargs.get("chain_id", 137)
            self_inner.signer = None
            self_inner.creds = None
            self_inner.mode = 0
            self_inner.builder = MagicMock()
            self_inner.use_server_time = False
            self_inner.retry_on_error = False
            self_inner.builder_config = None
            self_inner.fee_slippage = 0
            self_inner._ClobClient__tick_sizes = {}
            self_inner._ClobClient__neg_risk = {}
            self_inner._ClobClient__fee_rates = {}

        with patch.object(ClobClient, "__init__", fake_clob_init):
            # Instantiate only to verify the kwargs forwarded into ClobClient.__init__.
            PolymarketClient(
                private_key="0" * 64,
                address="0x" + "a" * 40,
            )

        assert captured_args.get("signature_type") == 3, (
            "ClobClient default signature_type should be 3 (POLY_1271/deposit wallet) "
            "for current MetaMask deposit-wallet API flows"
        )
        assert (
            captured_args.get("funder") == "0x" + "a" * 40
        ), "funder must default to the user's signer address when POLYMARKET_FUNDER is not provided"

    def test_initialize_client_accepts_distinct_funder_and_signature_type(self):
        """New deposit-wallet flows must pass explicit funder + signature_type through."""
        captured_args = {}

        def fake_clob_init(self_inner, **kwargs):
            captured_args.update(kwargs)
            self_inner.host = kwargs.get("host", "")
            self_inner.chain_id = kwargs.get("chain_id", 137)
            self_inner.signer = None
            self_inner.creds = None
            self_inner.mode = 0
            self_inner.builder = MagicMock()
            self_inner.use_server_time = False
            self_inner.retry_on_error = False
            self_inner.builder_config = None
            self_inner.fee_slippage = 0
            self_inner._ClobClient__tick_sizes = {}
            self_inner._ClobClient__neg_risk = {}
            self_inner._ClobClient__fee_rates = {}

        with patch.object(ClobClient, "__init__", fake_clob_init):
            PolymarketClient(
                private_key="0" * 64,
                address="0x" + "a" * 40,
                funder="0x" + "b" * 40,
                signature_type=3,
            )

        assert captured_args.get("signature_type") == 3
        assert captured_args.get("funder") == "0x" + "b" * 40

    def test_initialize_client_accepts_poly_proxy_signature_type(self):
        """POLY_PROXY mode (signature_type=2) must be forwarded unchanged."""
        captured_args = {}

        def fake_clob_init(self_inner, **kwargs):
            captured_args.update(kwargs)
            self_inner.host = kwargs.get("host", "")
            self_inner.chain_id = kwargs.get("chain_id", 137)
            self_inner.signer = None
            self_inner.creds = None
            self_inner.mode = 0
            self_inner.builder = MagicMock()
            self_inner.use_server_time = False
            self_inner.retry_on_error = False
            self_inner.builder_config = None
            self_inner.fee_slippage = 0
            self_inner._ClobClient__tick_sizes = {}
            self_inner._ClobClient__neg_risk = {}
            self_inner._ClobClient__fee_rates = {}

        with patch.object(ClobClient, "__init__", fake_clob_init):
            PolymarketClient(
                private_key="0" * 64,
                address="0x" + "a" * 40,
                funder="0x" + "b" * 40,
                signature_type=2,
            )

        assert captured_args.get("signature_type") == 2
        assert captured_args.get("funder") == "0x" + "b" * 40

    def test_initialize_client_auto_approves_allowance_for_signature_type_2(self):
        """Smart proxy mode should trigger allowance verification after client init."""

        def fake_initialize(self):
            self.client = MagicMock()
            self.clob_client = self.client

        with (
            patch.object(PolymarketClient, "_initialize_client", fake_initialize),
            patch.object(PolymarketClient, "auto_approve_allowances", return_value=True) as approve,
        ):
            PolymarketClient(
                private_key="0" * 64,
                address="0x" + "a" * 40,
                signature_type=2,
            )

        approve.assert_called_once_with()

    def test_auto_approve_allowances_skips_tx_when_allowance_is_already_high(self):
        """auto_approve_allowances should not submit a tx when allowance is already sufficient."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                signature_type=2,
            )

        allowance_call = MagicMock()
        allowance_call.call.return_value = MIN_ALLOWANCE_THRESHOLD + (10**6)

        functions = MagicMock()
        functions.allowance.return_value = allowance_call

        usdc_contract = MagicMock()
        usdc_contract.functions = functions

        eth = MagicMock()
        eth.contract.return_value = usdc_contract

        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth = eth

        with patch("web3.Web3") as web3_cls:
            web3_cls.HTTPProvider.return_value = MagicMock()
            web3_cls.return_value = w3
            assert client.auto_approve_allowances() is True
        functions.approve.assert_not_called()
        eth.send_raw_transaction.assert_not_called()

    def test_auto_approve_allowances_skips_tx_at_threshold(self):
        """auto_approve_allowances should skip when allowance matches the threshold exactly."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                signature_type=2,
            )

        allowance_call = MagicMock()
        allowance_call.call.return_value = MIN_ALLOWANCE_THRESHOLD

        functions = MagicMock()
        functions.allowance.return_value = allowance_call

        usdc_contract = MagicMock()
        usdc_contract.functions = functions

        eth = MagicMock()
        eth.contract.return_value = usdc_contract

        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth = eth

        with patch("web3.Web3") as web3_cls:
            web3_cls.HTTPProvider.return_value = MagicMock()
            web3_cls.return_value = w3
            assert client.auto_approve_allowances() is True
        functions.approve.assert_not_called()
        eth.send_raw_transaction.assert_not_called()

    def test_auto_approve_allowances_submits_tx_when_allowance_is_low(self):
        """auto_approve_allowances should sign and send an approve tx when needed."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                signature_type=2,
            )

        allowance_call = MagicMock()
        allowance_call.call.return_value = 0

        approve_call = MagicMock()
        approve_call.build_transaction.return_value = {
            "from": client.address,
            "nonce": 7,
            "gasPrice": 123,
            "chainId": client.chain_id,
        }
        approve_call.estimate_gas.return_value = 45678

        functions = MagicMock()
        functions.allowance.return_value = allowance_call
        functions.approve.return_value = approve_call

        usdc_contract = MagicMock()
        usdc_contract.functions = functions

        signed_tx = MagicMock()
        signed_tx.raw_transaction = b"signed"

        tx_hash = MagicMock()
        tx_hash.hex.return_value = "0xabc"

        eth = MagicMock()
        eth.contract.return_value = usdc_contract
        eth.get_transaction_count.return_value = 7
        eth.gas_price = 123
        eth.account.sign_transaction.return_value = signed_tx
        eth.send_raw_transaction.return_value = tx_hash

        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth = eth

        with patch("web3.Web3") as web3_cls:
            web3_cls.HTTPProvider.return_value = MagicMock()
            web3_cls.return_value = w3
            assert client.auto_approve_allowances() is True
        functions.approve.assert_called_once()
        approve_call.build_transaction.assert_called_once()
        eth.account.sign_transaction.assert_called_once()
        eth.send_raw_transaction.assert_called_once_with(b"signed")
        eth.wait_for_transaction_receipt.assert_called_once_with(tx_hash)

    def test_auto_approve_allowances_uses_llamarpc_fallback_for_default_rpc(self):
        """Should replace polygon-rpc.com with resilient llamarpc fallback."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                signature_type=2,
            )

        allowance_call = MagicMock()
        allowance_call.call.return_value = MIN_ALLOWANCE_THRESHOLD

        functions = MagicMock()
        functions.allowance.return_value = allowance_call

        usdc_contract = MagicMock()
        usdc_contract.functions = functions

        eth = MagicMock()
        eth.contract.return_value = usdc_contract

        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth = eth

        with (
            patch.dict(os.environ, {"POLYGON_RPC_URL": "https://polygon-rpc.com"}, clear=False),
            patch("web3.Web3") as web3_cls,
        ):
            web3_cls.HTTPProvider.return_value = MagicMock()
            web3_cls.return_value = w3
            assert client.auto_approve_allowances() is True

        web3_cls.HTTPProvider.assert_called_once_with("https://polygon.llamarpc.com")

    def test_auto_approve_allowances_uses_config_rpc_before_env(self):
        """Config polygon_rpc_url must override POLYGON_RPC_URL environment variable."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                signature_type=2,
            )

        client.config = MagicMock()
        client.config.polygon_rpc_url = "https://config-rpc.example"

        allowance_call = MagicMock()
        allowance_call.call.return_value = MIN_ALLOWANCE_THRESHOLD

        functions = MagicMock()
        functions.allowance.return_value = allowance_call

        usdc_contract = MagicMock()
        usdc_contract.functions = functions

        eth = MagicMock()
        eth.contract.return_value = usdc_contract

        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth = eth

        with (
            patch.dict(os.environ, {"POLYGON_RPC_URL": "https://env-rpc.example"}, clear=False),
            patch("web3.Web3") as web3_cls,
        ):
            web3_cls.HTTPProvider.return_value = MagicMock()
            web3_cls.return_value = w3
            assert client.auto_approve_allowances() is True

        web3_cls.HTTPProvider.assert_called_once_with("https://config-rpc.example")

    def test_auto_approve_allowances_injects_poa_middleware_on_direct_web3(self):
        """Direct RPC Web3 client should receive POA middleware injection."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                signature_type=2,
            )

        allowance_call = MagicMock()
        allowance_call.call.return_value = MIN_ALLOWANCE_THRESHOLD

        functions = MagicMock()
        functions.allowance.return_value = allowance_call

        usdc_contract = MagicMock()
        usdc_contract.functions = functions

        eth = MagicMock()
        eth.contract.return_value = usdc_contract

        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth = eth
        w3.middleware_onion = MagicMock()

        with patch("web3.Web3") as web3_cls:
            web3_cls.HTTPProvider.return_value = MagicMock()
            web3_cls.return_value = w3
            assert client.auto_approve_allowances() is True

        w3.middleware_onion.inject.assert_called_once()

    def test_initialize_client_injects_poa_middleware_into_internal_clob_web3(self):
        """SDK-internal Web3 should receive middleware injection after ClobClient init."""

        def fake_clob_init(self_inner, **kwargs):
            self_inner.host = kwargs.get("host", "")
            self_inner.chain_id = kwargs.get("chain_id", 137)
            self_inner.client = MagicMock()
            self_inner.client.w3 = MagicMock()
            self_inner.client.w3.middleware_onion = MagicMock()

        with patch.object(ClobClient, "__init__", fake_clob_init):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "a" * 40,
                signature_type=3,
            )

        client.clob_client.client.w3.middleware_onion.inject.assert_called_once()

    def test_auto_approve_allowances_keeps_custom_rpc_url(self):
        """Should keep configured custom RPC URL when it is not polygon-rpc.com."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                signature_type=2,
            )

        allowance_call = MagicMock()
        allowance_call.call.return_value = MIN_ALLOWANCE_THRESHOLD

        functions = MagicMock()
        functions.allowance.return_value = allowance_call

        usdc_contract = MagicMock()
        usdc_contract.functions = functions

        eth = MagicMock()
        eth.contract.return_value = usdc_contract

        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth = eth

        custom_rpc = "https://my.custom-rpc.example"
        with (
            patch.dict(os.environ, {"POLYGON_RPC_URL": custom_rpc}, clear=False),
            patch("web3.Web3") as web3_cls,
        ):
            web3_cls.HTTPProvider.return_value = MagicMock()
            web3_cls.return_value = w3
            assert client.auto_approve_allowances() is True

        web3_cls.HTTPProvider.assert_called_once_with(custom_rpc)

    def test_auto_approve_allowances_does_not_fallback_on_substring_match(self):
        """Should not fallback when polygon-rpc.com appears outside hostname."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                signature_type=2,
            )

        allowance_call = MagicMock()
        allowance_call.call.return_value = MIN_ALLOWANCE_THRESHOLD

        functions = MagicMock()
        functions.allowance.return_value = allowance_call

        usdc_contract = MagicMock()
        usdc_contract.functions = functions

        eth = MagicMock()
        eth.contract.return_value = usdc_contract

        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth = eth

        custom_rpc = "https://rpc-proxy.example/polygon-rpc.com"
        with (
            patch.dict(os.environ, {"POLYGON_RPC_URL": custom_rpc}, clear=False),
            patch("web3.Web3") as web3_cls,
        ):
            web3_cls.HTTPProvider.return_value = MagicMock()
            web3_cls.return_value = w3
            assert client.auto_approve_allowances() is True

        web3_cls.HTTPProvider.assert_called_once_with(custom_rpc)

    def test_initialize_client_signature_type_0_forces_eoa_funder(self):
        """EOA direct mode must use signer EOA as funder."""
        captured_args = {}

        def fake_clob_init(self_inner, **kwargs):
            captured_args.update(kwargs)
            self_inner.host = kwargs.get("host", "")
            self_inner.chain_id = kwargs.get("chain_id", 137)
            self_inner.signer = None
            self_inner.creds = None
            self_inner.mode = 0
            self_inner.builder = MagicMock()
            self_inner.use_server_time = False
            self_inner.retry_on_error = False
            self_inner.builder_config = None
            self_inner.fee_slippage = 0
            self_inner._ClobClient__tick_sizes = {}
            self_inner._ClobClient__neg_risk = {}
            self_inner._ClobClient__fee_rates = {}

        with patch.object(ClobClient, "__init__", fake_clob_init):
            PolymarketClient(
                private_key="0" * 64,
                address="0x" + "a" * 40,
                funder="0x" + "b" * 40,
                signature_type=0,
            )

        assert captured_args.get("signature_type") == 0
        assert captured_args.get("funder") == "0x" + "a" * 40

    @pytest.mark.asyncio
    async def test_get_balance_returns_nonzero_with_poly_proxy_type(self):
        """balance-allowance response is forwarded correctly when signature type is correct."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "1" * 40,
                api_key="key",
                api_secret="secret",
                passphrase="passphrase",
            )

        class FakeClob:
            def get_balance_allowance(self, params):
                assert params.asset_type == AssetType.COLLATERAL
                return {"balance": "42.00", "allowance": "999999999"}

        client.client = FakeClob()
        balance = await client.get_balance()
        assert float(balance["balance"]) == pytest.approx(42.0), (
            f"Expected 42.0 USDC but got '{balance['balance']}'; "
            "POLY_PROXY balance-allowance response not being parsed correctly"
        )


class TestPortfolioBalanceHandling:
    """Regression tests for portfolio cash-balance extraction from CLOB payloads."""

    @staticmethod
    async def _run_portfolio_value(balance_payload):
        polymarket_client = MagicMock()
        polymarket_client.get_balance = AsyncMock(return_value=balance_payload)
        polymarket_client.get_orders = AsyncMock(return_value=[])

        rate_limiter = MagicMock()
        rate_limiter.acquire = AsyncMock(return_value=0.0)

        config = MagicMock()
        config.POLYGON_ADDRESS = "0x" + "1" * 40

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = []

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)

        async_client_cm = AsyncMock()
        async_client_cm.__aenter__.return_value = mock_http_client
        async_client_cm.__aexit__.return_value = None

        with patch(
            "polymarket_mcp.tools.portfolio.httpx.AsyncClient", return_value=async_client_cm
        ):
            with patch(
                "polymarket_mcp.tools.portfolio._get_usd_to_eur_rate",
                new=AsyncMock(return_value=(1.0, True)),
            ):
                result = await get_portfolio_value(
                    polymarket_client=polymarket_client,
                    rate_limiter=rate_limiter,
                    config=config,
                    include_breakdown=True,
                )

        return result[0].text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("balance_payload", "expected_cash_line"),
        [
            ({"available": "0.93", "balance": "0"}, "Cash Balance (USDC): $0.93"),
            ({"available_balance": "1.10", "balance": "0"}, "Cash Balance (USDC): $1.10"),
            ({"balance": "2.50"}, "Cash Balance (USDC): $2.50"),
            ({"balance": "7488975"}, "Cash Balance (USDC): $7.49"),
            ({}, "Cash Balance (USDC): $0.00"),
        ],
    )
    async def test_get_portfolio_value_cash_balance_field_precedence(
        self, balance_payload, expected_cash_line
    ):
        """Portfolio value should follow available -> available_balance -> balance -> 0 fallback."""
        output = await self._run_portfolio_value(balance_payload)
        assert expected_cash_line in output

    def test_extract_cash_balance_scales_raw_usdc_atomic_units_exactly(self):
        assert _extract_cash_balance({"balance": "7488975"}) == pytest.approx(7.488975)

    def test_scale_usdc_atomic_units_handles_scaled_and_raw_values(self):
        assert scale_usdc_atomic_units(7488975, "7488975") == pytest.approx(7.488975)
        assert scale_usdc_atomic_units(7488975.0, "7488975.0") == pytest.approx(7.488975)
        assert scale_usdc_atomic_units(7.49, "7.49") == pytest.approx(7.49)

    @pytest.mark.asyncio
    async def test_get_portfolio_value_logs_permission_error_and_includes_eur(self, caplog):
        """Portfolio summary should log explicit position-permission failures and include EUR total."""
        polymarket_client = MagicMock()
        polymarket_client.get_balance = AsyncMock(return_value={"available": "7488975"})
        polymarket_client.get_orders = AsyncMock(return_value=[])
        polymarket_client.get_funder_address = MagicMock(return_value="0x" + "2" * 40)
        polymarket_client.get_user_positions = AsyncMock(
            side_effect=RuntimeError("READ-ONLY mode: L2 API credentials required")
        )
        polymarket_client.get_positions = AsyncMock(
            return_value=[
                {
                    "asset_id": "token_123",
                    "market": "market_123",
                    "outcome": "Yes",
                    "size": "2",
                    "average_price": "0.50",
                }
            ]
        )
        polymarket_client.get_orderbook = AsyncMock(
            return_value={"bids": [{"price": "0.50"}], "asks": [{"price": "0.60"}]}
        )

        rate_limiter = MagicMock()
        rate_limiter.acquire = AsyncMock(return_value=0.0)

        config = MagicMock()
        config.POLYGON_ADDRESS = "0x" + "1" * 40

        with patch(
            "polymarket_mcp.tools.portfolio._get_usd_to_eur_rate",
            new=AsyncMock(return_value=(0.90, False)),
        ):
            with caplog.at_level(logging.ERROR):
                result = await get_portfolio_value(
                    polymarket_client=polymarket_client,
                    rate_limiter=rate_limiter,
                    config=config,
                    include_breakdown=False,
                )

        output = result[0].text
        assert (
            "Failed to fetch positions via get_user_positions due to permissions/credentials"
            in caplog.text
        )
        assert "Cash Balance (USDC): $7.49" in output
        assert "TOTAL PORTFOLIO VALUE (EUR): €7.73" in output

    @pytest.mark.asyncio
    async def test_get_portfolio_value_uses_funder_wallet_for_positions(self):
        """Portfolio positions must be queried for the Polymarket UI funder/deposit wallet."""
        polymarket_client = MagicMock()
        polymarket_client.get_balance = AsyncMock(return_value={"balance": "0"})
        polymarket_client.get_orders = AsyncMock(return_value=[])
        polymarket_client.get_funder_address = MagicMock(return_value="0x" + "2" * 40)

        rate_limiter = MagicMock()
        rate_limiter.acquire = AsyncMock(return_value=0.0)

        config = MagicMock()
        config.POLYGON_ADDRESS = "0x" + "1" * 40

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = []

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_response)

        async_client_cm = AsyncMock()
        async_client_cm.__aenter__.return_value = mock_http_client
        async_client_cm.__aexit__.return_value = None

        with patch(
            "polymarket_mcp.tools.portfolio.httpx.AsyncClient", return_value=async_client_cm
        ):
            with patch(
                "polymarket_mcp.tools.portfolio._get_usd_to_eur_rate",
                new=AsyncMock(return_value=(1.0, True)),
            ):
                await get_portfolio_value(
                    polymarket_client=polymarket_client,
                    rate_limiter=rate_limiter,
                    config=config,
                    include_breakdown=True,
                )

        assert mock_http_client.get.await_args.kwargs["params"]["user"] == "0x" + "2" * 40


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
    async def test_get_server_status_tool_returns_environment_and_balance(self):
        import polymarket_mcp.server as server_module

        saved = {
            "config": server_module.config,
            "polymarket_client": server_module.polymarket_client,
        }

        fake_config = MagicMock()
        fake_config.POLYMARKET_ENV = "testnet"
        fake_config.POLYMARKET_CHAIN_ID = 80002
        fake_config.CLOB_API_URL = "https://clob-testnet.polytest.cloud"
        fake_config.GAMMA_API_URL = "https://gcomm-api.polytest.cloud"
        fake_config.effective_funder = "0x" + "2" * 40

        fake_client = MagicMock()
        fake_client.get_balance = AsyncMock(return_value={"available": "1500"})
        fake_client.has_api_credentials.return_value = True
        fake_client.has_verified_api_credentials.return_value = True

        try:
            server_module.config = fake_config
            server_module.polymarket_client = fake_client

            result = await server_module.call_tool("get_server_status", {})
            payload = json.loads(result[0].text)

            assert payload["status"] == "connected"
            assert payload["environment"] == "testnet"
            assert payload["network"] == "Polygon Amoy"
            assert payload["wallet_address"] == fake_config.effective_funder
            assert payload["available_balance_usdc"] == "1500.00"
            assert payload["endpoints"]["clob_api"] == fake_config.CLOB_API_URL
            assert payload["endpoints"]["gamma_api"] == fake_config.GAMMA_API_URL
        finally:
            server_module.config = saved["config"]
            server_module.polymarket_client = saved["polymarket_client"]

    @pytest.mark.asyncio
    async def test_get_server_status_scales_raw_usdc_atomic_balance(self):
        import polymarket_mcp.server as server_module

        saved = {
            "config": server_module.config,
            "polymarket_client": server_module.polymarket_client,
        }

        fake_config = MagicMock()
        fake_config.POLYMARKET_ENV = "testnet"
        fake_config.POLYMARKET_CHAIN_ID = 80002
        fake_config.CLOB_API_URL = "https://clob-testnet.polytest.cloud"
        fake_config.GAMMA_API_URL = "https://gcomm-api.polytest.cloud"
        fake_config.effective_funder = "0x" + "2" * 40

        fake_client = MagicMock()
        fake_client.get_balance = AsyncMock(return_value={"available": "7488975"})
        fake_client.has_api_credentials.return_value = True
        fake_client.has_verified_api_credentials.return_value = True

        try:
            server_module.config = fake_config
            server_module.polymarket_client = fake_client

            result = await server_module.call_tool("get_server_status", {})
            payload = json.loads(result[0].text)

            assert payload["available_balance_usdc"] == "7.49"
        finally:
            server_module.config = saved["config"]
            server_module.polymarket_client = saved["polymarket_client"]

    @pytest.mark.asyncio
    async def test_initialize_server_registers_and_starts_websocket_manager(self):
        """Server init should register realtime manager and start the background loop."""
        import polymarket_mcp.server as server_module

        call_order = []

        fake_config = MagicMock()
        fake_config.POLYGON_PRIVATE_KEY = "0" * 64
        fake_config.POLYGON_ADDRESS = "0x" + "0" * 40
        fake_config.POLYMARKET_CHAIN_ID = 137
        fake_config.POLYMARKET_ENV = "mainnet"
        fake_config.POLYMARKET_API_KEY = None
        fake_config.POLYMARKET_API_SECRET = None
        fake_config.POLYMARKET_PASSPHRASE = None
        fake_config.CLOB_API_URL = "https://clob.polymarket.com"
        fake_config.GAMMA_API_URL = "https://gamma-api.polymarket.com"
        fake_config.polymarket_ready = True
        fake_config.polymarket_config_error = None
        fake_config.WS_ENABLED = True
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
    async def test_config_and_websocket_keep_secret_passphrase_separate_when_secret_missing(self):
        """Config should reject incomplete triplet and websocket payload must not copy passphrase into secret."""
        config = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "0" * 40,
            POLYMARKET_API_KEY="test-key",
            POLYMARKET_API_SECRET=None,
            POLYMARKET_PASSPHRASE="legacy-secret",
        )
        assert config.has_api_credentials() is False
        assert config.effective_api_secret is None
        manager = WebSocketManager(config=config)
        manager.clob_ws = AsyncMock()
        manager.clob_ws.recv = AsyncMock(return_value='{"type":"authenticated"}')

        await manager._authenticate_clob()

        payload = manager.clob_ws.send.await_args.args[0]
        data = json.loads(payload)
        assert data["auth"]["secret"] is None
        assert data["auth"]["passphrase"] == "legacy-secret"

    def test_config_prefers_relayer_triplet_over_legacy_api_triplet(self):
        """Relayer credentials from UI should be the primary L2 auth source when set."""
        config = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "0" * 40,
            POLYMARKET_API_KEY="legacy-api-key",
            POLYMARKET_API_SECRET="legacy-api-secret",
            POLYMARKET_PASSPHRASE="legacy-api-passphrase",
            POLYMARKET_RELAYER_KEY="relayer-key",
            POLYMARKET_RELAYER_SECRET="relayer-secret",
            POLYMARKET_RELAYER_PASSPHRASE="relayer-passphrase",
        )

        assert config.effective_api_key == "relayer-key"
        assert config.effective_api_secret == "relayer-secret"
        assert config.effective_api_passphrase == "relayer-passphrase"
        assert config.has_api_credentials() is True
        assert config.l2_auth_mode == "static"

    def test_config_allows_startup_with_only_private_key(self):
        """Startup must succeed with only POLYGON_PRIVATE_KEY set.

        L2 trading credentials are auto-derived from the private key at runtime
        via create_or_derive_api_key() (official Polymarket Python SDK flow).
        POLYMARKET_RELAYER_KEY is NOT required for this path.
        """
        config = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "0" * 40,
            POLYMARKET_CHAIN_ID=137,
            CLOB_API_URL="https://clob.polymarket.com",
            GAMMA_API_URL="https://gamma-api.polymarket.com",
            POLYMARKET_ENV="mainnet",
        )
        assert config.l2_auth_mode == "auto_derive"
        assert config.effective_api_key is None
        assert config.polymarket_ready is True

    def test_config_uses_derived_mode_when_key_exists_but_secret_passphrase_missing(self):
        """When key exists but secret/passphrase are missing, mode should be derived."""
        config = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "0" * 40,
            POLYMARKET_CHAIN_ID=137,
            CLOB_API_URL="https://clob.polymarket.com",
            GAMMA_API_URL="https://gamma-api.polymarket.com",
            POLYMARKET_ENV="mainnet",
            POLYMARKET_RELAYER_KEY="relayer-key",
            POLYMARKET_RELAYER_SECRET=None,
            POLYMARKET_RELAYER_PASSPHRASE=None,
            POLYMARKET_API_SECRET="legacy-secret",
            POLYMARKET_PASSPHRASE="legacy-passphrase",
        )

        assert config.effective_api_key == "relayer-key"
        assert config.effective_api_secret is None
        assert config.effective_api_passphrase is None
        assert config.l2_auth_mode == "derived"

    def test_config_requires_all_three_l2_credential_fields(self):
        """L2 auth should require key, secret, and passphrase."""
        base_kwargs = {
            "POLYGON_PRIVATE_KEY": "0" * 64,
            "POLYGON_ADDRESS": "0x" + "0" * 40,
        }

        missing_key = PolymarketConfig(
            **base_kwargs,
            POLYMARKET_RELAYER_SECRET="s",
            POLYMARKET_RELAYER_PASSPHRASE="p",
        )
        missing_secret = PolymarketConfig(
            **base_kwargs,
            POLYMARKET_RELAYER_KEY="k",
            POLYMARKET_RELAYER_PASSPHRASE="p",
        )
        missing_passphrase = PolymarketConfig(
            **base_kwargs,
            POLYMARKET_RELAYER_KEY="k",
            POLYMARKET_RELAYER_SECRET="s",
        )

        assert missing_key.has_api_credentials() is False
        assert missing_secret.has_api_credentials() is False
        assert missing_passphrase.has_api_credentials() is False

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
    async def test_closing_soon_handles_iso_z_dates_without_timezone_warning(self, caplog):
        """Closing-soon filtering should compare timezone-aware datetimes safely."""
        from polymarket_mcp.tools import market_discovery

        with patch.object(
            market_discovery, "_fetch_gamma_markets", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = [
                {
                    "question": "soon market",
                    "end_date_iso": (datetime.utcnow() - timedelta(days=365)).isoformat() + "Z",
                }
            ]
            results = await market_discovery.get_closing_soon_markets(hours=24, limit=5)

        assert len(results) == 1
        assert "offset-naive and offset-aware datetimes" not in caplog.text

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
            tool_names = [tool.name for tool in tools]
            assert "get_server_status" in tool_names
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

    def test_get_yes_token_id_with_unknown_label_uses_first_token_fallback(self):
        """Unknown labels should fall back to deterministic first-token behavior."""
        tt = self._make_trading_tools()
        tokens = [
            {"token_id": "no_tok", "outcome": "No"},
            {"token_id": "maybe_tok", "outcome": "Maybe"},
        ]
        assert tt._get_yes_token_id(tokens) == "no_tok"

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
        assert (
            selected == "yes_token_xyz"
        ), "Should select YES token even when NO is listed first in the tokens array"


# ---------------------------------------------------------------------------
# Startup credential validation — ensure_valid_api_credentials()
# ---------------------------------------------------------------------------


class TestEnsureValidApiCredentials:
    """ensure_valid_api_credentials() must verify/refresh/create creds at startup."""

    _DUMMY_PRIVATE_KEY = "0x" + "a" * 64
    _DUMMY_ADDRESS = "0x" + "0" * 40

    def test_client_does_not_reuse_passphrase_as_secret(self):
        """Client bootstrap must not cross-fallback passphrase into secret."""
        with patch("polymarket_mcp.auth.client.ClobClient"):
            client = PolymarketClient(
                private_key=self._DUMMY_PRIVATE_KEY,
                address=self._DUMMY_ADDRESS,
                signature_type=2,
                api_key="key-only",
                api_secret=None,
                passphrase="pass-only",
            )

        assert client.api_creds is None
        assert client._allow_credential_derivation is True

    def _make_client(self, with_creds: bool = True, key_only: bool = False):
        """Return a PolymarketClient whose ClobClient is fully mocked."""
        with patch("polymarket_mcp.auth.client.ClobClient"):
            api_key = "key-test" if with_creds or key_only else None
            client = PolymarketClient(
                private_key=self._DUMMY_PRIVATE_KEY,
                address=self._DUMMY_ADDRESS,
                signature_type=2,
                api_key=api_key,
                api_secret="secret-test" if with_creds else None,
                passphrase="pass-test" if with_creds else None,
            )
        return client

    def test_signature_type_3_uses_configured_api_credentials(self):
        with patch("polymarket_mcp.auth.client.ClobClient") as mock_clob:
            client = PolymarketClient(
                private_key=self._DUMMY_PRIVATE_KEY,
                address=self._DUMMY_ADDRESS,
                signature_type=3,
                api_key="legacy-key",
                api_secret="legacy-secret",
                passphrase="legacy-pass",
            )

        assert client.api_creds is not None
        assert client.api_creds.api_key == "legacy-key"
        assert client._allow_credential_derivation is False
        assert mock_clob.call_args.kwargs["creds"].api_key == "legacy-key"

    @pytest.mark.asyncio
    async def test_derived_mode_bootstrap_calls_create_api_credentials(self):
        """When only relayer key is configured, secret/passphrase must be derived."""
        client = self._make_client(with_creds=False, key_only=True)
        assert not client.has_api_credentials()

        with patch.object(client, "create_api_credentials", new_callable=AsyncMock) as mock_create:
            await client.ensure_valid_api_credentials()

        mock_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_valid_creds_probes_balance_and_passes(self):
        """When creds are valid, _fetch_balance_once is called and no refresh occurs."""
        client = self._make_client(with_creds=True)

        with (
            patch.object(
                client, "_fetch_balance_once", return_value={"balance": "10.0"}
            ) as mock_probe,
            patch.object(client, "_refresh_api_credentials") as mock_refresh,
        ):
            await client.ensure_valid_api_credentials()

        mock_probe.assert_called_once()
        mock_refresh.assert_not_called()
        assert client.has_verified_api_credentials()

    @pytest.mark.asyncio
    async def test_configured_creds_from_different_wallet_are_reconciled_before_probe(self):
        """Startup must not derive/replace creds when static relayer triplet is provided."""
        client = self._make_client(with_creds=True)
        client.client.create_or_derive_api_key = MagicMock()

        with patch.object(client, "_fetch_balance_once", return_value={"balance": "10.0"}):
            await client.ensure_valid_api_credentials()

        client.client.create_or_derive_api_key.assert_not_called()
        assert client._api_creds_from_config is True
        assert client.has_verified_api_credentials()

    @pytest.mark.asyncio
    async def test_stale_creds_401_triggers_refresh(self):
        """On HTTP 401 with static credentials, startup must not auto-derive new keys."""
        from py_clob_client_v2.exceptions import PolyApiException

        client = self._make_client(with_creds=True)

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"error": "Unauthorized"}
        exc_401 = PolyApiException(resp=mock_resp)

        with (
            patch.object(client, "_fetch_balance_once", side_effect=[exc_401, {"balance": "10.0"}]),
            patch.object(client, "_refresh_api_credentials") as mock_refresh,
        ):
            await client.ensure_valid_api_credentials()

        mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_creds_401_triggers_refresh_for_derived_mode(self):
        """On HTTP 401, derived-mode credentials should auto-refresh."""
        from py_clob_client_v2.exceptions import PolyApiException

        client = self._make_client(with_creds=True)
        client._allow_credential_derivation = True

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"error": "Unauthorized"}
        exc_401 = PolyApiException(resp=mock_resp)

        with (
            patch.object(client, "_fetch_balance_once", side_effect=[exc_401, {"balance": "10.0"}]),
            patch.object(client, "_refresh_api_credentials") as mock_refresh,
        ):
            await client.ensure_valid_api_credentials()

        mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_401_api_error_does_not_block_startup(self):
        """Non-auth errors (e.g. 503) must log a warning but not raise."""
        from py_clob_client_v2.exceptions import PolyApiException

        client = self._make_client(with_creds=True)

        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.json.return_value = {"error": "Service Unavailable"}
        exc_503 = PolyApiException(resp=mock_resp)

        with (
            patch.object(client, "_fetch_balance_once", side_effect=exc_503),
            patch.object(client, "_refresh_api_credentials") as mock_refresh,
        ):
            # Must not raise
            await client.ensure_valid_api_credentials()

        mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_network_error_does_not_block_startup(self):
        """Generic network errors must log a warning and allow startup to continue."""
        client = self._make_client(with_creds=True)

        with (
            patch.object(client, "_fetch_balance_once", side_effect=ConnectionError("timeout")),
            patch.object(client, "_refresh_api_credentials") as mock_refresh,
        ):
            await client.ensure_valid_api_credentials()

        mock_refresh.assert_not_called()
        assert not client.has_verified_api_credentials()

    @pytest.mark.asyncio
    async def test_initialize_server_calls_ensure_valid(self):
        """initialize_server() must call ensure_valid_api_credentials() on startup."""
        import polymarket_mcp.server as server_module

        saved = {
            "config": server_module.config,
            "polymarket_client": server_module.polymarket_client,
            "safety_limits": server_module.safety_limits,
            "rate_limiter": server_module.rate_limiter,
            "trading_tools": server_module.trading_tools,
            "websocket_manager": server_module.websocket_manager,
        }

        mock_client = MagicMock(spec=PolymarketClient)
        mock_client.has_api_credentials.return_value = True
        mock_client.ensure_valid_api_credentials = AsyncMock()
        mock_client.get_address.return_value = "0x" + "0" * 40
        mock_client.get_chain_id.return_value = 137

        mock_config = MagicMock()
        mock_config.POLYGON_PRIVATE_KEY = "0x" + "a" * 64
        mock_config.POLYGON_ADDRESS = "0x" + "0" * 40
        mock_config.POLYMARKET_CHAIN_ID = 137
        mock_config.POLYMARKET_API_KEY = "k"
        mock_config.POLYMARKET_API_SECRET = "s"
        mock_config.POLYMARKET_PASSPHRASE = "p"
        mock_config.POLYMARKET_ENV = "mainnet"
        mock_config.CLOB_API_URL = "https://clob.polymarket.com"
        mock_config.GAMMA_API_URL = "https://gamma-api.polymarket.com"
        mock_config.POLYGON_RPC_URL = "https://polygon-rpc.example.com"
        mock_config.POLYMARKET_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
        mock_config.effective_funder = "0x" + "0" * 40
        mock_config.POLYMARKET_FUNDER = mock_config.effective_funder
        # Non-type-3 flow should still perform startup credential verification.
        mock_config.POLYMARKET_SIGNATURE_TYPE = 2
        mock_config.WS_ENABLED = False
        mock_config.LOG_LEVEL = "INFO"
        mock_config.DEMO_MODE = False
        mock_config.polymarket_ready = True
        mock_config.polymarket_config_error = None

        try:
            with (
                patch("polymarket_mcp.server.load_config", return_value=mock_config),
                patch(
                    "polymarket_mcp.server.create_polymarket_client", return_value=mock_client
                ) as mock_create,
                patch("polymarket_mcp.server.create_safety_limits_from_config"),
                patch("polymarket_mcp.server.get_rate_limiter"),
                patch("polymarket_mcp.server.TradingTools"),
            ):
                await server_module.initialize_server()

            mock_client.ensure_valid_api_credentials.assert_awaited_once()
            assert mock_create.call_args.kwargs["host"] == mock_config.CLOB_API_URL
        finally:
            for key, val in saved.items():
                setattr(server_module, key, val)

    @pytest.mark.asyncio
    async def test_initialize_server_calls_ensure_valid_for_signature_type_3(self):
        import polymarket_mcp.server as server_module

        saved = {
            "config": server_module.config,
            "polymarket_client": server_module.polymarket_client,
            "safety_limits": server_module.safety_limits,
            "rate_limiter": server_module.rate_limiter,
            "trading_tools": server_module.trading_tools,
            "websocket_manager": server_module.websocket_manager,
        }

        mock_client = MagicMock(spec=PolymarketClient)
        mock_client.has_api_credentials.return_value = True
        mock_client.has_verified_api_credentials.return_value = True
        mock_client.ensure_valid_api_credentials = AsyncMock()

        mock_config = MagicMock()
        mock_config.POLYGON_PRIVATE_KEY = "0x" + "a" * 64
        mock_config.POLYGON_ADDRESS = "0x" + "0" * 40
        mock_config.POLYMARKET_CHAIN_ID = 137
        mock_config.POLYMARKET_ENV = "mainnet"
        mock_config.CLOB_API_URL = "https://clob.polymarket.com"
        mock_config.GAMMA_API_URL = "https://gamma-api.polymarket.com"
        mock_config.POLYGON_RPC_URL = "https://polygon-rpc.example.com"
        mock_config.POLYMARKET_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
        mock_config.effective_funder = "0x" + "0" * 40
        mock_config.POLYMARKET_FUNDER = mock_config.effective_funder
        mock_config.POLYMARKET_SIGNATURE_TYPE = 3
        mock_config.WS_ENABLED = False
        mock_config.LOG_LEVEL = "INFO"
        mock_config.DEMO_MODE = False

        try:
            with (
                patch("polymarket_mcp.server.load_config", return_value=mock_config),
                patch(
                    "polymarket_mcp.server.get_polymarket_runtime_state", return_value=(True, None)
                ),
                patch(
                    "polymarket_mcp.server._check_geoblock_status", new_callable=AsyncMock
                ) as mock_geoblock,
                patch(
                    "polymarket_mcp.server.create_polymarket_client",
                    return_value=mock_client,
                ),
                patch("polymarket_mcp.server.create_safety_limits_from_config"),
                patch("polymarket_mcp.server.get_rate_limiter"),
                patch("polymarket_mcp.server.TradingTools"),
            ):
                mock_geoblock.return_value = False
                await server_module.initialize_server()

            mock_client.ensure_valid_api_credentials.assert_awaited_once()
        finally:
            for key, val in saved.items():
                setattr(server_module, key, val)

    @pytest.mark.asyncio
    async def test_initialize_server_aborts_when_geoblocked(self):
        """initialize_server() must fail fast when startup geoblock check returns blocked."""
        import polymarket_mcp.server as server_module

        saved = {
            "config": server_module.config,
            "polymarket_client": server_module.polymarket_client,
            "safety_limits": server_module.safety_limits,
            "rate_limiter": server_module.rate_limiter,
            "trading_tools": server_module.trading_tools,
            "websocket_manager": server_module.websocket_manager,
        }

        mock_config = MagicMock()
        mock_config.POLYGON_PRIVATE_KEY = "0x" + "a" * 64
        mock_config.POLYGON_ADDRESS = "0x" + "0" * 40
        mock_config.POLYMARKET_CHAIN_ID = 137
        mock_config.POLYMARKET_API_KEY = "k"
        mock_config.POLYMARKET_API_SECRET = "s"
        mock_config.POLYMARKET_PASSPHRASE = "p"
        mock_config.POLYMARKET_ENV = "mainnet"
        mock_config.CLOB_API_URL = "https://clob.polymarket.com"
        mock_config.GAMMA_API_URL = "https://gamma-api.polymarket.com"
        mock_config.effective_funder = "0x" + "0" * 40
        mock_config.POLYMARKET_SIGNATURE_TYPE = 3
        mock_config.WS_ENABLED = False
        mock_config.LOG_LEVEL = "INFO"
        mock_config.DEMO_MODE = False

        try:
            with (
                patch("polymarket_mcp.server.load_config", return_value=mock_config),
                patch(
                    "polymarket_mcp.server.get_polymarket_runtime_state", return_value=(True, None)
                ),
                patch(
                    "polymarket_mcp.server._check_geoblock_status", new_callable=AsyncMock
                ) as mock_geoblock,
                patch("polymarket_mcp.server.create_polymarket_client") as mock_create,
            ):
                mock_geoblock.return_value = True
                with pytest.raises(RuntimeError, match="geoblocked"):
                    await server_module.initialize_server()

            mock_create.assert_not_called()
            mock_geoblock.assert_awaited_once_with(mock_config.POLYMARKET_GEOBLOCK_URL)
        finally:
            for key, val in saved.items():
                setattr(server_module, key, val)

    @pytest.mark.asyncio
    async def test_initialize_server_runs_geoblock_check(self):
        """initialize_server() should call startup geoblock check with CLOB host."""
        import polymarket_mcp.server as server_module

        saved = {
            "config": server_module.config,
            "polymarket_client": server_module.polymarket_client,
            "safety_limits": server_module.safety_limits,
            "rate_limiter": server_module.rate_limiter,
            "trading_tools": server_module.trading_tools,
            "websocket_manager": server_module.websocket_manager,
        }

        mock_client = MagicMock(spec=PolymarketClient)
        mock_client.has_api_credentials.return_value = True
        mock_client.ensure_valid_api_credentials = AsyncMock()

        mock_config = MagicMock()
        mock_config.POLYGON_PRIVATE_KEY = "0x" + "a" * 64
        mock_config.POLYGON_ADDRESS = "0x" + "0" * 40
        mock_config.POLYMARKET_CHAIN_ID = 137
        mock_config.POLYMARKET_API_KEY = "k"
        mock_config.POLYMARKET_API_SECRET = "s"
        mock_config.POLYMARKET_PASSPHRASE = "p"
        mock_config.POLYMARKET_ENV = "mainnet"
        mock_config.CLOB_API_URL = "https://clob.polymarket.com"
        mock_config.GAMMA_API_URL = "https://gamma-api.polymarket.com"
        mock_config.POLYMARKET_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
        mock_config.effective_funder = "0x" + "0" * 40
        mock_config.POLYMARKET_SIGNATURE_TYPE = 3
        mock_config.WS_ENABLED = False
        mock_config.LOG_LEVEL = "INFO"
        mock_config.DEMO_MODE = False

        try:
            with (
                patch("polymarket_mcp.server.load_config", return_value=mock_config),
                patch(
                    "polymarket_mcp.server.get_polymarket_runtime_state", return_value=(True, None)
                ),
                patch(
                    "polymarket_mcp.server._check_geoblock_status", new_callable=AsyncMock
                ) as mock_geoblock,
                patch(
                    "polymarket_mcp.server.create_polymarket_client", return_value=mock_client
                ) as mock_create,
                patch("polymarket_mcp.server.create_safety_limits_from_config"),
                patch("polymarket_mcp.server.get_rate_limiter"),
                patch("polymarket_mcp.server.TradingTools"),
            ):
                mock_geoblock.return_value = False
                await server_module.initialize_server()

            mock_geoblock.assert_awaited_once_with(mock_config.POLYMARKET_GEOBLOCK_URL)
            mock_create.assert_called_once()
        finally:
            for key, val in saved.items():
                setattr(server_module, key, val)

    @pytest.mark.asyncio
    async def test_initialize_server_continues_when_geoblock_inconclusive(self):
        """initialize_server() should continue when geoblock check returns None."""
        import polymarket_mcp.server as server_module

        saved = {
            "config": server_module.config,
            "polymarket_client": server_module.polymarket_client,
            "safety_limits": server_module.safety_limits,
            "rate_limiter": server_module.rate_limiter,
            "trading_tools": server_module.trading_tools,
            "websocket_manager": server_module.websocket_manager,
        }

        mock_client = MagicMock(spec=PolymarketClient)
        mock_client.has_api_credentials.return_value = True
        mock_client.ensure_valid_api_credentials = AsyncMock()

        mock_config = MagicMock()
        mock_config.POLYGON_PRIVATE_KEY = "0x" + "a" * 64
        mock_config.POLYGON_ADDRESS = "0x" + "0" * 40
        mock_config.POLYMARKET_CHAIN_ID = 137
        mock_config.POLYMARKET_API_KEY = "k"
        mock_config.POLYMARKET_API_SECRET = "s"
        mock_config.POLYMARKET_PASSPHRASE = "p"
        mock_config.POLYMARKET_ENV = "mainnet"
        mock_config.CLOB_API_URL = "https://clob.polymarket.com"
        mock_config.GAMMA_API_URL = "https://gamma-api.polymarket.com"
        mock_config.POLYMARKET_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
        mock_config.effective_funder = "0x" + "0" * 40
        mock_config.POLYMARKET_SIGNATURE_TYPE = 3
        mock_config.WS_ENABLED = False
        mock_config.LOG_LEVEL = "INFO"
        mock_config.DEMO_MODE = False

        try:
            with (
                patch("polymarket_mcp.server.load_config", return_value=mock_config),
                patch(
                    "polymarket_mcp.server.get_polymarket_runtime_state", return_value=(True, None)
                ),
                patch(
                    "polymarket_mcp.server._check_geoblock_status", new_callable=AsyncMock
                ) as mock_geoblock,
                patch(
                    "polymarket_mcp.server.create_polymarket_client", return_value=mock_client
                ) as mock_create,
                patch("polymarket_mcp.server.create_safety_limits_from_config"),
                patch("polymarket_mcp.server.get_rate_limiter"),
                patch("polymarket_mcp.server.TradingTools"),
            ):
                mock_geoblock.return_value = None
                await server_module.initialize_server()

            mock_geoblock.assert_awaited_once_with(mock_config.POLYMARKET_GEOBLOCK_URL)
            mock_create.assert_called_once()
        finally:
            for key, val in saved.items():
                setattr(server_module, key, val)

    @pytest.mark.asyncio
    async def test_initialize_server_skips_geoblock_check_when_url_missing(self):
        """initialize_server() should skip geoblock check when URL is not configured."""
        import polymarket_mcp.server as server_module

        saved = {
            "config": server_module.config,
            "polymarket_client": server_module.polymarket_client,
            "safety_limits": server_module.safety_limits,
            "rate_limiter": server_module.rate_limiter,
            "trading_tools": server_module.trading_tools,
            "websocket_manager": server_module.websocket_manager,
        }

        mock_client = MagicMock(spec=PolymarketClient)
        mock_client.has_api_credentials.return_value = True
        mock_client.ensure_valid_api_credentials = AsyncMock()

        mock_config = MagicMock()
        mock_config.POLYGON_PRIVATE_KEY = "0x" + "a" * 64
        mock_config.POLYGON_ADDRESS = "0x" + "0" * 40
        mock_config.POLYMARKET_CHAIN_ID = 137
        mock_config.POLYMARKET_API_KEY = "k"
        mock_config.POLYMARKET_API_SECRET = "s"
        mock_config.POLYMARKET_PASSPHRASE = "p"
        mock_config.POLYMARKET_ENV = "mainnet"
        mock_config.CLOB_API_URL = "https://clob.polymarket.com"
        mock_config.GAMMA_API_URL = "https://gamma-api.polymarket.com"
        mock_config.POLYMARKET_GEOBLOCK_URL = None
        mock_config.effective_funder = "0x" + "0" * 40
        mock_config.POLYMARKET_SIGNATURE_TYPE = 3
        mock_config.WS_ENABLED = False
        mock_config.LOG_LEVEL = "INFO"
        mock_config.DEMO_MODE = False

        try:
            with (
                patch("polymarket_mcp.server.load_config", return_value=mock_config),
                patch(
                    "polymarket_mcp.server.get_polymarket_runtime_state", return_value=(True, None)
                ),
                patch(
                    "polymarket_mcp.server._check_geoblock_status", new_callable=AsyncMock
                ) as mock_geoblock,
                patch(
                    "polymarket_mcp.server.create_polymarket_client", return_value=mock_client
                ) as mock_create,
                patch("polymarket_mcp.server.create_safety_limits_from_config"),
                patch("polymarket_mcp.server.get_rate_limiter"),
                patch("polymarket_mcp.server.TradingTools"),
                patch("polymarket_mcp.server.logger") as mock_logger,
            ):
                await server_module.initialize_server()

            mock_geoblock.assert_not_awaited()
            mock_create.assert_called_once()
            mock_logger.info.assert_any_call(
                "POLYMARKET_GEOBLOCK_URL not set; skipping startup geoblock check"
            )
        finally:
            for key, val in saved.items():
                setattr(server_module, key, val)

    @pytest.mark.asyncio
    async def test_initialize_server_demo_mode_skips_auth_bootstrap(self):
        """initialize_server() must skip wallet/auth bootstrap in DEMO mode."""
        import polymarket_mcp.server as server_module

        saved = {
            "config": server_module.config,
            "polymarket_client": server_module.polymarket_client,
            "safety_limits": server_module.safety_limits,
            "rate_limiter": server_module.rate_limiter,
            "trading_tools": server_module.trading_tools,
            "websocket_manager": server_module.websocket_manager,
        }

        demo_config = PolymarketConfig(DEMO_MODE=True, WS_ENABLED=False)

        try:
            with (
                patch("polymarket_mcp.server.load_config", return_value=demo_config),
                patch("polymarket_mcp.server.create_polymarket_client") as mock_create,
                patch("polymarket_mcp.server.create_safety_limits_from_config"),
                patch("polymarket_mcp.server.get_rate_limiter"),
            ):
                await server_module.initialize_server()

            mock_create.assert_not_called()
            assert server_module.polymarket_client is None
            assert server_module.trading_tools is None
        finally:
            for key, val in saved.items():
                setattr(server_module, key, val)


class TestVerifiedCredentialGating:
    """Trading tools should require verified (not just present) API credentials."""

    @pytest.mark.asyncio
    async def test_list_tools_disables_trading_when_creds_not_verified(self):
        import polymarket_mcp.server as server_module

        saved = {
            "polymarket_client": server_module.polymarket_client,
            "config": server_module.config,
        }

        fake_client = MagicMock()
        fake_client.has_api_credentials.return_value = True
        fake_client.has_verified_api_credentials.return_value = False

        fake_config = MagicMock()
        fake_config.WS_ENABLED = False

        try:
            server_module.polymarket_client = fake_client
            server_module.config = fake_config

            tools = await server_module.list_tools()
            tool_names = {tool.name for tool in tools}

            assert "place_order" not in tool_names
            assert "get_portfolio_value" not in tool_names
            assert "search_markets" in tool_names
        finally:
            server_module.polymarket_client = saved["polymarket_client"]
            server_module.config = saved["config"]


# ---------------------------------------------------------------------------
# SDK alignment — market order, direct order lookup, bulk cancel, trades, price history
# ---------------------------------------------------------------------------


class TestSDKAlignment:
    """
    Regression tests that verify our code uses the py-clob-client-v2 SDK correctly,
    matching the patterns in the official Polymarket agents repo and mjunaidca skills.
    """

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _build_client():
        """Build a PolymarketClient with a pre-injected set of API credentials."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "a" * 40,
                api_key="key",
                api_secret="secret",
                passphrase="passphrase",
            )
        return client

    @staticmethod
    def _build_trading_tools(mock_clob_client=None):
        """Build a TradingTools instance with an optional injected PolymarketClient mock."""
        from polymarket_mcp.utils.safety_limits import SafetyLimits

        config = PolymarketConfig(
            POLYGON_PRIVATE_KEY="0" * 64,
            POLYGON_ADDRESS="0x" + "0" * 40,
        )
        safety_limits = MagicMock(spec=SafetyLimits)
        safety_limits.validate_order.return_value = (True, None)
        safety_limits.should_require_confirmation.return_value = False

        pm_client = mock_clob_client or MagicMock()
        return TradingTools(client=pm_client, config=config, safety_limits=safety_limits)

    # ------------------------------------------------------------------
    # 1. Market orders use MarketOrderArgsV2 / create_and_post_market_order
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_post_market_order_calls_create_and_post_market_order(self):
        """PolymarketClient.post_market_order must delegate to create_and_post_market_order.

        The SDK's create_and_post_market_order(MarketOrderArgsV2(token_id, amount, side))
        is the correct FOK market-order path — it passes an *amount* of USDC to spend,
        not a price+size pair.
        """
        from py_clob_client_v2.clob_types import MarketOrderArgsV2

        client = self._build_client()

        captured = {}

        class FakeClobClient:
            def create_and_post_market_order(self_, order_args, **kwargs):
                captured["order_args"] = order_args
                return {"orderID": "mkt-001", "status": "matched"}

        client.client = FakeClobClient()

        result = await client.post_market_order(token_id="token-abc", amount=25.0, side="BUY")

        assert result["orderID"] == "mkt-001"
        assert isinstance(captured["order_args"], MarketOrderArgsV2)
        assert captured["order_args"].token_id == "token-abc"
        assert captured["order_args"].amount == 25.0
        assert captured["order_args"].side == "BUY"

    @pytest.mark.asyncio
    async def test_post_market_order_requires_l2_credentials(self):
        """post_market_order must raise RuntimeError when API creds are absent."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "a" * 40,
                signature_type=2,
            )
        # No credentials set
        with pytest.raises(RuntimeError, match="L2 API credentials required"):
            await client.post_market_order("token-abc", 10.0, "BUY")

    @pytest.mark.asyncio
    async def test_trading_tools_create_market_order_uses_post_market_order(self):
        """TradingTools.create_market_order must call client.post_market_order, not create_limit_order."""
        pm_client = AsyncMock()
        pm_client.get_orderbook = AsyncMock(
            return_value={
                "bids": [{"price": "0.55", "size": "500"}],
                "asks": [{"price": "0.60", "size": "400"}],
            }
        )
        pm_client.get_positions = AsyncMock(return_value=[])
        pm_client.post_market_order = AsyncMock(
            return_value={"orderID": "mkt-002", "status": "matched"}
        )

        tt = self._build_trading_tools(mock_clob_client=pm_client)

        # Patch market resolution
        tt._get_market_with_gamma_fallback = AsyncMock(
            return_value={
                "tokens": [{"token_id": "tok-yes", "outcome": "Yes"}],
                "volume": "10000",
            }
        )

        result = await tt.create_market_order(market_id="0xcondition", side="BUY", size=50.0)

        assert result["success"] is True
        assert result["execution_type"] == "market_order"
        # Must have called post_market_order with USDC amount, not shares
        pm_client.post_market_order.assert_awaited_once()
        call_kwargs = pm_client.post_market_order.call_args
        assert (
            call_kwargs.kwargs.get(
                "amount", call_kwargs.args[1] if len(call_kwargs.args) > 1 else None
            )
            == 50.0
        )

    @pytest.mark.asyncio
    async def test_trading_tools_create_market_order_does_not_call_create_limit_order(self):
        """Verify the old workaround (create_limit_order with FOK) is no longer used."""
        pm_client = AsyncMock()
        pm_client.get_orderbook = AsyncMock(
            return_value={
                "bids": [{"price": "0.45", "size": "100"}],
                "asks": [{"price": "0.55", "size": "200"}],
            }
        )
        pm_client.get_positions = AsyncMock(return_value=[])
        pm_client.post_market_order = AsyncMock(
            return_value={"orderID": "mkt-003", "status": "matched"}
        )

        tt = self._build_trading_tools(mock_clob_client=pm_client)
        tt._get_market_with_gamma_fallback = AsyncMock(
            return_value={
                "tokens": [{"token_id": "tok-yes", "outcome": "Yes"}],
                "volume": "5000",
            }
        )

        # Spy on create_limit_order — it should NOT be called for market orders
        tt.create_limit_order = AsyncMock()

        await tt.create_market_order(market_id="0xcondition", side="BUY", size=30.0)

        tt.create_limit_order.assert_not_awaited()

    # ------------------------------------------------------------------
    # 2. Direct order lookup via get_order (not scan-all-orders)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_order_calls_clob_get_order_directly(self):
        """PolymarketClient.get_order must call client.get_order(order_id) for O(1) lookup."""
        client = self._build_client()

        class FakeClobClient:
            def get_order(self_, order_id):
                return {"id": order_id, "status": "live", "size": "10", "price": "0.55"}

        client.client = FakeClobClient()
        order = await client.get_order("ord-xyz")

        assert order["id"] == "ord-xyz"
        assert order["status"] == "live"

    @pytest.mark.asyncio
    async def test_trading_tools_get_order_status_uses_get_order_not_get_orders(self):
        """TradingTools.get_order_status must use direct get_order(id), not get_orders() scan."""
        pm_client = AsyncMock()
        pm_client.get_order = AsyncMock(
            return_value={
                "id": "ord-direct",
                "status": "live",
                "size": "100",
                "sizeMatched": "40",
                "originalSize": "100",
            }
        )
        # get_orders should NOT be called
        pm_client.get_orders = AsyncMock()

        tt = self._build_trading_tools(mock_clob_client=pm_client)
        result = await tt.get_order_status("ord-direct")

        assert result["success"] is True
        assert result["order_id"] == "ord-direct"
        assert result["fill_status"]["fill_percentage"] == pytest.approx(40.0)
        pm_client.get_order.assert_awaited_once_with("ord-direct")
        pm_client.get_orders.assert_not_awaited()

    # ------------------------------------------------------------------
    # 3. Trade history via get_trades
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_trades_calls_clob_get_trades(self):
        """PolymarketClient.get_trades must call client.get_trades(TradeParams)."""
        from py_clob_client_v2.clob_types import TradeParams

        client = self._build_client()
        captured = {}

        class FakeClobClient:
            def get_trades(self_, params=None, **kwargs):
                captured["params"] = params
                return [{"tradeID": "t1"}, {"tradeID": "t2"}]

        client.client = FakeClobClient()
        trades = await client.get_trades(market="0xmarket", asset_id="0xtoken")

        assert len(trades) == 2
        assert captured["params"].market == "0xmarket"
        assert captured["params"].asset_id == "0xtoken"
        assert isinstance(captured["params"], TradeParams)

    @pytest.mark.asyncio
    async def test_get_trades_requires_l2_credentials(self):
        """get_trades must raise RuntimeError when API creds are absent."""
        with patch.object(PolymarketClient, "_initialize_client", return_value=None):
            client = PolymarketClient(
                private_key="0" * 64,
                address="0x" + "a" * 40,
                signature_type=2,
            )
        with pytest.raises(RuntimeError, match="L2 API credentials required"):
            await client.get_trades()

    # ------------------------------------------------------------------
    # 4. Bulk market cancel via cancel_market_orders (single API call)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cancel_market_orders_by_params_calls_sdk_endpoint(self):
        """PolymarketClient.cancel_market_orders_by_params must call cancel_market_orders(OrderMarketCancelParams)."""
        from py_clob_client_v2.clob_types import OrderMarketCancelParams

        client = self._build_client()
        captured = {}

        class FakeClobClient:
            def cancel_market_orders(self_, payload):
                captured["payload"] = payload
                return {"cancelled": ["ord-1", "ord-2"]}

        client.client = FakeClobClient()
        response = await client.cancel_market_orders_by_params(market="0xmarket")

        assert isinstance(captured["payload"], OrderMarketCancelParams)
        assert captured["payload"].market == "0xmarket"
        assert response["cancelled"] == ["ord-1", "ord-2"]

    @pytest.mark.asyncio
    async def test_cancel_market_orders_by_params_requires_market_or_asset(self):
        """cancel_market_orders_by_params must raise ValueError when neither market nor asset_id is given."""
        client = self._build_client()
        with pytest.raises(ValueError, match="Either market or asset_id must be provided"):
            await client.cancel_market_orders_by_params()

    @pytest.mark.asyncio
    async def test_trading_tools_cancel_market_orders_uses_single_api_call(self):
        """TradingTools.cancel_market_orders must use cancel_market_orders_by_params (not loop)."""
        pm_client = AsyncMock()
        pm_client.cancel_market_orders_by_params = AsyncMock(
            return_value={"cancelled": ["ord-a", "ord-b"]}
        )
        # get_orders must NOT be called when we use the single-call path
        pm_client.get_orders = AsyncMock()

        tt = self._build_trading_tools(mock_clob_client=pm_client)
        result = await tt.cancel_market_orders(market_id="0xcondition")

        assert result["success"] is True
        pm_client.cancel_market_orders_by_params.assert_awaited_once_with(
            market="0xcondition", asset_id=None
        )
        pm_client.get_orders.assert_not_awaited()

    # ------------------------------------------------------------------
    # 5. Price history uses real /prices-history endpoint (not stub)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_get_price_history_calls_prices_history_endpoint(self):
        """get_price_history must call /prices-history, not return a stub error."""
        from polymarket_mcp.tools import market_analysis

        history_payload = {"history": [{"t": 1700000000, "p": 0.55}, {"t": 1700003600, "p": 0.57}]}

        with patch.object(market_analysis, "_fetch_clob_api", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = history_payload
            result = await market_analysis.get_price_history("token-xyz", resolution="1h")

        # Must have called the real endpoint
        mock_fetch.assert_awaited_once()
        endpoint_called = mock_fetch.call_args.args[0]
        assert endpoint_called == "/prices-history"
        # Result must be the history list, not an error dict
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["p"] == 0.55

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "resolution,expected_interval",
        [
            ("1h", "1h"),
            ("6h", "6h"),
            ("1d", "1d"),
            ("1w", "1w"),
            ("max", "max"),
        ],
    )
    async def test_get_price_history_passes_interval_for_standard_resolutions(
        self, resolution, expected_interval
    ):
        """Each recognised resolution string must be forwarded as interval= to the CLOB."""
        from polymarket_mcp.tools import market_analysis

        with patch.object(market_analysis, "_fetch_clob_api", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {"history": []}
            await market_analysis.get_price_history("token-xyz", resolution=resolution)

        _, params = mock_fetch.call_args.args
        assert params.get("interval") == expected_interval, (
            f"resolution={resolution!r} should produce interval={expected_interval!r}, "
            f"got {params.get('interval')!r}"
        )

    @pytest.mark.asyncio
    async def test_get_price_history_does_not_return_stub_error(self):
        """get_price_history must never return the old 'not available' stub error dict."""
        from polymarket_mcp.tools import market_analysis

        with patch.object(market_analysis, "_fetch_clob_api", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {"history": []}
            result = await market_analysis.get_price_history("token-xyz")

        for item in result:
            assert "Historical price data not available" not in str(item)
            assert "error" not in item or "Historical price data" not in item.get("error", "")


class TestConfiguredHostFailures:
    """Regression tests for using only the configured host."""

    @pytest.mark.asyncio
    async def test_market_discovery_does_not_fall_back_from_polytest_gamma_host(self):
        from polymarket_mcp.tools import market_discovery

        original_gamma_url = market_discovery.GAMMA_API_URL
        market_discovery.set_gamma_api_url("https://gcomm-api.polytest.cloud")

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [{"id": "m1"}]

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(
            side_effect=[httpx.ConnectError("Name or service not known"), mock_response]
        )

        async_client_cm = AsyncMock()
        async_client_cm.__aenter__.return_value = mock_http_client
        async_client_cm.__aexit__.return_value = None

        with patch(
            "polymarket_mcp.tools.market_discovery.httpx.AsyncClient", return_value=async_client_cm
        ):
            with pytest.raises(httpx.ConnectError):
                await market_discovery._fetch_gamma_markets("/markets", {"active": "true"}, limit=1)

        requested_urls = [call.args[0] for call in mock_http_client.get.await_args_list]
        assert requested_urls[0].startswith("https://gcomm-api.polytest.cloud/")
        assert len(requested_urls) == 1

        market_discovery.set_gamma_api_url(original_gamma_url)

    @pytest.mark.asyncio
    async def test_market_analysis_does_not_fall_back_from_polytest_gamma_host(self):
        from polymarket_mcp.tools import market_analysis

        original_gamma_url = market_analysis.GAMMA_API_URL
        original_clob_url = market_analysis.CLOB_API_URL
        market_analysis.set_api_urls(
            "https://gcomm-api.polytest.cloud", "https://clob.polymarket.com"
        )

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [{"id": "m1"}]

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(
            side_effect=[httpx.ConnectError("Name or service not known"), mock_response]
        )

        async_client_cm = AsyncMock()
        async_client_cm.__aenter__.return_value = mock_http_client
        async_client_cm.__aexit__.return_value = None

        with patch(
            "polymarket_mcp.tools.market_analysis.httpx.AsyncClient", return_value=async_client_cm
        ):
            with pytest.raises(httpx.ConnectError):
                await market_analysis._fetch_gamma_api("/markets", {"active": "true"})

        requested_urls = [call.args[0] for call in mock_http_client.get.await_args_list]
        assert requested_urls[0].startswith("https://gcomm-api.polytest.cloud/")
        assert len(requested_urls) == 1

        market_analysis.set_api_urls(original_gamma_url, original_clob_url)

    @pytest.mark.asyncio
    async def test_market_analysis_does_not_fall_back_from_polytest_clob_host(self):
        from polymarket_mcp.tools import market_analysis

        original_gamma_url = market_analysis.GAMMA_API_URL
        original_clob_url = market_analysis.CLOB_API_URL
        market_analysis.set_api_urls(
            "https://gamma-api.polymarket.com", "https://clob-testnet.polytest.cloud"
        )

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"price": "0.42"}

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(
            side_effect=[httpx.ConnectError("Name or service not known"), mock_response]
        )

        async_client_cm = AsyncMock()
        async_client_cm.__aenter__.return_value = mock_http_client
        async_client_cm.__aexit__.return_value = None

        with patch(
            "polymarket_mcp.tools.market_analysis.httpx.AsyncClient", return_value=async_client_cm
        ):
            with pytest.raises(httpx.ConnectError):
                await market_analysis._fetch_clob_api("/price", {"token_id": "1", "side": "BUY"})

        requested_urls = [call.args[0] for call in mock_http_client.get.await_args_list]
        assert requested_urls[0].startswith("https://clob-testnet.polytest.cloud/")
        assert len(requested_urls) == 1

        market_analysis.set_api_urls(original_gamma_url, original_clob_url)


# ---------------------------------------------------------------------------
# Deposit wallet auto-discovery (type 3 startup flow)
# ---------------------------------------------------------------------------


class TestDepositWalletDiscovery:
    """Tests for automatic deposit wallet (POLYMARKET_FUNDER) discovery at startup."""

    SIGNER_ADDRESS = "0x" + "a" * 40
    DEPOSIT_WALLET = "0x" + "b" * 40
    PRIVATE_KEY = "0" * 64

    def _make_fake_clob_init(self, captured_args=None, signer_obj=None):
        """Return a fake ClobClient.__init__ that stores kwargs and exposes stub internals."""

        def fake_clob_init(self_inner, **kwargs):
            if captured_args is not None:
                captured_args.update(kwargs)
            self_inner.host = kwargs.get("host", "")
            self_inner.chain_id = kwargs.get("chain_id", 137)
            self_inner.signer = signer_obj
            self_inner.creds = None
            self_inner.mode = 0
            self_inner.builder = MagicMock()
            self_inner.use_server_time = False
            self_inner.retry_on_error = False
            self_inner.builder_config = None
            self_inner.fee_slippage = 0
            self_inner._ClobClient__tick_sizes = {}
            self_inner._ClobClient__neg_risk = {}
            self_inner._ClobClient__fee_rates = {}

        return fake_clob_init

    def test_discover_funder_address_returns_proxy_wallet(self):
        """_discover_funder_address extracts proxyWallet from the Gamma API response."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"proxyWallet": self.DEPOSIT_WALLET}

        with patch("polymarket_mcp.auth.client.httpx.get", return_value=response) as mock_get:
            result = PolymarketClient._discover_funder_address(
                self.SIGNER_ADDRESS,
                "https://gamma-api.polymarket.com",
            )

        mock_get.assert_called_once_with(
            "https://gamma-api.polymarket.com/public-profile",
            params={"address": self.SIGNER_ADDRESS},
            timeout=10.0,
        )
        assert result == self.DEPOSIT_WALLET

    def test_discover_funder_address_returns_none_without_proxy_wallet(self):
        """_discover_funder_address returns None when Gamma reports no proxy wallet."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"proxyWallet": None}

        with patch("polymarket_mcp.auth.client.httpx.get", return_value=response):
            result = PolymarketClient._discover_funder_address(
                self.SIGNER_ADDRESS,
                "https://gamma-api.polymarket.com",
            )

        assert result is None

    def test_discover_funder_address_returns_none_on_network_error(self):
        """_discover_funder_address returns None when the Gamma API call fails."""
        with patch(
            "polymarket_mcp.auth.client.httpx.get",
            side_effect=httpx.ConnectError("network error"),
        ):
            result = PolymarketClient._discover_funder_address(
                self.SIGNER_ADDRESS,
                "https://gamma-api.polymarket.com",
            )

        assert result is None

    def test_discover_funder_address_returns_none_when_proxy_matches_eoa(self):
        """_discover_funder_address returns None when Gamma reports the EOA itself."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"proxyWallet": self.SIGNER_ADDRESS}

        with patch("polymarket_mcp.auth.client.httpx.get", return_value=response):
            result = PolymarketClient._discover_funder_address(
                self.SIGNER_ADDRESS,
                "https://gamma-api.polymarket.com",
            )

        assert result is None

    def test_startup_uses_discovered_wallet_when_funder_equals_signer(self):
        """When funder==signer on type-3, __init__ discovers and uses the deposit wallet."""
        captured_args = {}
        mock_signer = MagicMock()
        profile_response = {"proxyWallet": self.DEPOSIT_WALLET}

        fake_init = self._make_fake_clob_init(captured_args, signer_obj=mock_signer)
        with patch.object(ClobClient, "__init__", fake_init):
            with patch("polymarket_mcp.auth.client.httpx.get") as mock_get:
                response = MagicMock()
                response.status_code = 200
                response.json.return_value = profile_response
                mock_get.return_value = response
                with patch.object(ClobClient, "update_balance_allowance"):
                    client = PolymarketClient(
                        private_key=self.PRIVATE_KEY,
                        address=self.SIGNER_ADDRESS,
                        signature_type=3,
                    )

        assert client.funder_address == self.DEPOSIT_WALLET.lower()
        assert captured_args.get("funder") == self.DEPOSIT_WALLET.lower()

    def test_startup_skips_discovery_when_funder_is_different_from_signer(self):
        """When POLYMARKET_FUNDER is explicitly set and != signer, discovery is skipped."""
        captured_args = {}
        mock_signer = MagicMock()

        fake_init = self._make_fake_clob_init(captured_args, signer_obj=mock_signer)
        discover_mock = MagicMock(return_value=self.DEPOSIT_WALLET)
        with patch.object(ClobClient, "__init__", fake_init):
            with patch.object(PolymarketClient, "_discover_funder_address", discover_mock):
                with patch.object(ClobClient, "update_balance_allowance"):
                    client = PolymarketClient(
                        private_key=self.PRIVATE_KEY,
                        address=self.SIGNER_ADDRESS,
                        funder="0x" + "c" * 40,
                        signature_type=3,
                    )

        discover_mock.assert_not_called()
        assert client.funder_address == "0x" + "c" * 40

    def test_startup_continues_with_signer_when_discovery_returns_none(self):
        """When discovery returns None, startup uses the signer as funder."""

        def fake_initialize(self_inner):
            self_inner.client = MagicMock()
            self_inner.clob_client = self_inner.client

        with patch.object(PolymarketClient, "_initialize_client", fake_initialize):
            with patch.object(
                PolymarketClient,
                "_discover_funder_address",
                return_value=None,
            ):
                client = PolymarketClient(
                    private_key=self.PRIVATE_KEY,
                    address=self.SIGNER_ADDRESS,
                    signature_type=3,
                )

        assert client.funder_address == self.SIGNER_ADDRESS.lower()

    def test_startup_logs_discovery_banner(self):
        """The discovery banner is emitted at WARNING level when a wallet is discovered."""
        mock_signer = MagicMock()
        profile_response = {"proxyWallet": self.DEPOSIT_WALLET}

        fake_init = self._make_fake_clob_init(signer_obj=mock_signer)
        with patch.object(ClobClient, "__init__", fake_init):
            with patch("polymarket_mcp.auth.client.httpx.get") as mock_get:
                response = MagicMock()
                response.status_code = 200
                response.json.return_value = profile_response
                mock_get.return_value = response
                with patch.object(ClobClient, "update_balance_allowance"):
                    with patch.object(
                        PolymarketClient,
                        "_log_deposit_wallet_discovery_banner",
                    ) as banner_mock:
                        PolymarketClient(
                            private_key=self.PRIVATE_KEY,
                            address=self.SIGNER_ADDRESS,
                            signature_type=3,
                        )

        banner_mock.assert_called_once_with(self.DEPOSIT_WALLET)

    def test_type3_update_balance_allowance_called_on_init(self):
        """For signature_type=3, update_balance_allowance is called at startup."""

        def fake_initialize(self_inner):
            self_inner.client = MagicMock()
            self_inner.clob_client = self_inner.client

        with patch.object(PolymarketClient, "_initialize_client", fake_initialize):
            with patch.object(
                PolymarketClient, "_discover_funder_address", return_value=self.DEPOSIT_WALLET
            ):
                with patch.object(
                    PolymarketClient, "_log_deposit_wallet_discovery_banner"
                ):
                    client = PolymarketClient(
                        private_key=self.PRIVATE_KEY,
                        address=self.SIGNER_ADDRESS,
                        signature_type=3,
                    )

        client.client.update_balance_allowance.assert_called_once()
        call_args = client.client.update_balance_allowance.call_args[0][0]
        assert call_args.signature_type == 3


# ---------------------------------------------------------------------------
# validate_startup_config warnings
# ---------------------------------------------------------------------------


class TestValidateStartupConfig:
    """Tests for the validate_startup_config() startup warning function."""

    def _make_config(self, **overrides):
        """Return a minimal object that looks like PolymarketConfig for validate_startup_config."""
        from types import SimpleNamespace

        defaults = {
            "POLYGON_RPC_URL": None,
            "POLYMARKET_SIGNATURE_TYPE": 3,
            "POLYGON_ADDRESS": "0x" + "a" * 40,
            "POLYMARKET_FUNDER": "0x" + "b" * 40,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_warns_for_cloudflare_eth_rpc_url(self):
        from polymarket_mcp.config import validate_startup_config

        cfg = self._make_config(POLYGON_RPC_URL="https://cloudflare-eth.com/v1/mainnet")
        with patch.object(logging.getLogger("polymarket_mcp.config"), "warning") as warn:
            validate_startup_config(cfg)
        messages = " ".join(call.args[0] for call in warn.call_args_list)
        assert "cloudflare-eth.com" in messages

    def test_no_warning_for_polygon_rpc(self):
        from polymarket_mcp.config import validate_startup_config

        cfg = self._make_config(POLYGON_RPC_URL="https://polygon-rpc.com")
        with patch.object(logging.getLogger("polymarket_mcp.config"), "warning") as warn:
            validate_startup_config(cfg)
        messages = " ".join(call.args[0] for call in warn.call_args_list)
        assert "cloudflare-eth.com" not in messages

    def test_warns_for_signature_type_0(self):
        from polymarket_mcp.config import validate_startup_config

        cfg = self._make_config(POLYMARKET_SIGNATURE_TYPE=0)
        with patch.object(logging.getLogger("polymarket_mcp.config"), "warning") as warn:
            validate_startup_config(cfg)
        messages = " ".join(call.args[0] for call in warn.call_args_list)
        assert "POLYMARKET_SIGNATURE_TYPE=0" in messages

    def test_warns_for_signature_type_1(self):
        from polymarket_mcp.config import validate_startup_config

        cfg = self._make_config(POLYMARKET_SIGNATURE_TYPE=1)
        with patch.object(logging.getLogger("polymarket_mcp.config"), "warning") as warn:
            validate_startup_config(cfg)
        messages = " ".join(call.args[0] for call in warn.call_args_list)
        assert "POLYMARKET_SIGNATURE_TYPE=1" in messages

    def test_warns_when_funder_equals_signer_for_type3(self):
        from polymarket_mcp.config import validate_startup_config

        addr = "0x" + "a" * 40
        cfg = self._make_config(
            POLYMARKET_SIGNATURE_TYPE=3,
            POLYGON_ADDRESS=addr,
            POLYMARKET_FUNDER=addr,
        )
        with patch.object(logging.getLogger("polymarket_mcp.config"), "warning") as warn:
            validate_startup_config(cfg)
        messages = " ".join(call.args[0] for call in warn.call_args_list)
        assert "POLYMARKET_FUNDER" in messages

    def test_no_warning_when_funder_equals_signer_for_type0(self):
        """For EOA mode (type 0), funder == signer is correct, no warning expected."""
        from polymarket_mcp.config import validate_startup_config

        addr = "0x" + "a" * 40
        cfg = self._make_config(
            POLYMARKET_SIGNATURE_TYPE=0,
            POLYGON_ADDRESS=addr,
            POLYMARKET_FUNDER=addr,
        )
        with patch.object(logging.getLogger("polymarket_mcp.config"), "warning") as warn:
            validate_startup_config(cfg)
        # Only EOA type-0 warning expected, not funder-mismatch warning
        funder_msgs = [
            call.args[0]
            for call in warn.call_args_list
            if "POLYMARKET_FUNDER" in call.args[0]
        ]
        assert not funder_msgs, f"Unexpected funder warning for type 0: {funder_msgs}"

    def test_no_warning_when_funder_differs_from_signer(self):
        """No funder warning when POLYMARKET_FUNDER is properly distinct from EOA."""
        from polymarket_mcp.config import validate_startup_config

        cfg = self._make_config(
            POLYMARKET_SIGNATURE_TYPE=3,
            POLYGON_ADDRESS="0x" + "a" * 40,
            POLYMARKET_FUNDER="0x" + "b" * 40,
        )
        with patch.object(logging.getLogger("polymarket_mcp.config"), "warning") as warn:
            validate_startup_config(cfg)
        funder_msgs = [
            call.args[0]
            for call in warn.call_args_list
            if "POLYMARKET_FUNDER" in call.args[0]
        ]
        assert not funder_msgs, f"Unexpected funder warning: {funder_msgs}"
