
from __future__ import annotations

import json
import os

from common_pricing_e2e_runner import ServiceConfig, run_service_pricing_e2e

CONFIG = ServiceConfig(
    name="audio",
    base_url_env="AUDIO_URL",
    preview_path=os.getenv("AUDIO_PREVIEW_PATH", "/api/audio/tts/pricing/preview"),
    generate_path=os.getenv("AUDIO_GENERATE_PATH", "/api/audio/tts/generate"),
    status_path_template=os.getenv("AUDIO_STATUS_PATH_TEMPLATE", "/api/audio/jobs/{job_id}/status"),
    preview_payload={
        "text": "Welcome to DesiFaces. This is a pricing contract check for audio generation.",
        "voice_id": os.getenv("AUDIO_VOICE_ID", "default"),
        "locale": os.getenv("AUDIO_LOCALE", "en-IN"),
        "channel": "web",
        "country_code": "US",
        "pricing_confirmation": {
            "confirmed": False
        }
    },
    generate_payload={
        "text": "Welcome to DesiFaces. This is a pricing contract check for audio generation.",
        "voice_id": os.getenv("AUDIO_VOICE_ID", "default"),
        "locale": os.getenv("AUDIO_LOCALE", "en-IN"),
        "channel": "web",
        "country_code": "US",
        "pricing_confirmation": {
            "confirmed": True
        }
    },
)

if __name__ == "__main__":
    print(json.dumps(run_service_pricing_e2e(CONFIG), indent=2))
