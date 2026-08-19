from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    DATABASE_URL: str
    JWT_SECRET: str
    JWT_ALG: str = "HS256"
    JWT_AUDIENCE: str = "desifaces_clients"
    JWT_ISSUER: str = "desifaces"

    DF_DIRECTOR_LLM_MODEL: str = ""
    DF_DIRECTOR_EMBEDDING_MODEL: str = ""
    DF_DIRECTOR_REVIEW_REQUIRED: bool = True
    DF_DIRECTOR_MAX_REVISIONS: int = 3
    DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP: bool = True

    # V3 control-plane bridge to the existing Face Studio API. Provider execution
    # still happens in svc-face-worker; Director only previews, dispatches and
    # reconciles participant-scoped Face output slots.
    DF_FACE_BASE_URL: str = "http://svc-face:8003"


settings = Settings()
