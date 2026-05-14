"""
Polymarket CLOB client with authentication.
Handles L1 (private key) and L2 (API key) authentication.
"""

from typing import Dict, Any, List, Optional
import logging
import httpx
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    ApiCreds,
    OrderArgs,
    OrderType,
    BalanceAllowanceParams,
    AssetType,
    OrderPayload,
    OpenOrderParams,
)
from py_clob_client_v2.exceptions import PolyApiException

from .signer import OrderSigner

logger = logging.getLogger(__name__)

_CRED_BANNER = "=" * 70


def _log_new_credentials(api_key: str, api_secret: str, passphrase: str, reason: str) -> None:
    """
    Emit a visually prominent INFO block showing newly generated API credentials.

    The banner is logged at INFO level so it is always visible regardless of the
    configured log level, and it tells the operator exactly which docker-compose /
    .env variables they need to update and restart the container.

    Args:
        api_key: The newly issued POLYMARKET_API_KEY value.
        api_secret: The newly issued POLYMARKET_API_SECRET value.
        passphrase: The newly issued POLYMARKET_PASSPHRASE value.
        reason: Short description of why credentials were generated (shown in banner title).
    """
    logger.info(_CRED_BANNER)
    logger.info("⚠️  NEW POLYMARKET API CREDENTIALS GENERATED  ⚠️")
    logger.info("Reason: %s", reason)
    logger.info(_CRED_BANNER)
    logger.info("Copy the values below into your docker-compose.yml (or .env file):")
    logger.info("")
    # NOTE: Clear-text credential logging is intentional here.
    # The sole purpose of this function is to display newly-derived API keys to the
    # operator so they can persist them in their docker-compose / .env and avoid
    # re-deriving a fresh key on every restart (Polymarket imposes per-wallet limits).
    # Ensure your logging backend (log files, aggregators) has appropriate access controls.
    logger.info("  POLYMARKET_API_KEY=%s", api_key)  # codeql[py/clear-text-logging-sensitive-data]
    logger.info(
        "  POLYMARKET_API_SECRET=%s", api_secret
    )  # codeql[py/clear-text-logging-sensitive-data]
    logger.info(
        "  POLYMARKET_PASSPHRASE=%s", passphrase
    )  # codeql[py/clear-text-logging-sensitive-data]
    logger.info("")
    logger.info("Then RESTART the container so the new credentials are picked up:")
    logger.info("  docker compose down && docker compose up -d")
    logger.info(_CRED_BANNER)


class PolymarketClient:
    """
    Authenticated client for Polymarket CLOB API.

    Features:
    - L1 authentication with private key signing
    - L2 authentication with API key HMAC
    - Auto-creation of API credentials if not provided
    - Comprehensive market and trading operations
    """

    def __init__(
        self,
        private_key: str,
        address: str,
        chain_id: int = 137,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        host: str = "https://clob.polymarket.com",
        signature_type: Optional[int] = None,
        funder: Optional[str] = None,
    ):
        """
        Initialize Polymarket client.

        Args:
            private_key: Polygon wallet private key
            address: Polygon wallet address
            chain_id: Chain ID (137 for mainnet, 80002 for Amoy testnet)
            api_key: Optional L2 API key
            api_secret: Optional L2 API secret (same as passphrase)
            passphrase: Optional L2 API passphrase
            host: CLOB API host URL
            signature_type: Order signature type (0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE).
                Set to 1 when using a Polymarket proxy wallet (address differs from key-derived address).
            funder: Funder address for POLY_PROXY or POLY_GNOSIS_SAFE wallets.
                Should equal ``address`` for proxy wallets.  Leave None for plain EOA wallets.
        """
        self.private_key = private_key
        self.address = address.lower()
        self.chain_id = chain_id
        self.host = host
        self.signature_type = signature_type
        self.funder = funder

        # Initialize order signer
        self.signer = OrderSigner(private_key, chain_id)

        # L2 API credentials
        self.api_creds: Optional[ApiCreds] = None
        if api_key and (api_secret or passphrase):
            secret = api_secret or passphrase
            # Prefer the explicit passphrase when both values are provided, but
            # fall back to the secret for backward compatibility with older
            # environments that only persisted a single credential value.
            auth_passphrase = passphrase if passphrase is not None else secret
            self.api_creds = ApiCreds(
                api_key=api_key, api_secret=secret, api_passphrase=auth_passphrase
            )

        # Initialize CLOB client
        self.client: Optional[ClobClient] = None
        self._initialize_client()

        logger.info(
            f"PolymarketClient initialized for {self.address} "
            f"(chain_id: {chain_id}, L2 auth: {self.api_creds is not None}, "
            f"sig_type: {self.signature_type if self.signature_type is not None else 'EOA(default)'}, "
            f"funder: {'set' if self.funder else 'none'})"
        )

    def _initialize_client(self) -> None:
        """Initialize the ClobClient with appropriate authentication"""
        try:
            # Build client arguments
            client_args = {
                "host": self.host,
                "chain_id": self.chain_id,
                "key": self.private_key,
            }

            # Add L2 credentials if available
            if self.api_creds:
                client_args["creds"] = self.api_creds

            # Pass signature_type and funder when explicitly configured.
            # This is required for Polymarket proxy wallets (signature_type=1/POLY_PROXY)
            # where the maker address differs from the key-derived signer address.
            if self.signature_type is not None:
                client_args["signature_type"] = self.signature_type

            if self.funder is not None:
                client_args["funder"] = self.funder

            # Create client
            self.client = ClobClient(**client_args)

            logger.info("ClobClient initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize ClobClient: {e}")
            raise

    def get_client(self) -> ClobClient:
        """
        Get the underlying ClobClient instance.

        Returns:
            ClobClient instance

        Raises:
            RuntimeError: If client not initialized
        """
        if not self.client:
            raise RuntimeError("ClobClient not initialized")
        return self.client

    async def create_api_credentials(self, nonce_timeout: int = 3600) -> ApiCreds:
        """
        Create L2 API credentials for this wallet.

        This is required for authenticated operations like posting orders.
        The credentials are created once and can be reused.

        Args:
            nonce_timeout: Nonce timeout in seconds (default: 1 hour)

        Returns:
            ApiCreds object with api_key, api_secret, api_passphrase

        Raises:
            Exception: If credential creation fails
        """
        try:
            logger.info("Creating API credentials...")

            # Use the client's built-in method to create/derive credentials
            creds = self.client.create_or_derive_api_key()

            # Store credentials
            self.api_creds = ApiCreds(
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                api_passphrase=creds.api_passphrase,
            )

            # Reinitialize client with new credentials
            self._initialize_client()

            _log_new_credentials(
                api_key=self.api_creds.api_key,
                api_secret=self.api_creds.api_secret,
                passphrase=self.api_creds.api_passphrase,
                reason="No API credentials were configured — generated automatically on first run",
            )
            return self.api_creds

        except Exception as e:
            logger.error(f"Failed to create API credentials: {e}")
            raise

    async def get_markets(
        self, next_cursor: Optional[str] = None, limit: int = 100
    ) -> Dict[str, Any]:
        """
        Fetch markets from Polymarket.

        Args:
            next_cursor: Pagination cursor
            limit: Number of markets to fetch (max 100)

        Returns:
            Dictionary with markets data
        """
        try:
            # Use simplified markets endpoint
            markets = self.client.get_markets(next_cursor=next_cursor)
            return markets

        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            raise

    async def get_market(self, condition_id: str) -> Dict[str, Any]:
        """
        Fetch single market by condition ID.

        Args:
            condition_id: Market condition ID

        Returns:
            Market data dictionary
        """
        try:
            market = self.client.get_market(condition_id)
            return market

        except Exception as e:
            logger.error(f"Failed to fetch market {condition_id}: {e}")
            raise

    @staticmethod
    def _normalize_orderbook_level(level: Any) -> Dict[str, Any]:
        """Coerce a single orderbook price level to a plain dict."""
        if isinstance(level, dict):
            return level
        if hasattr(level, "__dict__") and isinstance(level.__dict__, dict):
            return dict(level.__dict__)
        return {}

    @staticmethod
    def _sort_orderbook_levels(
        levels: List[Dict[str, Any]], descending: bool
    ) -> List[Dict[str, Any]]:
        """Sort orderbook levels by price.

        Args:
            levels: List of price-level dicts with at least a ``price`` key.
            descending: True for bids (highest price first); False for asks (lowest price first).

        Returns:
            New list sorted so that ``levels[0]`` is always the best (most competitive) price.
        """
        try:
            return sorted(levels, key=lambda lvl: float(lvl.get("price", 0)), reverse=descending)
        except (TypeError, ValueError):
            return levels

    async def get_orderbook(self, token_id: str) -> Dict[str, Any]:
        """
        Fetch order book for a token.

        Args:
            token_id: Token ID to fetch orderbook for

        Returns:
            Order book with bids sorted descending (best bid at index 0) and asks sorted
            ascending (best ask at index 0).
        """
        try:
            orderbook = self.client.get_order_book(token_id)

            if isinstance(orderbook, dict):
                normalized = dict(orderbook)
            elif hasattr(orderbook, "__dict__") and isinstance(orderbook.__dict__, dict):
                # py-clob-client may return OrderBookSummary dataclass-like objects.
                normalized = dict(orderbook.__dict__)
            else:
                raise TypeError(f"Unsupported orderbook response type: {type(orderbook).__name__}")

            normalized["bids"] = self._sort_orderbook_levels(
                [self._normalize_orderbook_level(b) for b in (normalized.get("bids") or [])],
                descending=True,
            )
            normalized["asks"] = self._sort_orderbook_levels(
                [self._normalize_orderbook_level(a) for a in (normalized.get("asks") or [])],
                descending=False,
            )
            return normalized

        except Exception as e:
            logger.error(f"Failed to fetch orderbook for {token_id}: {e}")
            raise

    async def get_price(self, token_id: str, side: str) -> float:
        """
        Get current price for a token.

        Args:
            token_id: Token ID
            side: BUY or SELL

        Returns:
            Price as float
        """
        try:
            price_data = self.client.get_price(token_id, side.upper())
            return float(price_data.get("price", 0))

        except Exception as e:
            logger.error(f"Failed to fetch price for {token_id}: {e}")
            raise

    async def post_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type: str = "GTC",
        expiration: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Post a limit order.

        Args:
            token_id: Token ID to trade
            price: Limit price (0-1 for probabilities)
            size: Order size in shares
            side: BUY or SELL
            order_type: Order type (GTC, FOK, GTD)
            expiration: Order expiration timestamp (required for GTD)

        Returns:
            Order response dictionary

        Raises:
            RuntimeError: If L2 credentials not available
        """
        if not self.api_creds:
            raise RuntimeError(
                "L2 API credentials required for posting orders. "
                "Call create_api_credentials() first."
            )

        try:
            # Map string order_type to OrderType enum
            order_type_map = {
                "GTC": OrderType.GTC,
                "FOK": OrderType.FOK,
                "GTD": OrderType.GTD,
                "FAK": OrderType.FAK,
            }
            order_type_upper = order_type.upper()
            if order_type_upper not in order_type_map:
                logger.warning(f"Unknown order_type '{order_type}', defaulting to GTC")
            order_type_enum = order_type_map.get(order_type_upper, OrderType.GTC)

            # Build order args for V2 (fee_rate_bps and nonce are managed by the SDK)
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side.upper(),
                expiration=expiration or 0,
            )

            # create_and_post_order handles V1/V2 version negotiation and auto-retry
            order_response = self.client.create_and_post_order(
                order_args, order_type=order_type_enum
            )

            logger.info(
                f"Order posted: {side} {size} @ {price} "
                f"(token: {token_id}, order_id: {order_response.get('orderID')})"
            )

            return order_response

        except Exception as e:
            logger.error(f"Failed to post order: {e}")
            raise

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        Cancel an open order.

        Args:
            order_id: ID of order to cancel

        Returns:
            Cancellation response

        Raises:
            RuntimeError: If L2 credentials not available
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required for canceling orders")

        try:
            response = self.client.cancel_order(OrderPayload(orderID=order_id))

            logger.info(f"Order cancelled: {order_id}")
            return response

        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            raise

    async def cancel_all_orders(self) -> Dict[str, Any]:
        """
        Cancel all open orders.

        Returns:
            Cancellation response

        Raises:
            RuntimeError: If L2 credentials not available
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required")

        try:
            response = self.client.cancel_all()

            logger.info("All orders cancelled")
            return response

        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            raise

    async def get_orders(
        self, market: Optional[str] = None, asset_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get user's open orders.

        Args:
            market: Filter by market ID
            asset_id: Filter by asset ID

        Returns:
            List of open orders

        Raises:
            RuntimeError: If L2 credentials not available
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required")

        try:
            params = OpenOrderParams(
                market=market,
                asset_id=asset_id,
            )
            orders = self.client.get_open_orders(params)
            return orders

        except Exception as e:
            logger.error(f"Failed to fetch orders: {e}")
            raise

    async def get_positions(self) -> List[Dict[str, Any]]:
        """
        Get user's positions.

        Returns:
            List of positions

        Raises:
            RuntimeError: If L2 credentials not available
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required")

        try:
            get_positions_fn = getattr(self.client, "get_positions", None)
            if callable(get_positions_fn):
                try:
                    positions = get_positions_fn(self.address)
                except TypeError:
                    positions = get_positions_fn()
                if isinstance(positions, list):
                    return positions

            # SDK >=0.28 no longer exposes get_positions(); fallback to Data API.
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    "https://data-api.polymarket.com/positions",
                    params={"user": self.address},
                )
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, list) else []

        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            raise

    @staticmethod
    def _extract_numeric_balance(balance_data: Dict[str, Any]) -> float:
        """Extract a numeric balance from known balance/allowance payload shapes."""
        candidates = [
            balance_data.get("balance"),
            balance_data.get("available"),
            balance_data.get("available_balance"),
            balance_data.get("amount"),
        ]

        for nested_key in ("collateral", "usdc", "data", "balance_allowance"):
            nested = balance_data.get(nested_key)
            if isinstance(nested, dict):
                candidates.extend(
                    [
                        nested.get("balance"),
                        nested.get("available"),
                        nested.get("available_balance"),
                        nested.get("amount"),
                        nested.get("value"),
                    ]
                )

        for value in candidates:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue

        return 0.0

    async def get_balance(self) -> Dict[str, Any]:
        """
        Get user's USDC balance.

        Returns:
            Dictionary with balance info

        Raises:
            RuntimeError: If L2 credentials not available
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required")

        try:
            return self._fetch_balance_once()
        except PolyApiException as e:
            if e.status_code == 401:
                logger.warning(
                    "Received 401 from balance endpoint; refreshing API credentials and retrying..."
                )
                self._refresh_api_credentials()
                return self._fetch_balance_once()
            logger.error(f"Failed to fetch balance: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            raise

    def _fetch_balance_once(self) -> Dict[str, Any]:
        """Execute the balance API call using the current credentials (no retry logic)."""
        # Prefer legacy SDK method when available.
        get_balance_fn = getattr(self.client, "get_balance", None)
        if callable(get_balance_fn):
            try:
                balance_data = get_balance_fn(self.address)
            except TypeError:
                # Some SDK variants may expose a no-arg get_balance()
                balance_data = get_balance_fn()

            if isinstance(balance_data, dict):
                return balance_data
            return {"balance": str(balance_data)}

        # SDK >=0.28 exposes get_balance_allowance() instead.
        get_balance_allowance_fn = getattr(self.client, "get_balance_allowance", None)
        if callable(get_balance_allowance_fn):
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            balance_data = get_balance_allowance_fn(params)
            if isinstance(balance_data, dict):
                normalized = dict(balance_data)
                normalized.setdefault("balance", str(self._extract_numeric_balance(normalized)))
                return normalized
            return {"balance": str(balance_data)}

        raise AttributeError("ClobClient does not expose a supported balance method")

    def has_api_credentials(self) -> bool:
        """Check if L2 API credentials are available"""
        return self.api_creds is not None

    async def ensure_valid_api_credentials(self) -> None:
        """
        Ensure that L2 API credentials are present and valid.

        Designed to be called once at server startup so that credential problems
        are caught and resolved eagerly rather than surfacing on the first user
        request.

        Behaviour:
        - No credentials configured → creates new credentials via
          ``create_api_credentials()`` and logs the banner with the new values.
        - Credentials configured → probes the CLOB balance endpoint to confirm
          they are accepted.  On HTTP 401 the credentials are automatically
          refreshed via ``_refresh_api_credentials()`` and the new values are
          logged.  On any other error (network timeout, service unavailable, etc.)
          a warning is emitted and startup continues with the existing credentials.

        Raises:
            Exception: If no credentials exist *and* creating new ones fails
                       (e.g. the wallet has insufficient allowance).
        """
        if not self.api_creds:
            logger.info("No API credentials found. Attempting to create...")
            await self.create_api_credentials()
            logger.info("API credentials created successfully!")
            return

        logger.info("Testing existing API credentials...")
        try:
            self._fetch_balance_once()
            logger.info("API credentials verified successfully.")
        except PolyApiException as e:
            if e.status_code == 401:
                logger.warning(
                    "API credentials rejected (HTTP 401) — refreshing credentials..."
                )
                self._refresh_api_credentials()
                logger.info("API credentials refreshed successfully.")
            else:
                logger.warning(
                    "API credential probe returned an unexpected error (%s). "
                    "Credentials may still be valid; continuing startup.",
                    e,
                )
        except Exception as e:
            logger.warning(
                "Could not verify API credentials at startup (%s). "
                "Credentials may still be valid; continuing startup.",
                e,
            )

    def _refresh_api_credentials(self) -> None:
        """
        Re-derive L2 API credentials from the wallet private key and update the client.

        Called automatically when an authenticated request returns HTTP 401 to recover
        from stale or expired API keys without requiring a server restart.

        Raises:
            Exception: If credential derivation fails.
        """
        logger.info("Refreshing API credentials via create_or_derive_api_key()...")
        new_creds = self.client.create_or_derive_api_key()
        self.api_creds = ApiCreds(
            api_key=new_creds.api_key,
            api_secret=new_creds.api_secret,
            api_passphrase=new_creds.api_passphrase,
        )
        # Push updated creds into the live ClobClient instance so subsequent
        # calls use the new key without a full re-initialization.
        self.client.set_api_creds(self.api_creds)
        _log_new_credentials(
            api_key=self.api_creds.api_key,
            api_secret=self.api_creds.api_secret,
            passphrase=self.api_creds.api_passphrase,
            reason="HTTP 401 received — existing API credentials were stale or invalid",
        )

    def get_address(self) -> str:
        """Get wallet address"""
        return self.address

    def get_chain_id(self) -> int:
        """Get chain ID"""
        return self.chain_id


def create_polymarket_client(
    private_key: str,
    address: str,
    chain_id: int = 137,
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    passphrase: Optional[str] = None,
    signature_type: Optional[int] = None,
    funder: Optional[str] = None,
) -> PolymarketClient:
    """
    Create PolymarketClient instance.

    Args:
        private_key: Polygon wallet private key
        address: Polygon wallet address
        chain_id: Chain ID (default: 137)
        api_key: Optional L2 API key
        api_secret: Optional L2 API secret
        passphrase: Optional L2 API passphrase
        signature_type: Order signature type (0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE).
            Required for Polymarket proxy wallets to avoid order_version_mismatch errors.
        funder: Funder address for POLY_PROXY / POLY_GNOSIS_SAFE wallets (typically equals address).

    Returns:
        PolymarketClient instance
    """
    return PolymarketClient(
        private_key=private_key,
        address=address,
        chain_id=chain_id,
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        signature_type=signature_type,
        funder=funder,
    )
