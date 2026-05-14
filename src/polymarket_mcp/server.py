"""
Polymarket MCP Server - Main entry point.

Provides MCP server for Polymarket trading integration with Claude Desktop.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, Router
import uvicorn

from .config import load_config, PolymarketConfig
from .auth import PolymarketClient, create_polymarket_client
from .utils import (
    get_rate_limiter,
    create_safety_limits_from_config,
    SafetyLimits,
    WebSocketManager,
)
from .tools import (
    market_discovery,
    market_analysis,
    TradingTools,
    get_tool_definitions,
    portfolio_integration,
    realtime,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Global instances
server = Server("polymarket-trading")
config: Optional[PolymarketConfig] = None
polymarket_client: Optional[PolymarketClient] = None
safety_limits: Optional[SafetyLimits] = None
rate_limiter = None
trading_tools: Optional[TradingTools] = None
websocket_manager: Optional[WebSocketManager] = None


class StreamableHTTPASGIApp:
    """ASGI adapter for MCP Streamable HTTP transport."""

    def __init__(self, session_manager: StreamableHTTPSessionManager):
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send) -> None:
        await self.session_manager.handle_request(scope, receive, send)


def get_transport_mode() -> str:
    """Get MCP transport mode from environment."""
    raw = os.getenv("MCP_TRANSPORT", "streamable-http").strip().lower()
    normalized = raw.replace("_", "-")
    if normalized not in {"streamable-http", "stdio"}:
        raise ValueError(
            "Invalid MCP_TRANSPORT value. Use 'streamable-http' or 'stdio' "
            "(underscores are normalized automatically)."
        )
    return normalized


def get_streamable_http_settings() -> tuple[str, int, str]:
    """Get Streamable HTTP host, port, and endpoint path from environment."""
    host = os.getenv("MCP_STREAMABLE_HTTP_HOST", "0.0.0.0").strip() or "0.0.0.0"
    raw_port = os.getenv("MCP_STREAMABLE_HTTP_PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(
            f"Invalid MCP_STREAMABLE_HTTP_PORT '{raw_port}'. Use a valid integer port."
        ) from exc
    if not (1 <= port <= 65535):
        raise ValueError(
            f"Invalid MCP_STREAMABLE_HTTP_PORT '{raw_port}'. Use a port between 1 and 65535."
        )
    raw_path = os.getenv("MCP_STREAMABLE_HTTP_PATH", "/mcp").strip() or "/mcp"
    path = raw_path if raw_path.startswith("/") else f"/{raw_path}"
    return host, port, path


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """
    List available tools.

    Returns:
        List of tools (conditional on authentication):
        - 8 Market Discovery tools (always available - public API)
        - 10 Market Analysis tools (always available - public API)
        - 12 Trading tools (requires API credentials)
        - 8 Portfolio Management tools (requires API credentials)
        - 7 Real-time WebSocket tools (partial - some require auth)
    """
    tools = []

    # Always available - public APIs (no auth needed)
    tools.extend(market_discovery.get_tools())
    tools.extend(market_analysis.get_tools())

    # Only available with API credentials
    has_credentials = polymarket_client and polymarket_client.has_api_credentials()

    if has_credentials:
        # Trading tools (require L2 auth)
        tools.extend(get_tool_definitions())
        # Portfolio management tools (require auth)
        tools.extend(portfolio_integration.get_portfolio_tool_definitions())
        logger.info("Trading and Portfolio tools enabled (authenticated)")
    else:
        logger.info("Trading and Portfolio tools disabled (no API credentials - read-only mode)")

    # Real-time tools can be disabled via WS_ENABLED
    websocket_enabled = config.WS_ENABLED if config else True
    if websocket_enabled:
        tools.extend(realtime.get_tools())
    else:
        logger.info("Real-time tools disabled (WS_ENABLED=false)")

    return tools


@server.list_resources()
async def list_resources() -> list[types.Resource]:
    """
    List available resources for Claude to access.

    Resources provide read-only access to:
    - Server status and configuration
    - Rate limiter status
    - Safety limits configuration
    """
    resources = [
        types.Resource(
            uri="polymarket://status",
            name="Connection Status",
            description="Check Polymarket connection and authentication status",
            mimeType="application/json",
        ),
        types.Resource(
            uri="polymarket://config",
            name="Configuration",
            description="View current safety limits and trading configuration",
            mimeType="application/json",
        ),
        types.Resource(
            uri="polymarket://rate-limits",
            name="Rate Limiter Status",
            description="Check API rate limit status across all endpoint categories",
            mimeType="application/json",
        ),
    ]

    return resources


@server.read_resource()
async def read_resource(uri: str) -> str:
    """
    Read resource content by URI.

    Args:
        uri: Resource URI (e.g., polymarket://status)

    Returns:
        JSON string with resource data
    """
    import json

    if uri == "polymarket://status":
        # Connection and authentication status
        status_data = {
            "connected": polymarket_client is not None,
            "address": config.POLYGON_ADDRESS if config else None,
            "chain_id": config.POLYMARKET_CHAIN_ID if config else None,
            "has_api_credentials": (
                polymarket_client.has_api_credentials() if polymarket_client else False
            ),
            "server_version": "0.1.0",
        }
        return json.dumps(status_data, indent=2)

    elif uri == "polymarket://config":
        # Safety limits and configuration
        if not config or not safety_limits:
            return json.dumps({"error": "Configuration not loaded"})

        config_data = {
            "safety_limits": {
                "max_order_size_usd": safety_limits.max_order_size_usd,
                "max_total_exposure_usd": safety_limits.max_total_exposure_usd,
                "max_position_size_per_market": safety_limits.max_position_size_per_market,
                "min_liquidity_required": safety_limits.min_liquidity_required,
                "max_spread_tolerance": safety_limits.max_spread_tolerance,
            },
            "trading_controls": {
                "enable_autonomous_trading": config.ENABLE_AUTONOMOUS_TRADING,
                "require_confirmation_above_usd": config.REQUIRE_CONFIRMATION_ABOVE_USD,
                "auto_cancel_on_large_spread": config.AUTO_CANCEL_ON_LARGE_SPREAD,
            },
            "endpoints": {
                "clob_api": config.CLOB_API_URL,
                "gamma_api": config.GAMMA_API_URL,
            },
        }
        return json.dumps(config_data, indent=2)

    elif uri == "polymarket://rate-limits":
        # Rate limiter status
        rate_limiter = get_rate_limiter()
        status = rate_limiter.get_status()
        return json.dumps(status, indent=2)

    else:
        return json.dumps({"error": f"Unknown resource: {uri}"})


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> list[types.TextContent]:
    """
    Handle tool calls from Claude.

    Args:
        name: Tool name
        arguments: Tool arguments

    Returns:
        List of TextContent with tool results
    """
    import json

    try:
        # Route to market discovery tools
        if name in [
            "search_markets",
            "get_trending_markets",
            "filter_markets_by_category",
            "get_event_markets",
            "get_featured_markets",
            "get_closing_soon_markets",
            "get_sports_markets",
            "get_crypto_markets",
        ]:
            return await market_discovery.handle_tool(name, arguments)

        # Route to market analysis tools
        elif name in [
            "get_market_details",
            "get_current_price",
            "get_orderbook",
            "get_spread",
            "get_market_volume",
            "get_liquidity",
            "get_price_history",
            "get_market_holders",
            "analyze_market_opportunity",
            "compare_markets",
        ]:
            return await market_analysis.handle_tool(name, arguments)

        # Route to portfolio management tools
        elif name in [
            "get_all_positions",
            "get_position_details",
            "get_portfolio_value",
            "get_pnl_summary",
            "get_trade_history",
            "get_activity_log",
            "analyze_portfolio_risk",
            "suggest_portfolio_actions",
        ]:
            if not rate_limiter:
                raise ValueError(
                    "Rate limiter not initialized. Ensure initialize_server() has run first."
                )
            return await portfolio_integration.call_portfolio_tool(
                name, arguments, polymarket_client, rate_limiter, config
            )

        # Route to real-time websocket tools
        elif name in [
            "subscribe_market_prices",
            "subscribe_orderbook_updates",
            "subscribe_user_orders",
            "subscribe_user_trades",
            "subscribe_market_resolution",
            "get_realtime_status",
            "unsubscribe_realtime",
        ]:
            if config and not config.WS_ENABLED:
                raise ValueError("WebSocket features are disabled (WS_ENABLED=false)")
            if not websocket_manager:
                raise ValueError("WebSocket manager not initialized")
            return await realtime.handle_tool_call(name, arguments)

        # Route to trading tools
        elif trading_tools:
            if name == "create_limit_order":
                result = await trading_tools.create_limit_order(**arguments)
            elif name == "create_market_order":
                result = await trading_tools.create_market_order(**arguments)
            elif name == "create_batch_orders":
                result = await trading_tools.create_batch_orders(**arguments)
            elif name == "suggest_order_price":
                result = await trading_tools.suggest_order_price(**arguments)
            elif name == "get_order_status":
                result = await trading_tools.get_order_status(**arguments)
            elif name == "get_open_orders":
                result = await trading_tools.get_open_orders(**arguments)
            elif name == "get_order_history":
                result = await trading_tools.get_order_history(**arguments)
            elif name == "cancel_order":
                result = await trading_tools.cancel_order(**arguments)
            elif name == "cancel_market_orders":
                result = await trading_tools.cancel_market_orders(**arguments)
            elif name == "cancel_all_orders":
                result = await trading_tools.cancel_all_orders()
            elif name == "execute_smart_trade":
                result = await trading_tools.execute_smart_trade(**arguments)
            elif name == "rebalance_position":
                result = await trading_tools.rebalance_position(**arguments)
            else:
                raise ValueError(f"Unknown tool: {name}")

            # Return result as JSON
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        logger.exception("Tool call failed: %s", name)
        error_result = {"success": False, "error": str(e), "tool": name, "arguments": arguments}
        return [types.TextContent(type="text", text=json.dumps(error_result, indent=2))]


async def initialize_server() -> None:
    """
    Initialize server components.

    - Load configuration from environment
    - Initialize Polymarket client
    - Set up safety limits
    - Initialize rate limiter
    - Initialize trading tools
    - Initialize WebSocket manager
    """
    global config, polymarket_client, safety_limits, rate_limiter, trading_tools, websocket_manager

    try:
        # Load configuration
        logger.info("Loading configuration...")
        config = load_config()

        # Set log level from config
        logging.getLogger().setLevel(config.LOG_LEVEL)

        logger.info(f"Configuration loaded for address: {config.POLYGON_ADDRESS}")

        # Initialize Polymarket client
        logger.info("Initializing Polymarket client...")
        polymarket_client = create_polymarket_client(
            private_key=config.POLYGON_PRIVATE_KEY,
            address=config.POLYGON_ADDRESS,
            chain_id=config.POLYMARKET_CHAIN_ID,
            api_key=config.POLYMARKET_API_KEY,
            api_secret=config.POLYMARKET_API_SECRET or config.POLYMARKET_PASSPHRASE,
            passphrase=config.POLYMARKET_PASSPHRASE,
            signature_type=config.POLYMARKET_SIGNATURE_TYPE,
            funder=config.POLYMARKET_FUNDER,
        )

        # Test (and if necessary refresh or create) API credentials at startup.
        # This runs on every container start so that invalid/expired keys are
        # detected and regenerated before any user request arrives.
        try:
            await polymarket_client.ensure_valid_api_credentials()
        except Exception as e:
            logger.warning("Could not create or validate API credentials: %s", e)
            logger.info("Continuing in READ-ONLY mode")
            logger.info("Available: Market Discovery (8 tools) + Market Analysis (10 tools)")
            logger.info("Unavailable: Trading (12 tools) + Portfolio (8 tools)")
            logger.info(
                "To enable trading, fund your wallet or configure existing API credentials"
            )

        # Initialize safety limits
        logger.info("Initializing safety limits...")
        safety_limits = create_safety_limits_from_config(config)

        # Initialize rate limiter (singleton)
        rate_limiter = get_rate_limiter()
        logger.info("Rate limiter initialized")

        # Initialize trading tools (only if authenticated)
        if polymarket_client.has_api_credentials():
            logger.info("Initializing trading tools...")
            trading_tools = TradingTools(
                client=polymarket_client, safety_limits=safety_limits, config=config
            )
            logger.info("Trading tools initialized with 12 tools")
        else:
            logger.info("Trading tools NOT initialized (no API credentials - read-only mode)")

        websocket_manager = None
        if config.WS_ENABLED:
            # Initialize WebSocket manager
            logger.info("Initializing WebSocket manager...")
            websocket_manager = WebSocketManager(config)
            realtime.set_websocket_manager(websocket_manager)

            async def _initialize_websocket_manager() -> None:
                try:
                    await websocket_manager.connect()
                    await websocket_manager.start_background_task()
                except Exception as e:
                    logger.exception("Failed to initialize WebSocket background task: %s", e)

            asyncio.create_task(_initialize_websocket_manager())
            logger.info("WebSocket manager initialized with 7 real-time tools")
        else:
            logger.info("WebSocket manager disabled (WS_ENABLED=false)")

        logger.info("Server initialization complete!")
        logger.info(f"Connected to Polymarket on chain ID {config.POLYMARKET_CHAIN_ID}")

        # Report available tools based on authentication
        if polymarket_client.has_api_credentials():
            logger.info("Mode: FULL (authenticated)")
            logger.info(
                "Available tools: 45 total (8 Discovery, 10 Analysis, 12 Trading, 8 Portfolio, 7 Real-time)"
            )
        else:
            logger.info("Mode: READ-ONLY (no API credentials)")
            logger.info("Available tools: 25 total (8 Discovery, 10 Analysis, 7 Real-time)")
            logger.info("Trading and Portfolio tools require API credentials")

    except Exception as e:
        logger.exception("Failed to initialize server: %s", e)
        raise


async def main() -> None:
    """
    Main entry point for MCP server.

    Initializes components and runs the configured MCP transport.
    """
    try:
        transport_mode = get_transport_mode()
        logger.info("Selected MCP transport: %s", transport_mode)

        if transport_mode == "stdio":
            # Initialize server components
            await initialize_server()

            # Run MCP server with stdio transport
            logger.info("Starting MCP server (stdio transport)...")
            async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )
        else:
            host, port, path = get_streamable_http_settings()
            session_manager = StreamableHTTPSessionManager(app=server)
            streamable_http_app = StreamableHTTPASGIApp(session_manager)

            @asynccontextmanager
            async def lifespan(app: Starlette):
                await initialize_server()
                async with session_manager.run():
                    yield

            async def health_check(request: Request) -> JSONResponse:
                return JSONResponse({"status": "ok", "transport": "streamable-http"})

            async def sse_not_supported(request: Request) -> PlainTextResponse:
                return PlainTextResponse(
                    f"SSE transport is not supported by this server.\n"
                    f"Use streamable-http transport: send POST requests to {path}",
                    status_code=501,
                )

            # Use Route(path + "{extra:path}", ...) instead of Mount(path, ...)
            # so that requests to the exact path (e.g. POST /mcp) are matched.
            # Mount("/mcp") generates regex ^/mcp/(?P<path>.*)$ which requires a
            # trailing slash and therefore misses POST /mcp, returning 404.
            # Route("/mcp{extra:path}") generates ^/mcp(?P<extra>.*)$ which matches
            # /mcp, /mcp/, and /mcp/<session-id> alike.
            router = Router(
                routes=[
                    Route("/health", endpoint=health_check),
                    Route("/healthz", endpoint=health_check),
                    Route("/ping", endpoint=health_check),
                    Route("/ready", endpoint=health_check),
                    Route(path + "/sse", endpoint=sse_not_supported, methods=["GET", "POST"]),
                    Route(path + "{extra:path}", endpoint=streamable_http_app),
                ],
                redirect_slashes=False,
                lifespan=lifespan,
            )

            logger.info(
                "Starting MCP server (streamable-http) on http://%s:%s%s",
                host,
                port,
                path,
            )
            uvicorn_server = uvicorn.Server(
                uvicorn.Config(router, host=host, port=port, log_level="info")
            )
            await uvicorn_server.serve()

    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.exception("Server error: %s", e)
        raise


def run():
    """Synchronous entry point for CLI"""
    asyncio.run(main())


if __name__ == "__main__":
    run()
