"""
Polymarket CLOB client with authentication.
Handles L1 (private key) and L2 (API key) authentication.
"""

from typing import Dict, Any, List, Optional
import logging
import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, BalanceAllowanceParams, AssetType

from .signer import OrderSigner

logger = logging.getLogger(__name__)


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
        """
        self.private_key = private_key
        self.address = address.lower()
        self.chain_id = chain_id
        self.host = host

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
            f"(chain_id: {chain_id}, L2 auth: {self.api_creds is not None})"
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

            # Use the client's built-in method to create credentials
            creds = self.client.create_api_key()

            # Store credentials
            self.api_creds = ApiCreds(
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                api_passphrase=creds.api_passphrase,
            )

            # Reinitialize client with new credentials
            self._initialize_client()

            logger.info(f"API credentials created: {creds.api_key[:8]}...")
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
    def _sort_orderbook_levels(levels: List[Dict[str, Any]], descending: bool) -> List[Dict[str, Any]]:
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
            # Build order args
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side.upper(),
                order_type=order_type,
            )

            if expiration:
                order_args.expiration = expiration

            # Post order using client
            order_response = self.client.create_order(order_args)

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
            response = self.client.cancel(order_id)

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
            # Build params
            params = {}
            if market:
                params["market"] = market
            if asset_id:
                params["asset_id"] = asset_id

            orders = self.client.get_orders(**params)
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

        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            raise

    def has_api_credentials(self) -> bool:
        """Check if L2 API credentials are available"""
        return self.api_creds is not None

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
    )
