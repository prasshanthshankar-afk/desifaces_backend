from __future__ import annotations

from typing import Optional
from pydantic import BaseModel
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ----------------------------
    # Service
    # ----------------------------
    LOG_LEVEL: str = "INFO"

    # ----------------------------
    # Database
    # ----------------------------
    DATABASE_URL: str = "postgresql://desifaces_admin:desifaces_admin@desifaces-db:5432/desifaces"

    # ----------------------------
    # Azure Storage (Audio artifacts)
    # ----------------------------
    AZURE_STORAGE_CONNECTION_STRING: str = ""
    AUDIO_OUTPUT_CONTAINER: str = "audio-output"  # create this container in the same storage account

    # SAS expiry hours
    AUDIO_SAS_HOURS: int = 24

    # ----------------------------
    # Azure Speech TTS
    # ----------------------------
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_REGION: str = "eastus"
    # Common formats for the Speech REST API:
    # - audio-24khz-48kbitrate-mono-mp3
    # - audio-48khz-192kbitrate-mono-mp3
    # - riff-24khz-16bit-mono-pcm
    AZURE_SPEECH_OUTPUT_FORMAT: str = "audio-48khz-192kbitrate-mono-mp3"

    # ----------------------------
    # Translation (optional but recommended)
    # ----------------------------
    AZURE_TRANSLATOR_KEY: str = ""
    AZURE_TRANSLATOR_ENDPOINT: str = ""  # e.g. https://api.cognitive.microsofttranslator.com
    AZURE_TRANSLATOR_REGION: str = ""    # required if using multi-service key

    # Worker polling
    WORKER_POLL_SECS: float = 1.5
    WORKER_BATCH_SIZE: int = 1

    class Config:
        env_file = ".env"
        extra = "ignore"




settings = Settings()