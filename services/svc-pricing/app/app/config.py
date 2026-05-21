from __future__ import annotations

import json
from typing import Any, Dict, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    SERVICE_NAME: str = "svc-pricing"
    LOG_LEVEL: str = Field(default="INFO")

    DATABASE_URL: str = Field(default="postgresql://desifaces_admin:desifaces_admin@localhost:5432/desifaces")
    DB_POOL_MIN_SIZE: int = Field(default=1)
    DB_POOL_MAX_SIZE: int = Field(default=10)
    DB_COMMAND_TIMEOUT_S: int = Field(default=60)

    REQUIRE_AUTH: bool = Field(default=True)
    REQUIRE_X_USER_ID: bool = Field(default=True)
    ALLOW_JWT_SUB_FALLBACK: bool = Field(default=True)

    PRICING_ADMIN_ROLE_KEYS: str = Field(default="admin,ops,pricing_admin")

    MAX_CREDITS_PER_RESERVATION: int = Field(default=250000)
    DEFAULT_RESERVATION_TTL_S: int = Field(default=900)
    MAX_RESERVATION_TTL_S: int = Field(default=3600)
    RESERVATION_EXPIRE_BATCH: int = Field(default=50)
    EXPIRER_POLL_INTERVAL_S: int = Field(default=10)
    EXPIRER_JITTER_S: int = Field(default=2)
    MONEY_DECIMALS: int = Field(default=2)
    GLOBAL_BILLING_MODE_OVERRIDE: str = Field(default="")

    DF_PAYMENT_GATEWAY_ENABLED: bool = Field(default=False)
    DF_PAYMENT_GATEWAY_PROVIDER: str = Field(default="stripe")
    DF_PAYMENT_WEBHOOK_TOLERANCE_SECONDS: int = Field(default=300)

    STRIPE_SECRET_KEY: str = Field(default="")
    STRIPE_PUBLISHABLE_KEY: str = Field(default="")
    STRIPE_WEBHOOK_SECRET: str = Field(default="")
    STRIPE_API_VERSION: str = Field(default="2025-03-31.basil")
    STRIPE_BILLING_PORTAL_ENABLED: bool = Field(default=True)

    DF_PLAN_PRO_MONTHLY_PRICE_ID: str = Field(default="")
    DF_PLAN_PRO_YEARLY_PRICE_ID: str = Field(default="")
    DF_PLAN_BUSINESS_MONTHLY_PRICE_ID: str = Field(default="")
    DF_PLAN_BUSINESS_YEARLY_PRICE_ID: str = Field(default="")
    DF_PLAN_ENTERPRISE_MONTHLY_PRICE_ID: str = Field(default="")
    DF_PLAN_ENTERPRISE_YEARLY_PRICE_ID: str = Field(default="")

    # Optional richer mapping. Supports either:
    # 1) Flat:
    # {
    #   "pro_monthly_v1": {"price_id":"price_usd", "currency":"USD", "tier_code":"pro", "interval":"month", "rank":10}
    # }
    #
    # 2) Currency-specific:
    # {
    #   "pro_monthly_v1": {
    #     "tier_code":"pro",
    #     "interval":"month",
    #     "rank":10,
    #     "prices": {
    #       "USD": "price_usd",
    #       "INR": "price_inr"
    #     }
    #   }
    # }
    #
    # 3) Currency-specific with metadata:
    # {
    #   "pro_monthly_v1": {
    #     "tier_code":"pro",
    #     "interval":"month",
    #     "rank":10,
    #     "prices": {
    #       "USD": {"price_id":"price_usd", "currency":"USD"},
    #       "INR": {"price_id":"price_inr", "currency":"INR"}
    #     }
    #   }
    # }
    DF_PLAN_PRICE_MAP_JSON: str = Field(default="")

    # Fallback only. Business rule should come from country_code.
    DF_WALLET_TOPUP_CURRENCY: str = Field(default="USD")
    DF_PAYMENT_SUCCESS_URL_BASE: str = Field(default="")
    DF_PAYMENT_CANCEL_URL_BASE: str = Field(default="")

    def normalize_currency(self, value: Optional[str]) -> str:
        return (value or "").strip().upper()

    def currency_for_country(self, country_code: Optional[str]) -> str:
        cc = (country_code or "").strip().upper()
        return "INR" if cc == "IN" else "USD"

    def _default_plan_map(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        default_currency = self.normalize_currency(self.DF_WALLET_TOPUP_CURRENCY) or "USD"

        def add(code: str, price_id: str, tier_code: str, interval: str, rank: int) -> None:
            pid = (price_id or "").strip()
            if not pid:
                return
            out[code] = {
                "price_id": pid,
                "currency": default_currency,
                "tier_code": tier_code,
                "interval": interval,
                "rank": rank,
            }

        add("pro_monthly_v1", self.DF_PLAN_PRO_MONTHLY_PRICE_ID, "pro", "month", 10)
        add("pro_yearly_v1", self.DF_PLAN_PRO_YEARLY_PRICE_ID, "pro", "year", 11)
        add("business_monthly_v1", self.DF_PLAN_BUSINESS_MONTHLY_PRICE_ID, "business", "month", 20)
        add("business_yearly_v1", self.DF_PLAN_BUSINESS_YEARLY_PRICE_ID, "business", "year", 21)
        add("enterprise_monthly_v1", self.DF_PLAN_ENTERPRISE_MONTHLY_PRICE_ID, "enterprise", "month", 30)
        add("enterprise_yearly_v1", self.DF_PLAN_ENTERPRISE_YEARLY_PRICE_ID, "enterprise", "year", 31)
        return out

    def plan_price_map(self) -> Dict[str, Dict[str, Any]]:
        out = self._default_plan_map()
        raw = (self.DF_PLAN_PRICE_MAP_JSON or "").strip()
        if not raw:
            return out
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if isinstance(value, dict):
                        out[str(key).strip().lower()] = value
        except Exception:
            pass
        return out

    def normalize_plan_code(self, value: str) -> str:
        code = (value or "").strip().lower()
        aliases = {
            "pro_monthly": "pro_monthly_v1",
            "pro_yearly": "pro_yearly_v1",
            "business_monthly": "business_monthly_v1",
            "business_yearly": "business_yearly_v1",
            "enterprise_monthly": "enterprise_monthly_v1",
            "enterprise_yearly": "enterprise_yearly_v1",
        }
        return aliases.get(code, code)

    def _coerce_currency_entry(
        self,
        *,
        base: Dict[str, Any],
        selected_currency: str,
        value: Any,
    ) -> Optional[Dict[str, Any]]:
        if isinstance(value, str):
            pid = value.strip()
            if not pid:
                return None
            return {
                **base,
                "price_id": pid,
                "currency": selected_currency,
            }
        if isinstance(value, dict):
            pid = str(value.get("price_id") or "").strip()
            if not pid:
                return None
            return {
                **base,
                **value,
                "price_id": pid,
                "currency": self.normalize_currency(value.get("currency")) or selected_currency,
            }
        return None

    def get_plan_details(self, plan_code: str, currency: Optional[str] = None) -> Optional[Dict[str, Any]]:
        code = self.normalize_plan_code(plan_code)
        details = self.plan_price_map().get(code)
        if not isinstance(details, dict):
            return None

        desired_currency = self.normalize_currency(currency)

        # Flat entry with direct price_id
        direct_price_id = str(details.get("price_id") or "").strip()
        if direct_price_id:
            out = dict(details)
            out["price_id"] = direct_price_id
            out["currency"] = self.normalize_currency(out.get("currency")) or desired_currency or "USD"
            return out

        # prices: { "USD": "...", "INR": "..." } or nested dicts
        prices = details.get("prices")
        if isinstance(prices, dict):
            if desired_currency:
                for key, value in prices.items():
                    if self.normalize_currency(key) == desired_currency:
                        return self._coerce_currency_entry(
                            base={k: v for k, v in details.items() if k != "prices"},
                            selected_currency=desired_currency,
                            value=value,
                        )

            # fallback to any first valid entry
            for key, value in prices.items():
                selected_currency = self.normalize_currency(key) or desired_currency or "USD"
                coerced = self._coerce_currency_entry(
                    base={k: v for k, v in details.items() if k != "prices"},
                    selected_currency=selected_currency,
                    value=value,
                )
                if coerced:
                    return coerced

        # Also support top-level currency keys like:
        # {"USD": {"price_id": ...}, "INR": {"price_id": ...}, "tier_code": ...}
        if desired_currency:
            for key, value in details.items():
                if self.normalize_currency(key) == desired_currency:
                    return self._coerce_currency_entry(
                        base={k: v for k, v in details.items() if self.normalize_currency(k) not in {"USD", "INR"}},
                        selected_currency=desired_currency,
                        value=value,
                    )

        for key, value in details.items():
            key_ccy = self.normalize_currency(key)
            if key_ccy in {"USD", "INR"}:
                coerced = self._coerce_currency_entry(
                    base={k: v for k, v in details.items() if self.normalize_currency(k) not in {"USD", "INR"}},
                    selected_currency=key_ccy,
                    value=value,
                )
                if coerced:
                    return coerced

        return None

    def price_id_to_plan_code(self, price_id: str) -> Optional[str]:
        pid = (price_id or "").strip()
        if not pid:
            return None

        for code, meta in self.plan_price_map().items():
            if not isinstance(meta, dict):
                continue

            direct_price_id = str(meta.get("price_id") or "").strip()
            if direct_price_id == pid:
                return code

            prices = meta.get("prices")
            if isinstance(prices, dict):
                for value in prices.values():
                    if isinstance(value, str) and value.strip() == pid:
                        return code
                    if isinstance(value, dict) and str(value.get("price_id") or "").strip() == pid:
                        return code

            for key, value in meta.items():
                if self.normalize_currency(key) in {"USD", "INR"}:
                    if isinstance(value, str) and value.strip() == pid:
                        return code
                    if isinstance(value, dict) and str(value.get("price_id") or "").strip() == pid:
                        return code

        return None

    def plan_rank(self, plan_code: str) -> int:
        details = self.get_plan_details(plan_code) or {}
        try:
            if details.get("rank") is not None:
                return int(details["rank"])
        except Exception:
            pass

        code = self.normalize_plan_code(plan_code)
        if code.startswith("enterprise"):
            return 30 if "monthly" in code else 31
        if code.startswith("business") or code.startswith("team"):
            return 20 if "monthly" in code else 21
        if code.startswith("pro"):
            return 10 if "monthly" in code else 11
        return 0


settings = Settings()