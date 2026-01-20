from __future__ import annotations

import json
from typing import Dict, Optional
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

    # ----------------------------
    # Defaults / voice map override
    # ----------------------------
    # JSON string override: {"en-US":"en-US-JennyNeural", "hi-IN":"hi-IN-SwaraNeural", ...}
    DEFAULT_VOICE_MAP_JSON: str = ""

    # Worker polling
    WORKER_POLL_SECS: float = 1.5
    WORKER_BATCH_SIZE: int = 1

    class Config:
        env_file = ".env"
        extra = "ignore"

    def default_voice_map(self) -> Dict[str, str]:
        # Reasonable defaults; override with DEFAULT_VOICE_MAP_JSON
        base = {
            # English (US/UK/India)
            "en-US": "en-US-JennyNeural",
            "en-GB": "en-GB-SoniaNeural",
            "en-IN": "en-IN-NeerjaNeural",

            # Hindi
            "hi-IN": "hi-IN-SwaraNeural",

            # Indian regional languages (expand as needed)
            "ta-IN": "ta-IN-PallaviNeural",
            "te-IN": "te-IN-ShrutiNeural",
            "kn-IN": "kn-IN-SapnaNeural",
            "ml-IN": "ml-IN-SobhanaNeural",
            "mr-IN": "mr-IN-AarohiNeural",
            "bn-IN": "bn-IN-TanishaaNeural",
            "gu-IN": "gu-IN-DhwaniNeural",
            "pa-IN": "pa-IN-GurleenNeural",
            "or-IN": "or-IN-SubhasiniNeural",
            "as-IN": "as-IN-YashicaNeural",
        }
        if self.DEFAULT_VOICE_MAP_JSON.strip():
            try:
                override = json.loads(self.DEFAULT_VOICE_MAP_JSON)
                if isinstance(override, dict):
                    base.update({str(k): str(v) for k, v in override.items()})
            except Exception:
                # ignore invalid JSON
                pass
        return base


settings = Settings()