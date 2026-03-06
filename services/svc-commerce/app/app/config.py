from __future__ import annotations

import os

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except Exception:  # pragma: no cover
    from pydantic import BaseSettings  # type: ignore
    SettingsConfigDict = None  # type: ignore


class Settings(BaseSettings):
    ENV: str = os.getenv("ENV", "dev")

    # Azure (keep defaults empty; runtime validation happens when AzureStorageService is instantiated)
    AZURE_STORAGE_CONNECTION_STRING: str = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
    COMMERCE_OUTPUT_CONTAINER: str = os.getenv("COMMERCE_OUTPUT_CONTAINER", "")

    COMMERCE_SAREE_ALPHA_MASK_PATH: str | None = None
    COMMERCE_SAREE_CACHE_DIR: str = "/var/cache/df_saree_templates"

    if SettingsConfigDict is not None:
        model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()