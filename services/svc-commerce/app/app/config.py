from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

try:
    # pydantic v2
    from pydantic_settings import BaseSettings, SettingsConfigDict
except Exception:  # pragma: no cover
    # pydantic v1 fallback
    from pydantic import BaseSettings  # type: ignore
    SettingsConfigDict = None  # type: ignore


class Settings(BaseSettings):
    """
    Minimal config for svc-commerce.

    IMPORTANT:
      - Keep defaults empty so the service can start even without Azure configured.
      - AzureStorageService should validate at runtime when actually used.
    """

    # Common / environment
    ENV: str = os.getenv("ENV", "dev")

    # Azure
    AZURE_STORAGE_CONNECTION_STRING: str = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
    COMMERCE_OUTPUT_CONTAINER: str = os.getenv("COMMERCE_OUTPUT_CONTAINER", "")

    # Optional: sometimes used by other utilities
    AZURE_STORAGE_CONTAINER: str = os.getenv("AZURE_STORAGE_CONTAINER", "")

    if SettingsConfigDict is not None:  # pydantic v2
        model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()