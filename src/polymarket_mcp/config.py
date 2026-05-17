"""
Configuration management for Polymarket MCP server.
Loads and validates environment variables.
"""

import logging
from typing import Optional
from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class PolymarketConfig(BaseSettings):
    """
    Configuration settings for Polymarket MCP server.
    Loads from environment variables with validation.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=True, extra="ignore"
    )

    _polymarket_ready: bool = PrivateAttr(default=False)
    _polymarket_config_error: Optional[str] = PrivateAttr(default=None)

    # DEMO MODE - Run without real credentials (read-only)
    DEMO_MODE: bool = Field(
        default=False, description="Run in demo mode without real wallet (read-only, no trading)"
    )

    # Required Polygon Wallet Configuration (optional in DEMO_MODE)
    POLYGON_PRIVATE_KEY: str = Field(
        default="", description="Polygon wallet private key (without 0x prefix)"
    )
    POLYGON_ADDRESS: str = Field(default="", description="Polygon wallet address")
    POLYMARKET_CHAIN_ID: Optional[int] = Field(
        default=None, description="Polygon chain ID for mainnet/prod"
    )
    POLYMARKET_TEST_CHAIN_ID: Optional[int] = Field(
        default=None, description="Polygon chain ID for testnet"
    )
    POLYMARKET_ENV: Optional[str] = Field(
        default=None,
        description="Polymarket environment selection: mainnet (or prod alias) or testnet",
    )

    # L2 API Credentials
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
    POLYMARKET_RELAYER_KEY: Optional[str] = Field(
        default=None, description="Relayer/CLOB API key exported from Polymarket UI"
    )
    POLYMARKET_RELAYER_SECRET: Optional[str] = Field(
        default=None, description="Relayer/CLOB API secret exported from Polymarket UI"
    )
    POLYMARKET_RELAYER_PASSPHRASE: Optional[str] = Field(
        default=None, description="Relayer/CLOB API passphrase exported from Polymarket UI"
    )
    POLYMARKET_SIGNATURE_TYPE: int = Field(
        default=3,
        description=(
            "Wallet signature type for CLOB auth. Supported values: "
            "0=EOA direct mode, 3=POLY_1271/deposit wallet (MetaMask deposit flow)."
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
    CLOB_API_URL: Optional[str] = Field(
        default=None, description="Polymarket CLOB API endpoint for mainnet/prod"
    )
    GAMMA_API_URL: Optional[str] = Field(
        default=None, description="Gamma API endpoint for market data on mainnet/prod"
    )
    CLOB_API_TEST_URL: Optional[str] = Field(
        default=None, description="Polymarket CLOB API endpoint for testnet"
    )
    GAMMA_API_TEST_URL: Optional[str] = Field(
        default=None, description="Gamma API endpoint for market data on testnet"
    )
    POLYMARKET_GEOBLOCK_URL: Optional[str] = Field(
        default=None,
        description="Required Polymarket geoblock endpoint on polymarket.com",
    )

    # WebSocket Controls
    WS_ENABLED: bool = Field(
        default=True, description="Enable WebSocket realtime features and background connections"
    )

    # Logging
    LOG_LEVEL: str = Field(default="INFO", description="Log level: DEBUG, INFO, WARNING, ERROR")

    # Polymarket Constants — no defaults; must be supplied via environment variables
    USDC_ADDRESS: Optional[str] = Field(
        default=None,
        description="USDC token address on Polygon (set via USDC_ADDRESS env var)",
    )
    CTF_EXCHANGE_ADDRESS: Optional[str] = Field(
        default=None,
        description="CTF Exchange contract address (set via CTF_EXCHANGE_ADDRESS env var)",
    )
    CONDITIONAL_TOKEN_ADDRESS: Optional[str] = Field(
        default=None,
        description="Conditional Token contract address (set via CONDITIONAL_TOKEN_ADDRESS env var)",
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

    @field_validator("POLYMARKET_ENV", mode="before")
    @classmethod
    def normalize_polymarket_env(cls, v: Optional[str]) -> Optional[str]:
        """Normalize empty POLYMARKET_ENV values."""
        if v is None:
            return None
        normalized = str(v).strip()
        return normalized or None

    @field_validator("POLYMARKET_ENV")
    @classmethod
    def validate_polymarket_env(cls, v: Optional[str]) -> Optional[str]:
        """Validate Polymarket environment selection."""
        if v is None:
            return None
        normalized = v.strip().lower()
        if normalized == "prod":
            return "mainnet"
        if normalized not in {"mainnet", "testnet"}:
            raise ValueError("POLYMARKET_ENV must be one of 'mainnet', 'prod', or 'testnet'")
        return normalized

    @field_validator("POLYMARKET_CHAIN_ID", "POLYMARKET_TEST_CHAIN_ID", mode="before")
    @classmethod
    def normalize_optional_chain_id(cls, v: object) -> Optional[int]:
        """Treat empty chain IDs as missing."""
        if v in (None, ""):
            return None
        return v

    @field_validator(
        "CLOB_API_URL",
        "GAMMA_API_URL",
        "CLOB_API_TEST_URL",
        "GAMMA_API_TEST_URL",
        "POLYMARKET_GEOBLOCK_URL",
        mode="before",
    )
    @classmethod
    def normalize_optional_url(cls, v: Optional[str]) -> Optional[str]:
        """Treat empty URLs as missing and normalize trailing slashes."""
        if v is None:
            return None
        normalized = str(v).strip()
        if not normalized:
            return None
        return normalized.rstrip("/")

    @field_validator(
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_PASSPHRASE",
        "POLYMARKET_API_KEY_NAME",
        "POLYMARKET_RELAYER_KEY",
        "POLYMARKET_RELAYER_SECRET",
        "POLYMARKET_RELAYER_PASSPHRASE",
        mode="before",
    )
    @classmethod
    def normalize_optional_credential(cls, v: Optional[str]) -> Optional[str]:
        """Treat empty credential strings as missing."""
        if v is None:
            return None
        normalized = str(v).strip()
        return normalized or None

    @field_validator("POLYMARKET_SIGNATURE_TYPE")
    @classmethod
    def validate_signature_type(cls, v: int) -> int:
        """Validate supported Polymarket wallet signature type."""
        if v not in {0, 3}:
            raise ValueError(
                "POLYMARKET_SIGNATURE_TYPE must be 0 (EOA direct mode) or 3 "
                "(POLY_1271/deposit wallet)."
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
    def resolve_polymarket_environment(self):
        """Resolve active Polymarket runtime settings from the selected environment."""
        self._polymarket_ready = False
        self._polymarket_config_error = None

        if not self.POLYMARKET_ENV:
            self.POLYMARKET_CHAIN_ID = None
            self.CLOB_API_URL = None
            self.GAMMA_API_URL = None
            self._polymarket_config_error = "POLYMARKET_ENV is not set"
            return self

        if self.POLYMARKET_ENV == "testnet":
            missing = [
                name
                for name, value in (
                    ("POLYMARKET_TEST_CHAIN_ID", self.POLYMARKET_TEST_CHAIN_ID),
                    ("CLOB_API_TEST_URL", self.CLOB_API_TEST_URL),
                    ("GAMMA_API_TEST_URL", self.GAMMA_API_TEST_URL),
                )
                if value in (None, "")
            ]
            if missing:
                self.POLYMARKET_CHAIN_ID = None
                self.CLOB_API_URL = None
                self.GAMMA_API_URL = None
                self._polymarket_config_error = (
                    "POLYMARKET_ENV=testnet but missing required variables: " + ", ".join(missing)
                )
                return self

            self.POLYMARKET_CHAIN_ID = self.POLYMARKET_TEST_CHAIN_ID
            self.CLOB_API_URL = self.CLOB_API_TEST_URL
            self.GAMMA_API_URL = self.GAMMA_API_TEST_URL
            self._polymarket_ready = True
            return self

        missing = [
            name
            for name, value in (
                ("POLYMARKET_CHAIN_ID", self.POLYMARKET_CHAIN_ID),
                ("CLOB_API_URL", self.CLOB_API_URL),
                ("GAMMA_API_URL", self.GAMMA_API_URL),
            )
            if value in (None, "")
        ]
        if missing:
            self.POLYMARKET_CHAIN_ID = None
            self.CLOB_API_URL = None
            self.GAMMA_API_URL = None
            self._polymarket_config_error = (
                "POLYMARKET_ENV=mainnet but missing required variables: " + ", ".join(missing)
            )
            return self

        self._polymarket_ready = True
        return self

    @model_validator(mode="after")
    def validate_l2_bootstrap_requirements(self):
        """Validate L2 bootstrap requirements for non-demo runtime.

        When ``POLYMARKET_RELAYER_KEY`` (or its legacy alias ``POLYMARKET_API_KEY``)
        is set the server starts in *static* or *derived* mode.  When neither is set
        but ``POLYGON_PRIVATE_KEY`` is present the server starts in *auto_derive*
        mode and calls ``create_or_derive_api_key()`` at runtime to obtain L2
        credentials (the official Polymarket Python SDK flow).
        """
        if self.DEMO_MODE or not self._polymarket_ready:
            return self
        if not self.effective_api_key and not self.POLYGON_PRIVATE_KEY:
            raise ValueError(
                "L2 auth bootstrap requires either POLYMARKET_RELAYER_KEY "
                "(or legacy POLYMARKET_API_KEY) or POLYGON_PRIVATE_KEY. "
                "Set DEMO_MODE=true for read-only access without credentials."
            )
        return self

    def has_api_credentials(self) -> bool:
        """Check if L2 CLOB/relayer credentials are configured."""
        return all(
            [
                self.effective_api_key,
                self.effective_api_secret,
                self.effective_api_passphrase,
            ]
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
        if data.get("POLYMARKET_RELAYER_KEY"):
            data["POLYMARKET_RELAYER_KEY"] = "***HIDDEN***"
        if data.get("POLYMARKET_RELAYER_SECRET"):
            data["POLYMARKET_RELAYER_SECRET"] = "***HIDDEN***"
        if data.get("POLYMARKET_RELAYER_PASSPHRASE"):
            data["POLYMARKET_RELAYER_PASSPHRASE"] = "***HIDDEN***"
        return data

    @property
    def effective_funder(self) -> str:
        """Wallet address that actually funds positions/orders in Polymarket."""
        return (self.POLYMARKET_FUNDER or self.POLYGON_ADDRESS).lower()

    @property
    def effective_api_key(self) -> Optional[str]:
        """Resolved CLOB/relayer API key used for authenticated L2 calls."""
        return (
            self.POLYMARKET_RELAYER_KEY
            if self.POLYMARKET_RELAYER_KEY is not None
            else self.POLYMARKET_API_KEY
        )

    @property
    def effective_api_secret(self) -> Optional[str]:
        """Resolved CLOB/relayer API secret used for HMAC signing."""
        if self.POLYMARKET_RELAYER_KEY is not None:
            return self.POLYMARKET_RELAYER_SECRET
        return self.POLYMARKET_API_SECRET

    @property
    def effective_api_passphrase(self) -> Optional[str]:
        """Resolved CLOB/relayer API passphrase."""
        if self.POLYMARKET_RELAYER_KEY is not None:
            return self.POLYMARKET_RELAYER_PASSPHRASE
        return self.POLYMARKET_PASSPHRASE

    @property
    def l2_auth_mode(self) -> str:
        """Resolved L2 auth bootstrap mode.

        Returns one of:
        - ``"static"``      – key + secret + passphrase all configured; no derivation needed.
        - ``"derived"``     – key configured but secret/passphrase absent; secret/passphrase
                              will be derived from the private key via the SDK.
        - ``"auto_derive"`` – neither key nor secret/passphrase configured; all three L2
                              credentials will be derived from POLYGON_PRIVATE_KEY at startup
                              (official Polymarket Python SDK flow).
        - ``"missing"``     – no private key and no API key; server cannot authenticate.
        """
        if not self.effective_api_key:
            if self.POLYGON_PRIVATE_KEY:
                return "auto_derive"
            return "missing"
        if self.has_api_credentials():
            return "static"
        return "derived"

    @property
    def polymarket_ready(self) -> bool:
        """Return True when the selected Polymarket environment is fully configured."""
        return self._polymarket_ready

    @property
    def polymarket_config_error(self) -> Optional[str]:
        """Reason why Polymarket runtime settings were not activated."""
        return self._polymarket_config_error


def get_polymarket_runtime_state(config: object) -> tuple[bool, Optional[str]]:
    """Return runtime readiness for a config-like object.

    Args:
        config: Config instance or mock exposing Polymarket readiness/error attributes.

    Returns:
        Tuple `(polymarket_ready, polymarket_config_error)` where the first value indicates
        whether the selected Polymarket environment is fully configured and the second value
        contains the reason initialization was skipped when it is not.
    """
    polymarket_ready_attr = getattr(config, "polymarket_ready", None)
    if isinstance(polymarket_ready_attr, bool):
        polymarket_ready = polymarket_ready_attr
    else:
        polymarket_ready = all(
            (
                getattr(config, "POLYMARKET_ENV", None),
                getattr(config, "POLYMARKET_CHAIN_ID", None),
                getattr(config, "CLOB_API_URL", None),
                getattr(config, "GAMMA_API_URL", None),
            )
        )

    polymarket_config_error = getattr(config, "polymarket_config_error", None)
    if not isinstance(polymarket_config_error, str):
        polymarket_config_error = None

    return polymarket_ready, polymarket_config_error


def load_config() -> PolymarketConfig:
    """
    Load configuration from environment variables.

    Returns:
        PolymarketConfig: Validated configuration object

    Raises:
        ValueError: If required variables are missing or invalid
    """
    return PolymarketConfig()
