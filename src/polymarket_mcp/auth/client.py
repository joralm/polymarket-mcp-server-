"""
Polymarket CLOB client with authentication.
Handles L1 (private key) and L2 (API key) authentication.
"""

from typing import Dict, Any, List, Optional
import logging
import re
from itertools import chain
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
    MarketOrderArgsV2,
    TradeParams,
    OrderMarketCancelParams,
)
from py_clob_client_v2.exceptions import PolyApiException

from .signer import OrderSigner

logger = logging.getLogger(__name__)

_CRED_BANNER = "=" * 70
_USDC_DECIMALS = 6
_USDC_SCALE = 10**_USDC_DECIMALS


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
    logger.info(
        "  POLYMARKET_RELAYER_KEY=%s", api_key
    )  # codeql[py/clear-text-logging-sensitive-data]
    logger.info(
        "  POLYMARKET_RELAYER_SECRET=%s", api_secret
    )  # codeql[py/clear-text-logging-sensitive-data]
    logger.info(
        "  POLYMARKET_RELAYER_PASSPHRASE=%s", passphrase
    )  # codeql[py/clear-text-logging-sensitive-data]
    logger.info("  # Legacy aliases (same values):")
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
        signature_type: int = 3,
        funder: Optional[str] = None,
        host: str = "https://clob.polymarket.com",
    ):
        """
        Initialize Polymarket client.

        Args:
            private_key: Polygon wallet private key
            address: Polygon wallet address
            chain_id: Chain ID (137 for mainnet, 80002 for Amoy testnet)
            api_key: Optional L2 API key
            api_secret: Optional L2 API secret
            passphrase: Optional L2 API passphrase
            signature_type: Polymarket wallet signature type
            funder: Funding / deposit wallet address used for settlement
            host: CLOB API host URL
        """
        self.private_key = private_key
        self.address = address.lower()
        self.funder_address = (funder or address).lower()
        self.chain_id = chain_id
        if signature_type != 3:
            logger.warning(
                "signature_type=%s requested, but this server enforces MetaMask deposit-wallet flow "
                "(signature_type=3). Overriding to 3.",
                signature_type,
            )
        self.signature_type = 3
        self.host = host

        # Initialize order signer
        self.signer = OrderSigner(private_key, chain_id)

        # L2 API credentials
        self.api_creds: Optional[ApiCreds] = None
        self._api_creds_from_config = False
        self._allow_credential_derivation = False
        self._expected_api_key = api_key
        self._api_credentials_verified = False
        self._zero_balance_proxy_refresh_attempted = False
        if api_key and api_secret and passphrase:
            self.api_creds = ApiCreds(
                api_key=api_key, api_secret=api_secret, api_passphrase=passphrase
            )
            self._api_creds_from_config = True
        elif api_key:
            self._api_creds_from_config = False
            self._allow_credential_derivation = True
        else:
            # No credentials at all — all three L2 fields will be derived from
            # the private key via create_or_derive_api_key() when credentials
            # are first needed (official Polymarket Python SDK flow).
            self._api_creds_from_config = False
            self._allow_credential_derivation = True

        # Initialize CLOB client
        self.client: Optional[ClobClient] = None
        self._initialize_client()

        logger.info(
            f"PolymarketClient initialized for signer {self.address} "
            f"(funder: {self.funder_address}, chain_id: {chain_id}, "
            f"signature_type: {self.signature_type}, L2 auth: {self.api_creds is not None})"
        )
        logger.debug(
            "Auth bootstrap state: creds_from_config=%s, allow_derivation=%s, verified=%s, "
            "zero_balance_refresh_attempted=%s",
            self._api_creds_from_config,
            self._allow_credential_derivation,
            self._api_credentials_verified,
            self._zero_balance_proxy_refresh_attempted,
        )

    def _initialize_client(self) -> None:
        """Initialize the ClobClient with appropriate authentication"""
        try:
            # Build client arguments. Polymarket separates the signing EOA
            # from the wallet that actually holds funds/positions (`funder`).
            # For deposit-wallet / proxy flows the funder may differ from the
            # signer address shown in MetaMask.
            client_args = {
                "host": self.host,
                "chain_id": self.chain_id,
                "key": self.private_key,
                "signature_type": self.signature_type,
                "funder": self.funder_address,
            }

            # Add L2 credentials if available
            if self.api_creds:
                client_args["creds"] = self.api_creds
            logger.debug(
                "Initializing ClobClient with host=%s chain_id=%s signature_type=%s signer=%s funder=%s has_l2=%s",
                self.host,
                self.chain_id,
                self.signature_type,
                self.address,
                self.funder_address,
                self.api_creds is not None,
            )

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

            # Use the client's built-in method to create/derive credentials.
            # NOTE: POLYMARKET_RELAYER_KEY (from the Polymarket UI) is a *relayer*
            # identity key and is distinct from the CLOB L2 trading key that the
            # SDK derives deterministically from the wallet private key.  Never
            # compare the two — they will always differ.
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

        except PolyApiException as e:
            error_text = str(e).lower()
            if e.status_code == 400 and "maker address not allowed" in error_text:
                raise RuntimeError(
                    "Polymarket rejected this maker address for trading. "
                    "Use a MetaMask-linked Polymarket trading wallet."
                ) from e
            if self._is_signer_api_key_mismatch_error(e):
                logger.warning(
                    "Order signer/API-key mismatch detected; re-deriving API credentials and retrying once."
                )
                self._refresh_api_credentials(
                    reason=(
                        "Order signer mismatch detected — configured API key belongs to a "
                        "different wallet and was replaced automatically"
                    )
                )
                order_response = self.client.create_and_post_order(
                    order_args, order_type=order_type_enum
                )
                logger.info(
                    "Order posted after signer/API-key credential refresh: %s %s @ %s (token: %s, order_id: %s)",
                    side,
                    size,
                    price,
                    token_id,
                    order_response.get("orderID") if isinstance(order_response, dict) else None,
                )
                return order_response
            logger.error(f"Failed to post order: {e}")
            raise
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

    async def post_market_order(
        self,
        token_id: str,
        amount: float,
        side: str,
    ) -> Dict[str, Any]:
        """
        Post a true market order (FOK) using the SDK's MarketOrderArgsV2 pattern.

        Unlike limit orders, market orders specify an *amount* of USDC to spend (BUY)
        or the number of shares to sell (SELL), not a price+size pair.  The CLOB fills
        at the best available price(s) and cancels any unfilled portion immediately (FOK).

        Args:
            token_id: CLOB token ID to trade.
            amount: USDC amount to spend (BUY) or number of shares to sell (SELL).
            side: ``"BUY"`` or ``"SELL"``.

        Returns:
            Order response dictionary from the CLOB.

        Raises:
            RuntimeError: If L2 credentials are not available.
        """
        if not self.api_creds:
            raise RuntimeError(
                "L2 API credentials required for posting orders. "
                "Call create_api_credentials() first."
            )

        try:
            order_args = MarketOrderArgsV2(
                token_id=token_id,
                amount=amount,
                side=side.upper(),
            )
            order_response = self.client.create_and_post_market_order(order_args)

            logger.info(
                "Market order posted: %s $%.4f (token: %s, order_id: %s)",
                side.upper(),
                amount,
                token_id,
                order_response.get("orderID") if isinstance(order_response, dict) else None,
            )
            return order_response

        except PolyApiException as e:
            error_text = str(e).lower()
            if e.status_code == 400 and "maker address not allowed" in error_text:
                raise RuntimeError(
                    "Polymarket rejected this maker address for trading. "
                    "Use a MetaMask-linked Polymarket trading wallet."
                ) from e
            if self._is_signer_api_key_mismatch_error(e):
                logger.warning(
                    "Market-order signer/API-key mismatch detected; re-deriving API credentials and retrying once."
                )
                self._refresh_api_credentials(
                    reason=(
                        "Order signer mismatch detected — configured API key belongs to a "
                        "different wallet and was replaced automatically"
                    )
                )
                order_response = self.client.create_and_post_market_order(order_args)
                logger.info(
                    "Market order posted after signer/API-key credential refresh: %s $%.4f (token: %s, order_id: %s)",
                    side.upper(),
                    amount,
                    token_id,
                    order_response.get("orderID") if isinstance(order_response, dict) else None,
                )
                return order_response
            logger.error("Failed to post market order: %s", e)
            raise
        except Exception as e:
            logger.error("Failed to post market order: %s", e)
            raise

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """
        Fetch a single order by ID.

        Args:
            order_id: The CLOB order ID to look up.

        Returns:
            Order details dictionary.

        Raises:
            RuntimeError: If L2 credentials are not available.
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required")

        try:
            order = self.client.get_order(order_id)
            return order
        except Exception as e:
            logger.error("Failed to fetch order %s: %s", order_id, e)
            raise

    async def get_trades(
        self,
        market: Optional[str] = None,
        asset_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch the authenticated user's trade history from the CLOB.

        Args:
            market: Optional market condition ID filter.
            asset_id: Optional token ID filter.

        Returns:
            List of trade dictionaries.

        Raises:
            RuntimeError: If L2 credentials are not available.
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required")

        try:
            params = TradeParams(market=market, asset_id=asset_id)
            trades = self.client.get_trades(params)
            return trades if isinstance(trades, list) else []
        except Exception as e:
            logger.error("Failed to fetch trades: %s", e)
            raise

    async def cancel_market_orders_by_params(
        self,
        market: Optional[str] = None,
        asset_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Cancel all open orders for a market or asset in a single API call.

        This wraps the SDK's ``cancel_market_orders(OrderMarketCancelParams)`` endpoint,
        which is more efficient than iterating over open orders and cancelling individually.

        Args:
            market: Market condition ID — cancels all orders in this market.
            asset_id: Token ID — cancels all orders for this specific token.

        Returns:
            Cancellation response from the CLOB.

        Raises:
            RuntimeError: If L2 credentials are not available.
            ValueError: If neither ``market`` nor ``asset_id`` is provided.
        """
        if not self.api_creds:
            raise RuntimeError("L2 API credentials required")

        if not market and not asset_id:
            raise ValueError("Either market or asset_id must be provided")

        try:
            payload = OrderMarketCancelParams(market=market, asset_id=asset_id)
            response = self.client.cancel_market_orders(payload)
            logger.info("Market orders cancelled: market=%s asset_id=%s", market, asset_id)
            return response
        except Exception as e:
            logger.error(
                "Failed to cancel market orders (market=%s, asset_id=%s): %s",
                market,
                asset_id,
                e,
            )
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
                    positions = get_positions_fn(self.funder_address)
                except TypeError:
                    positions = get_positions_fn()
                if isinstance(positions, list):
                    return positions

            # SDK >=0.28 no longer exposes get_positions(); fallback to Data API.
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    "https://data-api.polymarket.com/positions",
                    params={"user": self.funder_address},
                )
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, list) else []

        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            raise

    @staticmethod
    def _coerce_numeric(value: Any) -> Optional[float]:
        """Parse numeric-like values (including currency-formatted strings)."""
        def _scale_if_atomic(parsed: float, raw_token: Optional[str] = None) -> float:
            token = (raw_token or "").strip().lower()
            is_integer_like = parsed.is_integer() and "." not in token and "e" not in token
            if is_integer_like and abs(parsed) >= _USDC_SCALE:
                return parsed / _USDC_SCALE
            return parsed

        if value is None:
            return None
        if isinstance(value, (int, float)):
            return _scale_if_atomic(float(value))
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            # Extract the first signed decimal number from values like
            # "$1.93", "€0.93", and "0.93 USDC".
            # Lookarounds avoid partial matches inside malformed tokens.
            match = re.search(r"(?<![0-9.])[+-]?(?:\d+\.\d+|\d+|\.\d+)(?![0-9.])", cleaned)
            if not match:
                return None
            try:
                parsed = float(match.group(0))
                return _scale_if_atomic(parsed, match.group(0))
            except ValueError:
                return None
        return None

    @classmethod
    def _extract_numeric_balance(cls, balance_data: Dict[str, Any]) -> float:
        """Extract spendable USDC balance from known balance/allowance payload shapes."""
        # Prefer available/spendable fields over total balance to align with
        # CLOB `balance-allowance` semantics (usable cash vs. locked funds).
        available_candidates = [
            balance_data.get("available"),
            balance_data.get("available_balance"),
        ]
        total_candidates = [
            balance_data.get("balance"),
            balance_data.get("amount"),
            balance_data.get("value"),
        ]

        for nested_key in ("collateral", "usdc", "data", "balance_allowance"):
            nested = balance_data.get(nested_key)
            if isinstance(nested, dict):
                available_candidates.extend(
                    [
                        nested.get("available"),
                        nested.get("available_balance"),
                    ]
                )
                total_candidates.extend(
                    [
                        nested.get("balance"),
                        nested.get("amount"),
                        nested.get("value"),
                    ]
                )

        for value in chain(available_candidates, total_candidates):
            parsed = cls._coerce_numeric(value)
            if parsed is not None:
                return parsed

        return 0.0

    @classmethod
    def _normalize_balance_payload(cls, balance_data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize balance payload to include canonical spendable `balance` in USDC."""
        normalized = dict(balance_data)
        available_numeric = cls._coerce_numeric(normalized.get("available"))
        if available_numeric is None:
            available_numeric = cls._coerce_numeric(normalized.get("available_balance"))
        if available_numeric is not None:
            normalized["available"] = str(available_numeric)
            normalized["available_balance"] = str(available_numeric)
        # Keep compatibility with existing callers/tests that consume string balances.
        normalized["balance"] = (
            str(available_numeric)
            if available_numeric is not None
            else str(cls._extract_numeric_balance(normalized))
        )
        normalized.setdefault("currency", "USDC")
        return normalized

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
            logger.debug(
                "Running get_balance() with verified=%s creds_from_config=%s funder=%s",
                self._api_credentials_verified,
                self._api_creds_from_config,
                self.funder_address,
            )
            balance_data = self._fetch_balance_once()
            return self._handle_zero_balance_refresh(balance_data)
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
            logger.debug("Balance fetch path: client.get_balance")
            try:
                balance_data = get_balance_fn(self.funder_address)
            except TypeError:
                # Some SDK variants may expose a no-arg get_balance()
                balance_data = get_balance_fn()

            if isinstance(balance_data, dict):
                normalized = self._normalize_balance_payload(balance_data)
                logger.debug(
                    "Balance fetched via get_balance: keys=%s extracted=%s",
                    sorted(normalized.keys()),
                    self._extract_numeric_balance(normalized),
                )
                return normalized
            return {"balance": str(balance_data)}

        # SDK >=0.28 exposes get_balance_allowance() instead.
        get_balance_allowance_fn = getattr(self.client, "get_balance_allowance", None)
        if callable(get_balance_allowance_fn):
            logger.debug("Balance fetch path: client.get_balance_allowance")
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            balance_data = get_balance_allowance_fn(params)
            if isinstance(balance_data, dict):
                normalized = self._normalize_balance_payload(balance_data)
                logger.debug(
                    "Balance fetched via get_balance_allowance: keys=%s extracted=%s",
                    sorted(normalized.keys()),
                    self._extract_numeric_balance(normalized),
                )
                return normalized
            return {"balance": str(balance_data)}

        raise AttributeError("ClobClient does not expose a supported balance method")

    def has_api_credentials(self) -> bool:
        """Check if L2 API credentials are available"""
        return self.api_creds is not None

    def has_verified_api_credentials(self) -> bool:
        """Check if L2 credentials are present and were successfully verified."""
        return self.api_creds is not None and self._api_credentials_verified

    @staticmethod
    def _probe_looks_valid(balance_payload: Any) -> bool:
        """Return True when a balance probe contains recognizable balance fields."""
        if not isinstance(balance_payload, dict):
            return False
        direct_keys = {"balance", "available", "available_balance", "amount", "value"}
        if any(balance_payload.get(key) is not None for key in direct_keys):
            return True
        for nested_key in ("collateral", "usdc", "data", "balance_allowance"):
            nested = balance_payload.get(nested_key)
            if isinstance(nested, dict) and any(nested.get(key) is not None for key in direct_keys):
                return True
        return False

    @staticmethod
    def _is_signer_api_key_mismatch_error(exc: PolyApiException) -> bool:
        """Return True when CLOB reports API key wallet does not match order signer."""
        if exc.status_code != 400:
            return False
        error_text = str(exc).lower()
        return "order signer address has to be the address of the api key" in error_text

    def _handle_zero_balance_refresh(self, balance_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle proxy-wallet migration: refresh configured creds once if balance probes as 0.

        `balance_data` can be any normalized CLOB balance payload shape accepted
        by `_extract_numeric_balance()` (top-level or nested available/balance fields).

        Refresh is only attempted when credentials were loaded from static configuration
        (``_api_creds_from_config=True``).  Auto-derived credentials are never refreshed
        here — a zero balance simply means the wallet has no USDC.
        """
        if (
            self._api_creds_from_config
            and not self._zero_balance_proxy_refresh_attempted
            and self._extract_numeric_balance(balance_data) == 0.0
            and callable(getattr(self.client, "create_or_derive_api_key", None))
            and callable(getattr(self.client, "set_api_creds", None))
        ):
            self._zero_balance_proxy_refresh_attempted = True
            logger.warning(
                "Balance probe returned 0 with configured API credentials; "
                "re-deriving credentials for proxy-wallet mode and retrying once."
            )
            self._refresh_api_credentials(
                reason="Zero balance detected — re-deriving proxy-wallet API credentials"
            )
            refreshed = self._fetch_balance_once()
            if self._extract_numeric_balance(refreshed) > 0.0:
                logger.info("Proxy-wallet credential refresh recovered a non-zero balance.")
            else:
                logger.warning(
                    "Proxy-wallet credential refresh still returned 0 balance; "
                    "wallet may truly have no spendable USDC."
                )
            return refreshed

        return balance_data

    def _reconcile_configured_api_key_with_signer(self) -> None:
        """Replace configured credentials when they don't match signer-derived API key."""
        if (
            not self._allow_credential_derivation
            or not self.api_creds
            or not self._api_creds_from_config
        ):
            return

        derive_fn = getattr(self.client, "create_or_derive_api_key", None)
        if not callable(derive_fn):
            return

        try:
            derived = derive_fn()
        except Exception as exc:
            logger.warning(
                "Could not reconcile configured API key with signer-derived credentials: %s",
                exc,
            )
            logger.debug("API-key reconciliation exception details", exc_info=True)
            return

        derived_api_key = getattr(derived, "api_key", None)
        if not derived_api_key:
            logger.warning(
                "Signer-derived API credential probe returned no api_key; keeping configured credentials."
            )
            return

        if derived_api_key == self.api_creds.api_key:
            logger.debug("Configured API key already matches signer-derived wallet credentials.")
            return

        logger.warning(
            "Configured API key does not belong to signer %s; replacing with signer-derived credentials.",
            self.address,
        )
        self.api_creds = ApiCreds(
            api_key=derived.api_key,
            api_secret=derived.api_secret,
            api_passphrase=derived.api_passphrase,
        )
        self._api_creds_from_config = False
        self._api_credentials_verified = False

        set_api_creds_fn = getattr(self.client, "set_api_creds", None)
        if callable(set_api_creds_fn):
            set_api_creds_fn(self.api_creds)
        else:
            self._initialize_client()

        _log_new_credentials(
            api_key=self.api_creds.api_key,
            api_secret=self.api_creds.api_secret,
            passphrase=self.api_creds.api_passphrase,
            reason=(
                "Configured API key belonged to a different wallet — replaced "
                "with signer-derived credentials"
            ),
        )

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
            if not self._allow_credential_derivation:
                raise RuntimeError(
                    "L2 bootstrap requires POLYMARKET_RELAYER_SECRET and "
                    "POLYMARKET_RELAYER_PASSPHRASE when derivation is disabled."
                )
            logger.info("No API credentials found. Attempting to create...")
            await self.create_api_credentials()
            post_create_probe = self._handle_zero_balance_refresh(self._fetch_balance_once())
            self._api_credentials_verified = self._probe_looks_valid(post_create_probe)
            logger.debug(
                "Post-create credential probe result: looks_valid=%s extracted_balance=%s verified=%s",
                self._probe_looks_valid(post_create_probe),
                self._extract_numeric_balance(post_create_probe),
                self._api_credentials_verified,
            )
            if not self._api_credentials_verified:
                logger.warning(
                    "Post-create credential probe returned unexpected payload; keeping unverified."
                )
            logger.info("API credentials created successfully!")
            return

        logger.info("Testing existing API credentials...")
        try:
            logger.debug(
                "Credential verification start: signer=%s funder=%s signature_type=%s has_l2=%s",
                self.address,
                self.funder_address,
                self.signature_type,
                self.api_creds is not None,
            )
            balance_probe = self._fetch_balance_once()
            verified_probe = self._handle_zero_balance_refresh(balance_probe)
            logger.debug(
                "Credential verification probe result: looks_valid=%s extracted_balance=%s verified=%s",
                self._probe_looks_valid(verified_probe),
                self._extract_numeric_balance(verified_probe),
                self._api_credentials_verified,
            )
            self._api_credentials_verified = self._probe_looks_valid(verified_probe)
            if not self._api_credentials_verified:
                logger.warning("Credential probe returned unexpected payload; keeping unverified.")
                return
            if self._extract_numeric_balance(verified_probe) > 0.0:
                logger.info("API credentials verified successfully.")
            else:
                logger.warning(
                    "API credentials were accepted, but spendable USDC balance is 0 for funder %s. "
                    "If your Polymarket UI shows funds, set POLYMARKET_FUNDER to your actual "
                    "deposit wallet address and keep POLYMARKET_SIGNATURE_TYPE=3 "
                    "(MetaMask deposit-wallet flow).",
                    self.funder_address,
                )
        except PolyApiException as e:
            self._api_credentials_verified = False
            if e.status_code == 401:
                if not self._allow_credential_derivation:
                    logger.warning(
                        "Static relayer credentials were rejected (HTTP 401). "
                        "Automatic derivation is disabled for static mode."
                    )
                else:
                    logger.warning(
                        "API credentials rejected (HTTP 401) — refreshing credentials..."
                    )
                    self._refresh_api_credentials()
                    post_refresh_probe = self._handle_zero_balance_refresh(
                        self._fetch_balance_once()
                    )
                    self._api_credentials_verified = self._probe_looks_valid(post_refresh_probe)
                    logger.debug(
                        "Post-refresh credential probe result: looks_valid=%s extracted_balance=%s verified=%s",
                        self._probe_looks_valid(post_refresh_probe),
                        self._extract_numeric_balance(post_refresh_probe),
                        self._api_credentials_verified,
                    )
                    if not self._api_credentials_verified:
                        logger.warning(
                            "Post-refresh credential probe returned unexpected payload; keeping unverified."
                        )
                        return
                    if self._extract_numeric_balance(post_refresh_probe) > 0.0:
                        logger.info("API credentials refreshed and verified successfully.")
                    else:
                        logger.warning(
                            "API credentials refreshed successfully, but spendable USDC is still 0 for funder %s.",
                            self.funder_address,
                        )
            else:
                logger.warning(
                    "API credential probe returned an unexpected error (%s). "
                    "Credentials may still be valid; continuing startup.",
                    e,
                )
                logger.debug("Unexpected API credential probe exception details", exc_info=True)
        except Exception as e:
            self._api_credentials_verified = False
            logger.warning(
                "Could not verify API credentials at startup (%s). "
                "Credentials may still be valid; continuing startup.",
                e,
            )
            logger.debug("Credential verification exception details", exc_info=True)

    def _refresh_api_credentials(
        self,
        reason: str = "HTTP 401 received — existing API credentials were stale or invalid",
    ) -> bool:
        """
        Re-derive L2 API credentials from the wallet private key and update the client.

        Called automatically when an authenticated request returns HTTP 401 to recover
        from stale or expired API keys without requiring a server restart.

        Args:
            reason: Human-readable description of why the refresh was triggered, used in
                the "NEW CREDENTIALS GENERATED" banner.  Only shown when the derived key
                differs from the previously-configured key.

        Returns:
            True if the derived credentials differ from the previous ones (i.e. a
            genuinely new key was issued), False if the same key was returned
            deterministically (meaning the existing credentials were already correct).

        Raises:
            Exception: If credential derivation fails.
        """
        logger.info("Refreshing API credentials via create_or_derive_api_key()...")
        old_api_key = self.api_creds.api_key if self.api_creds else None
        new_creds = self.client.create_or_derive_api_key()
        self.api_creds = ApiCreds(
            api_key=new_creds.api_key,
            api_secret=new_creds.api_secret,
            api_passphrase=new_creds.api_passphrase,
        )
        self._api_credentials_verified = False
        # After refresh, treat credentials as wallet-derived/runtime-managed.
        # `_zero_balance_proxy_refresh_attempted` remains the one-shot guard
        # that prevents repeated refresh loops on truly zero-balance wallets.
        self._api_creds_from_config = False
        # Push updated creds into the live ClobClient instance so subsequent
        # calls use the new key without a full re-initialization.
        self.client.set_api_creds(self.api_creds)

        credentials_changed = old_api_key is None or new_creds.api_key != old_api_key
        logger.debug(
            "Credential refresh result: changed=%s old_key_set=%s new_key_set=%s",
            credentials_changed,
            old_api_key is not None,
            bool(new_creds.api_key),
        )
        if credentials_changed:
            # Only emit the noisy banner when the key genuinely changed so that
            # wallets with zero USDC balance don't spam "NEW CREDENTIALS GENERATED"
            # on every container restart (the SDK deterministically re-derives the
            # same key, so nothing actually changed).
            _log_new_credentials(
                api_key=self.api_creds.api_key,
                api_secret=self.api_creds.api_secret,
                passphrase=self.api_creds.api_passphrase,
                reason=reason,
            )
        else:
            logger.info(
                "Credential re-derivation returned the same API key — "
                "existing credentials are already correct, no update needed."
            )
        return credentials_changed

    def get_address(self) -> str:
        """Get signer wallet address (EOA)."""
        return self.address

    def get_funder_address(self) -> str:
        """Get wallet address that actually holds funds/positions in Polymarket."""
        return self.funder_address

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
    signature_type: int = 3,
    funder: Optional[str] = None,
    host: str = "https://clob.polymarket.com",
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
        signature_type: Polymarket wallet signature type
        funder: Funding / deposit wallet address
        host: CLOB API host URL

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
        host=host,
    )
