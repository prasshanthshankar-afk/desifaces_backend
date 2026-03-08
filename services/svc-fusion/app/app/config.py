from __future__ import annotations

import logging
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DF_",
        env_file=".env",
        extra="ignore",
    )

    # DB
    DATABASE_URL: str = Field(
        default="postgresql://desifaces_admin:desifaces_mahadev@localhost:5432/desifaces",
        validation_alias=AliasChoices("DATABASE_URL", "DF_DATABASE_URL"),
    )

    # Service behavior
    LOG_LEVEL: str = "INFO"
    WORKER_POLL_SECONDS: float = 2.0
    WORKER_BATCH_SIZE: int = 10
    JOB_POLL_MAX_SECONDS: int = 900
    JOB_POLL_INTERVAL_SECONDS: float = 5.0
    WORKER_IDLE_SLEEP_SECONDS: float = 2.0
    WORKER_CLAIM_LIMIT: int = 1

    # Feature flags
    FUSION_STUDIO_ENABLED: bool = True

    # Azure storage
    AZURE_STORAGE_CONNECTION_STRING: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "AZURE_STORAGE_CONNECTION_STRING",
            "DF_AZURE_STORAGE_CONNECTION_STRING",
        ),
    )
    AZURE_AUDIO_CONTAINER: str = "heygen-audio"
    AZURE_SAS_EXPIRY_HOURS: int = 2

    # Storage
    STORAGE_SAS_EXPIRY_SECONDS: int = 3600

    # HeyGen / Fusion AV4
    HEYGEN_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("HEYGEN_API_KEY", "DF_HEYGEN_API_KEY"),
    )
    HEYGEN_BASE_URL: str = Field(
        default="https://api.heygen.com",
        validation_alias=AliasChoices("HEYGEN_BASE_URL", "DF_HEYGEN_BASE_URL"),
    )
    HEYGEN_TIMEOUT_SECONDS: int = Field(
        default=60,
        validation_alias=AliasChoices("HEYGEN_TIMEOUT_SECONDS", "DF_HEYGEN_TIMEOUT_SECONDS"),
    )
    HEYGEN_MAX_POLL_TIME: int = Field(
        default=600,
        validation_alias=AliasChoices("HEYGEN_MAX_POLL_TIME", "DF_HEYGEN_MAX_POLL_TIME"),
    )

    # Idempotency / payload versioning
    HEYGEN_AV4_PAYLOAD_VERSION: str = Field(
        default="av4.v1",
        validation_alias=AliasChoices(
            "HEYGEN_AV4_PAYLOAD_VERSION",
            "DF_HEYGEN_AV4_PAYLOAD_VERSION",
        ),
    )

    # Backend switch: direct | fal
    HEYGEN_EXECUTION_BACKEND: str = Field(
        default="direct",
        validation_alias=AliasChoices(
            "HEYGEN_EXECUTION_BACKEND",
            "DF_HEYGEN_EXECUTION_BACKEND",
        ),
    )

    # Back-compat override if older code uses this name
    DF_FUSION_HEYGEN_EXECUTION_BACKEND: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "DF_FUSION_HEYGEN_EXECUTION_BACKEND",
            "FUSION_HEYGEN_EXECUTION_BACKEND",
        ),
    )

    # fal.ai AV4 execution settings
    FAL_KEY: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("FAL_KEY", "DF_FAL_KEY"),
    )
    FAL_QUEUE_BASE_URL: str = Field(
        default="https://queue.fal.run",
        validation_alias=AliasChoices("FAL_QUEUE_BASE_URL", "DF_FAL_QUEUE_BASE_URL"),
    )
    FAL_HEYGEN_AV4_ENDPOINT: str = Field(
        default="fal-ai/heygen/avatar4/image-to-video",
        validation_alias=AliasChoices(
            "FAL_HEYGEN_AV4_ENDPOINT",
            "DF_FAL_HEYGEN_AV4_ENDPOINT",
        ),
    )

    # Optional safety for TTS mode on fal
    HEYGEN_FAL_PASS_VOICE_ID_AS_NAME: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "HEYGEN_FAL_PASS_VOICE_ID_AS_NAME",
            "DF_HEYGEN_FAL_PASS_VOICE_ID_AS_NAME",
        ),
    )


settings = Settings()

if settings.AZURE_STORAGE_CONNECTION_STRING is None:
    logging.getLogger("config").warning(
        "AZURE_STORAGE_CONNECTION_STRING is not set; Fusion will run in Phase-1 mode (provider URLs only)."
    )