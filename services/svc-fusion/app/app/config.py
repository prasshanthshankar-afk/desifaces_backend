from __future__ import annotations
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DF_", env_file=".env", extra="ignore")

    # DB
    DATABASE_URL: str = Field(
        default="postgresql://desifaces_admin:desifaces_mahadev@localhost:5432/desifaces",
        validation_alias="DATABASE_URL",
    )

    # Service behavior
    LOG_LEVEL: str = "INFO"
    WORKER_POLL_SECONDS: float = 2.0
    WORKER_BATCH_SIZE: int = 10
    JOB_POLL_MAX_SECONDS: int = 900  # 15 minutes
    JOB_POLL_INTERVAL_SECONDS: float = 5.0

    AZURE_STORAGE_CONNECTION_STRING: Optional[str] = Field(
        default=None,
        validation_alias="AZURE_STORAGE_CONNECTION_STRING",
    )

    # Feature flags (hard-block)
    FUSION_STUDIO_ENABLED: bool = True  # env override; can be replaced by shared/df_core flags later

    WORKER_IDLE_SLEEP_SECONDS: float = 2.0
    WORKER_CLAIM_LIMIT: int = 1

    # HeyGen AV4
    HEYGEN_API_KEY: str = ""
    HEYGEN_BASE_URL: str = "https://api.heygen.com"
    HEYGEN_TIMEOUT_SECONDS: int = 60
    HEYGEN_MAX_POLL_TIME: int = 600  # 10 minutes

    # Idempotency / payload versioning
    HEYGEN_AV4_PAYLOAD_VERSION: str = "av4.v1"

    AZURE_STORAGE_CONNECTION_STRING: str
    AZURE_AUDIO_CONTAINER: str = "heygen-audio"
    AZURE_SAS_EXPIRY_HOURS: int = 2

    # Storage (placeholders for later; keep plug-in ready)
    STORAGE_SAS_EXPIRY_SECONDS: int = 3600


settings = Settings()

if settings.AZURE_STORAGE_CONNECTION_STRING is None:
    # Keep INFO level so you notice but it doesn't block boot
    import logging
    logging.getLogger("config").warning(
        "AZURE_STORAGE_CONNECTION_STRING is not set; Fusion will run in Phase-1 mode (provider URLs only)."
    )