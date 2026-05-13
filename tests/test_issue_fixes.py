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

from polymarket_mcp.config import PolymarketConfig
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
                patch.object(server_module, "create_safety_limits_from_config", return_value=MagicMock()),
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
                patch.object(server_module.realtime, "get_tools", return_value=[MagicMock()]) as mock_realtime,
            ):
                tools = await server_module.list_tools()

            mock_realtime.assert_not_called()
            assert tools == []
        finally:
            server_module.config = original_config
            server_module.polymarket_client = original_polymarket_client
