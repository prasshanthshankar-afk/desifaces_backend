# services/svc-marketing/app/app/config.py
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    # Service
    SERVICE_NAME: str = "svc-marketing"
    PORT: int = 8010

    # Auth
    AUTH_MODE: str = Field(default="none", description="none|svc_core")
    CORE_URL: str = Field(default="http://svc-core:8000")
    CORE_INTROSPECT_PATH: str = Field(default="/api/auth/introspect")

    # DB
    POSTGRES_DSN: str = Field(default="postgresql://postgres:postgres@desifaces-db:5432/desifaces")

    # Admin / bucketing
    ADMIN_MARKETING_USER_ID: str = Field(default="", description="UUID for admin marketing account")
    DEFAULT_COST_BUCKET: str = Field(default="internal_marketing")
    DEFAULT_COST_OWNER: str = Field(default="desifaces_marketing")
    STRICT_ADMIN_ONLY: bool = Field(default=True, description="Require admin for publish and admin endpoints")

    # Downstream services
    SVC_FACE_URL: str = Field(default="http://svc-face:8001")
    SVC_FUSION_URL: str = Field(default="http://svc-fusion:8002")
    SVC_MUSIC_URL: str = Field(default="http://svc-music:8007")
    SVC_COMMERCE_URL: str = Field(default="http://svc-commerce:8008")

    # Endpoint paths (customize to match your current APIs)
    FACE_CREATE_PATH: str = Field(default="/api/face/creator/generate")
    FACE_STATUS_PATH_TMPL: str = Field(default="/api/face/creator/jobs/{job_id}/status")

    FUSION_CREATE_PATH: str = Field(default="/jobs")
    FUSION_STATUS_PATH_TMPL: str = Field(default="/jobs/{job_id}")

    MUSIC_GENERATE_PATH: str = Field(default="/api/music/generate")
    MUSIC_STATUS_PATH_TMPL: str = Field(default="/api/music/jobs/{job_id}/status")

    COMMERCE_PROMO_PATH: str = Field(default="/api/commerce/promo/generate")
    COMMERCE_STATUS_PATH_TMPL: str = Field(default="/api/commerce/jobs/{job_id}/status")

    # LLM
    LLM_MODE: str = Field(default="none", description="none|openai")
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = Field(default="gpt-4.1-mini")
    OPENAI_BASE_URL: Optional[str] = None

    # Output / Storage
    OUTPUT_DIR: str = Field(default="/tmp/df_marketing_output")
    AZURE_STORAGE_CONNECTION_STRING: Optional[str] = None
    AZURE_OUTPUT_CONTAINER: str = Field(default="marketing-output")
    AZURE_BLOB_PREFIX: str = Field(default="marketing")

    ADMIN_MARKETING_EMAIL: str = Field(default="", description="Email of marketing admin (lookup)")
    ADMIN_MARKETING_USER_TABLE: str = Field(default="core.users", description="DB table to resolve admin user id (schema-qualified allowed)")
    ADMIN_MARKETING_USER_ID_COLUMN: str = Field(default="id")
    ADMIN_MARKETING_USER_EMAIL_COLUMN: str = Field(default="email")

    # Runtime
    WORKER_POLL_SECONDS: int = Field(default=2)

    # Feature flags
    ENABLE_PUBLISH_IG: bool = Field(default=False)

    # Instagram publishing (optional)
    IG_ACCESS_TOKEN: Optional[str] = None
    IG_BUSINESS_ACCOUNT_ID: Optional[str] = None

    # ---- NEW: data-driven usecase evolution ----
    ENABLE_USECASE_CURATION: bool = Field(default=True, description="Allow LLM to suggest new use cases (still requires admin approval)")
    ENABLE_OPTIMIZER: bool = Field(default=True, description="Adjust use case weights based on metrics")
    OPTIMIZER_INTERVAL_SECONDS: int = Field(default=6 * 60 * 60)  # every 6 hours
    OPTIMIZER_LOOKBACK_DAYS: int = Field(default=14)
    OPTIMIZER_MIN_WEIGHT: float = Field(default=0.1)
    OPTIMIZER_MAX_WEIGHT: float = Field(default=10.0)


settings = Settings()