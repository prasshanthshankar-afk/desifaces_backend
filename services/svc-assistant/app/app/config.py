from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    DATABASE_URL: str
    REDIS_URL: str = "redis://desifaces-redis:6379/0"

    JWT_SECRET: str
    JWT_ALG: str = "HS256"
    JWT_AUDIENCE: str = "desifaces_clients"
    JWT_ISSUER: str = "desifaces"

    OPENAI_API_KEY: str = ""
    DF_ASSISTANT_LLM_MODEL: str = ""
    DF_ASSISTANT_EMBEDDING_MODEL: str = ""

    DF_DIRECTOR_BASE_URL: str = "http://svc-director:8011"
    DF_DASHBOARD_BASE_URL: str = "http://svc-dashboard:8005"
    DF_PRICING_BASE_URL: str = "http://svc-pricing:8009"
    DF_ASSISTANT_DISPLAY_NAME: str = "Piku"
    DF_ASSISTANT_SESSION_TTL_SECONDS: int = 86400
    DF_ASSISTANT_MAX_HISTORY_MESSAGES: int = 12
    DF_ASSISTANT_RAG_TOP_K: int = 5
    DF_ASSISTANT_HTTP_TIMEOUT_SECONDS: float = 8.0
    DF_ASSISTANT_KNOWLEDGE_DIR: str = "/app/knowledge"


settings = Settings()
