# services/svc-pricing/app/app/config.py
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Service
    SERVICE_NAME: str = "svc-pricing"
    LOG_LEVEL: str = Field(default="INFO")

    # Database
    DATABASE_URL: str = Field(default="postgresql://desifaces_admin:desifaces_admin@localhost:5432/desifaces")
    DB_POOL_MIN_SIZE: int = Field(default=1)
    DB_POOL_MAX_SIZE: int = Field(default=10)
    DB_COMMAND_TIMEOUT_S: int = Field(default=60)

    # Auth expectations
    REQUIRE_AUTH: bool = Field(default=True)
    REQUIRE_X_USER_ID: bool = Field(default=True)
    ALLOW_JWT_SUB_FALLBACK: bool = Field(default=True)

    # RBAC (core.roles.role_key whitelist for admin routes)
    PRICING_ADMIN_ROLE_KEYS: str = Field(default="admin,ops,pricing_admin")

    MAX_CREDITS_PER_RESERVATION: int = Field(default=250000) 

    # Pricing / reservations
    DEFAULT_RESERVATION_TTL_S: int = Field(default=900)   # 15 min
    MAX_RESERVATION_TTL_S: int = Field(default=3600)      # 60 min
    RESERVATION_EXPIRE_BATCH: int = Field(default=50)

    # Worker loop
    EXPIRER_POLL_INTERVAL_S: int = Field(default=10)
    EXPIRER_JITTER_S: int = Field(default=2)

    # Money rounding precision
    MONEY_DECIMALS: int = Field(default=2)

    # Global ops override: disabled|shadow|free|bill
    GLOBAL_BILLING_MODE_OVERRIDE: str = Field(default="")  # empty = no override


settings = Settings()