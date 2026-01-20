# services/svc-face/app/app/config.py
from __future__ import annotations
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Service
    SERVICE_NAME: str = "svc-face"
    PORT: int = 8003
    LOG_LEVEL: str = "INFO"
    
    # Database
    DATABASE_URL: str
    
    # Redis
    REDIS_URL: str = "redis://desifaces-redis:6379/0"
    
    # Azure Storage
    AZURE_STORAGE_CONNECTION_STRING: str
    FACE_OUTPUT_CONTAINER: str = "face-output"
    
    # fal.ai
    FAL_API_KEY: str
    FAL_MODEL: str = "fal-ai/flux-pro/v1.1"
    
    # Azure OpenAI (for GPT-4 prompt generation)
    AZURE_OPENAI_ENDPOINT: str  # e.g., https://your-resource.openai.azure.com/
    AZURE_OPENAI_KEY: str
    AZURE_OPENAI_DEPLOYMENT: str  # Your deployment name (e.g., gpt-4)
    
    # Azure Content Moderator
    AZURE_CONTENT_MODERATOR_ENDPOINT: str
    AZURE_CONTENT_MODERATOR_KEY: str
    
    # JWT
    JWT_SECRET: str
    JWT_ALG: str = "HS256"
    JWT_ISSUER: str = "desifaces"
    JWT_AUDIENCE: str = "desifaces_clients"
    
    # Job Settings
    JOB_TIMEOUT_SECONDS: int = 180
    MAX_CONCURRENT_JOBS: int = 5
    
    class Config:
        env_file = ".env"
        env_prefix = ""

settings = Settings()