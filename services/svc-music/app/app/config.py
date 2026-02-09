from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    LOG_LEVEL: str = "INFO"
    DATABASE_URL: str
    REDIS_URL: str | None = None

    # JWT
    JWT_SECRET: str | None = None
    JWT_HMAC_SECRET: str | None = None
    JWT_ALG: str = "HS256"
    JWT_ISSUER: str = "desifaces"
    JWT_AUDIENCE: str = "desifaces_clients"
    JWT_LEEWAY_SECONDS: int = 60  # small clock skew tolerance

    # Azure
    AZURE_STORAGE_CONNECTION_STRING: str | None = None
    MUSIC_INPUT_CONTAINER: str = "music-input"
    MUSIC_OUTPUT_CONTAINER: str = "music-output"
    MUSIC_SAS_HOURS: int = 24
    AZURE_STORAGE_AUTO_CREATE_CONTAINER: bool = True

    # HeyGen
    HEYGEN_API_KEY: str | None = None
    HEYGEN_BASE_URL: str = "https://api.heygen.com"

    # fal.ai (Sonauto v2)
    FAL_KEY: str | None = None
    FAL_QUEUE_BASE_URL: str = "https://queue.fal.run"

    # Music autopilot provider routing
    MUSIC_AUTOPILOT_PROVIDER: str = "fal_sonauto_v2"  # default
    MUSIC_FAL_POLL_SECONDS: float = 2.5
    MUSIC_FAL_TIMEOUT_SECONDS: int = 900  # 15 minutes
    MUSIC_FAL_OBJECT_LIFECYCLE_SECONDS: int = 3600  # 1 hour on fal storage
    MUSIC_FAL_START_TIMEOUT_SECONDS: int | None = None  # optional “start within N seconds”


settings = Settings()