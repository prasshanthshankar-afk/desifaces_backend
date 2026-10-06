from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any


_PRICING_COUNTRY: ContextVar[str] = ContextVar(
    "desifaces_pricing_country_code",
    default="",
)


def normalize_pricing_country(value: Any) -> str:
    cc = str(value or "").strip().upper()

    if len(cc) != 2 or not cc.isalpha():
        return ""

    if cc in {"XX", "T1"}:
        return ""

    return cc


def get_pricing_country_code() -> str:
    return normalize_pricing_country(_PRICING_COUNTRY.get())


def set_pricing_country_code(value: Any) -> Token:
    return _PRICING_COUNTRY.set(
        normalize_pricing_country(value)
    )


def reset_pricing_country_code(token: Token) -> None:
    _PRICING_COUNTRY.reset(token)


class PricingCountryContextMiddleware:
    """
    Request-scoped billing geography.

    This is deliberately independent from content geography.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):

        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        pricing_country = ""

        for key, value in scope.get("headers") or []:
            if key.lower() == b"x-pricing-country-code":
                pricing_country = value.decode(
                    "ascii",
                    errors="ignore",
                )
                break

        token = set_pricing_country_code(
            pricing_country
        )

        try:
            await self.app(scope, receive, send)
        finally:
            reset_pricing_country_code(token)
