from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # DB
    DATABASE_URL: str = Field(..., description="postgresql://user:pass@host:port/dbname")

    # Auth (matches docker-compose env)
    JWT_SECRET: str = Field(..., description="JWT signing secret (same as svc-core)")
    JWT_ALG: str = Field(default="HS256", description="JWT algorithm")

    # Dashboard behavior
    DASHBOARD_STALE_SECONDS: int = Field(default=30)
    DASHBOARD_FORCE_REFRESH_ON_MISS: bool = Field(default=True)

    # Worker
    DASHBOARD_WORKER_ENABLED: bool = Field(default=True)
    DASHBOARD_WORKER_POLL_SECONDS: float = Field(default=0.75)
    DASHBOARD_WORKER_BATCH_SIZE: int = Field(default=50)

    # Azure Blob (SAS signing)
    AZURE_STORAGE_CONNECTION_STRING: str = Field(
        ..., description="Azure Storage connection string (must include AccountName and AccountKey)"
    )
    # Optional default container (only needed if storage_path doesn't include container)
    AZURE_STORAGE_CONTAINER_NAME: str = Field(
        default="", description="Default Azure Blob container name (optional if storage_path includes container)"
    )

    # SAS TTL policy for dashboard items
    DASHBOARD_FACE_SAS_TTL_SECONDS: int = Field(default=15 * 24 * 3600)
    DASHBOARD_VIDEO_SAS_TTL_SECONDS: int = Field(default=15 * 24 * 3600)
    DASHBOARD_RECENT_VIDEO_SAS_TTL_SECONDS: int = Field(default=15 * 24 * 3600)
    DASHBOARD_RECENT_WINDOW_DAYS: int = Field(default=15)

    LOG_LEVEL: str = Field(default="INFO")

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()