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
    # Interactive Studio UX is human-review gated already. Keep the LLM critic
    # available for deeper validation, but do not block the first draft on it by
    # default. This reduces a simple Ask Creative Director request to one LLM call.
    DF_DIRECTOR_BLOCKING_CRITIC: bool = False
    DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP: bool = True

    # Director is control-plane only. Each Studio remains execution/pricing owner.
    DF_FACE_BASE_URL: str = "http://svc-face:8003"
    DF_AUDIO_BASE_URL: str = "http://svc-audio:8004"
    DF_FUSION_BASE_URL: str = "http://svc-fusion:8002"
    DF_FUSION_EXTENSION_BASE_URL: str = "http://svc-fusion-extension:8006"


settings = Settings()
