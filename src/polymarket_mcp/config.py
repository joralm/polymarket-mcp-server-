"""
Configuration management for Polymarket MCP server.
Loads and validates environment variables with proper defaults.
"""

from typing import Optional, ClassVar
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class PolymarketConfig(BaseSettings):
    """
    Configuration settings for Polymarket MCP server.
    Loads from environment variables with validation.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=True, extra="ignore"
    )

    MAINNET_CHAIN_ID: ClassVar[int] = 137
    TESTNET_CHAIN_ID: ClassVar[int] = 80002
    MAINNET_CLOB_URL: ClassVar[str] = "https://clob.polymarket.com"
    TESTNET_CLOB_URL: ClassVar[str] = "https://clob.polymarket.com"
    MAINNET_GAMMA_URL: ClassVar[str] = "https://gamma-api.polymarket.com"
    TESTNET_GAMMA_URL: ClassVar[str] = "https://gamma-api.polymarket.com"

    # DEMO MODE - Run without real credentials (read-only)
    DEMO_MODE: bool = Field(
        default=False, description="Run in demo mode without real wallet (read-only, no trading)"
    )

    # Required Polygon Wallet Configuration (optional in DEMO_MODE)
    POLYGON_PRIVATE_KEY: str = Field(
        default="", description="Polygon wallet private key (without 0x prefix)"
    )
    POLYGON_ADDRESS: str = Field(default="", description="Polygon wallet address")
    POLYMARKET_CHAIN_ID: int = Field(
        default=137, description="Polygon chain ID (137 for mainnet, 80002 for Amoy testnet)"
    )
    POLYMARKET_ENV: str = Field(
        default="mainnet",
        description="Polymarket environment selection: mainnet or testnet",
    )

    # Optional L2 API Credentials (auto-created if not provided)
    POLYMARKET_API_KEY: Optional[str] = Field(
        default=None, description="L2 API key for authenticated requests"
    )
    POLYMARKET_PASSPHRASE: Optional[str] = Field(default=None, description="API key passphrase")
    POLYMARKET_API_SECRET: Optional[str] = Field(
        default=None, description="L2 API secret for authenticated requests"
    )
    POLYMARKET_API_KEY_NAME: Optional[str] = Field(
        default=None, description="API key name/identifier"
    )
    POLYMARKET_SIGNATURE_TYPE: int = Field(
        default=3,
        description=(
            "Wallet signature type for CLOB auth. This server supports only "
            "3=POLY_1271/deposit wallet (MetaMask deposit flow)."
        ),
    )
    POLYMARKET_FUNDER: Optional[str] = Field(
        default=None,
        description="Funding wallet / deposit wallet address used by Polymarket UI and settlement",
    )

    # Safety Limits - Risk Management
    MAX_ORDER_SIZE_USD: float = Field(
        default=1000.0, description="Maximum size for a single order in USD"
    )
    MAX_TOTAL_EXPOSURE_USD: float = Field(
        default=5000.0, description="Maximum total exposure across all positions in USD"
    )
    MAX_POSITION_SIZE_PER_MARKET: float = Field(
        default=2000.0, description="Maximum position size per market in USD"
    )
    MIN_LIQUIDITY_REQUIRED: float = Field(
        default=10000.0, description="Minimum liquidity required in market before trading (USD)"
    )
    MAX_SPREAD_TOLERANCE: float = Field(
        default=0.05, description="Maximum spread tolerance (0.05 = 5%)"
    )

    # Trading Controls
    ENABLE_AUTONOMOUS_TRADING: bool = Field(
        default=True, description="Enable autonomous trading without confirmation"
    )
    REQUIRE_CONFIRMATION_ABOVE_USD: float = Field(
        default=500.0, description="Require user confirmation for orders above this USD amount"
    )
    AUTO_CANCEL_ON_LARGE_SPREAD: bool = Field(
        default=True,
        description="Automatically cancel orders if spread exceeds MAX_SPREAD_TOLERANCE",
    )

    # API Endpoints
    CLOB_API_URL: str = Field(
        default="https://clob.polymarket.com", description="Polymarket CLOB API endpoint"
    )
    GAMMA_API_URL: str = Field(
        default="https://gamma-api.polymarket.com", description="Gamma API endpoint for market data"
    )

    # WebSocket Controls
    WS_ENABLED: bool = Field(
        default=True, description="Enable WebSocket realtime features and background connections"
    )

    # Logging
    LOG_LEVEL: str = Field(default="INFO", description="Log level: DEBUG, INFO, WARNING, ERROR")

    # Polymarket Constants
    USDC_ADDRESS: str = Field(
        default="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        description="USDC token address on Polygon",
    )
    CTF_EXCHANGE_ADDRESS: str = Field(
        default="0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
        description="CTF Exchange contract address",
    )
    CONDITIONAL_TOKEN_ADDRESS: str = Field(
        default="0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
        description="Conditional Token contract address",
    )

    @field_validator("POLYGON_PRIVATE_KEY")
    @classmethod
    def validate_private_key(cls, v: str, info) -> str:
        """Validate private key format (skipped in DEMO_MODE)"""
        # Get DEMO_MODE from the data being validated
        demo_mode = info.data.get("DEMO_MODE", False)

        # In DEMO mode, wallet credentials are optional and never auto-filled
        if demo_mode:
            if not v:
                return ""
            if v.startswith("0x"):
                v = v[2:]
            if len(v) != 64:
                raise ValueError("POLYGON_PRIVATE_KEY must be 64 hex characters when provided")
            try:
                int(v, 16)
            except ValueError:
                raise ValueError("POLYGON_PRIVATE_KEY must be valid hex")
            return v

        # Normal validation for non-demo mode
        if not v:
            raise ValueError(
                "POLYGON_PRIVATE_KEY is required (or set DEMO_MODE=true for read-only access)"
            )
        # Remove 0x prefix if present
        if v.startswith("0x"):
            v = v[2:]
        # Check if valid hex
        if len(v) != 64:
            raise ValueError("POLYGON_PRIVATE_KEY must be 64 hex characters")
        try:
            int(v, 16)
        except ValueError:
            raise ValueError("POLYGON_PRIVATE_KEY must be valid hex")
        return v

    @field_validator("POLYGON_ADDRESS")
    @classmethod
    def validate_address(cls, v: str, info) -> str:
        """Validate Polygon address format (skipped in DEMO_MODE)"""
        # Get DEMO_MODE from the data being validated
        demo_mode = info.data.get("DEMO_MODE", False)

        # In DEMO mode, wallet address is optional and never auto-filled
        if demo_mode:
            if not v:
                return ""
            if not v.startswith("0x"):
                raise ValueError("POLYGON_ADDRESS must start with 0x")
            if len(v) != 42:
                raise ValueError("POLYGON_ADDRESS must be 42 characters")
            return v.lower()

        # Normal validation for non-demo mode
        if not v:
            raise ValueError(
                "POLYGON_ADDRESS is required (or set DEMO_MODE=true for read-only access)"
            )
        if not v.startswith("0x"):
            raise ValueError("POLYGON_ADDRESS must start with 0x")
        if len(v) != 42:
            raise ValueError("POLYGON_ADDRESS must be 42 characters")
        return v.lower()

    @field_validator("MAX_SPREAD_TOLERANCE")
    @classmethod
    def validate_spread_tolerance(cls, v: float) -> float:
        """Validate spread tolerance is between 0 and 1"""
        if not 0 <= v <= 1:
            raise ValueError("MAX_SPREAD_TOLERANCE must be between 0 and 1")
        return v

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level"""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        v = v.upper()
        if v not in valid_levels:
            raise ValueError(f"LOG_LEVEL must be one of {valid_levels}")
        return v

    @field_validator("POLYMARKET_ENV")
    @classmethod
    def validate_polymarket_env(cls, v: str) -> str:
        """Validate Polymarket environment selection."""
        normalized = v.strip().lower()
        if normalized not in {"mainnet", "testnet"}:
            raise ValueError("POLYMARKET_ENV must be either 'mainnet' or 'testnet'")
        return normalized

    @field_validator("POLYMARKET_SIGNATURE_TYPE")
    @classmethod
    def validate_signature_type(cls, v: int) -> int:
        """Validate supported Polymarket wallet signature type."""
        if v != 3:
            raise ValueError(
                "POLYMARKET_SIGNATURE_TYPE must be 3 (POLY_1271/deposit wallet). "
                "Flow 1 (POLY_PROXY) is no longer supported by this server."
            )
        return v

    @field_validator("POLYMARKET_FUNDER")
    @classmethod
    def validate_funder(cls, v: Optional[str]) -> Optional[str]:
        """Validate optional funder/deposit wallet address."""
        if v in (None, ""):
            return None
        if not v.startswith("0x"):
            raise ValueError("POLYMARKET_FUNDER must start with 0x")
        if len(v) != 42:
            raise ValueError("POLYMARKET_FUNDER must be 42 characters")
        return v.lower()

    @model_validator(mode="after")
    def apply_environment_defaults(self):
        """
        Apply endpoint/chain defaults from POLYMARKET_ENV when not explicitly set.

        Explicit values from environment variables always win over inferred defaults.
        """
        fields_set = self.model_fields_set
        if self.POLYMARKET_ENV == "testnet":
            if "POLYMARKET_CHAIN_ID" not in fields_set:
                self.POLYMARKET_CHAIN_ID = self.TESTNET_CHAIN_ID
            if "CLOB_API_URL" not in fields_set:
                self.CLOB_API_URL = self.TESTNET_CLOB_URL
            if "GAMMA_API_URL" not in fields_set:
                self.GAMMA_API_URL = self.TESTNET_GAMMA_URL
        else:
            if "POLYMARKET_CHAIN_ID" not in fields_set:
                self.POLYMARKET_CHAIN_ID = self.MAINNET_CHAIN_ID
            if "CLOB_API_URL" not in fields_set:
                self.CLOB_API_URL = self.MAINNET_CLOB_URL
            if "GAMMA_API_URL" not in fields_set:
                self.GAMMA_API_URL = self.MAINNET_GAMMA_URL
        return self

    def has_api_credentials(self) -> bool:
        """Check if L2 API credentials are configured"""
        return all(
            [self.POLYMARKET_API_KEY, self.POLYMARKET_PASSPHRASE, self.POLYMARKET_API_KEY_NAME]
        )

    def to_dict(self) -> dict:
        """Convert config to dictionary (hiding sensitive data)"""
        data = self.model_dump()
        # Mask sensitive fields
        if data.get("POLYGON_PRIVATE_KEY"):
            data["POLYGON_PRIVATE_KEY"] = "***HIDDEN***"
        if data.get("POLYMARKET_API_KEY"):
            data["POLYMARKET_API_KEY"] = "***HIDDEN***"
        if data.get("POLYMARKET_API_SECRET"):
            data["POLYMARKET_API_SECRET"] = "***HIDDEN***"
        if data.get("POLYMARKET_PASSPHRASE"):
            data["POLYMARKET_PASSPHRASE"] = "***HIDDEN***"
        return data

    @property
    def effective_funder(self) -> str:
        """Wallet address that actually funds positions/orders in Polymarket."""
        return (self.POLYMARKET_FUNDER or self.POLYGON_ADDRESS).lower()


def load_config() -> PolymarketConfig:
    """
    Load configuration from environment variables.

    Returns:
        PolymarketConfig: Validated configuration object

    Raises:
        ValidationError: If required variables are missing or invalid
    """
    return PolymarketConfig()
