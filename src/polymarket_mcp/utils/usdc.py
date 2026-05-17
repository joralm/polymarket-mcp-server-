"""USDC numeric normalization helpers."""

from typing import Optional, Union

USDC_DECIMALS = 6
USDC_SCALE = 10**USDC_DECIMALS


def scale_usdc_atomic_units(amount: Union[int, float], raw_token: Optional[str] = None) -> float:
    """
    Normalize integer-like USDC atomic units (6 decimals) to display units.

    Examples:
    - 7488975 -> 7.488975
    - "7.49" (already decimal) remains 7.49
    """
    try:
        parsed_amount = float(amount)
    except (TypeError, ValueError):
        return 0.0

    token = (raw_token if raw_token is not None else str(amount)).strip().lower()
    normalized_token = token.lstrip("+-")
    has_exponent = "e" in normalized_token
    has_decimal = "." in normalized_token
    decimal_is_only_trailing_zeroes = has_decimal and normalized_token.rstrip("0").endswith(".")
    is_integer_like = (
        parsed_amount.is_integer()
        and not has_exponent
        and (not has_decimal or decimal_is_only_trailing_zeroes)
    )
    if is_integer_like and abs(parsed_amount) >= USDC_SCALE:
        return parsed_amount / USDC_SCALE
    return parsed_amount
