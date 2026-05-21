
from __future__ import annotations

import json
import os

from common_pricing_e2e_runner import ServiceConfig, run_service_pricing_e2e

CONFIG = ServiceConfig(
    name="fusion",
    base_url_env="FUSION_URL",
    preview_path=os.getenv("FUSION_PREVIEW_PATH", "/api/fusion/jobs/pricing/preview"),
    generate_path=os.getenv("FUSION_GENERATE_PATH", "/api/fusion/jobs"),
    status_path_template=os.getenv("FUSION_STATUS_PATH_TEMPLATE", "/api/fusion/jobs/{job_id}/status"),
    preview_payload={
        "face_artifact_id": os.getenv("FACE_ARTIFACT_ID", "REQUIRED_FACE_ARTIFACT_ID"),
        "audio_artifact_id": os.getenv("AUDIO_ARTIFACT_ID", "REQUIRED_AUDIO_ARTIFACT_ID"),
        "external_provider_ok": True,
        "video": {
            "duration_sec": 10
        },
        "channel": "web",
        "country_code": "US",
        "pricing_confirmation": {
            "confirmed": False
        }
    },
    generate_payload={
        "face_artifact_id": os.getenv("FACE_ARTIFACT_ID", "REQUIRED_FACE_ARTIFACT_ID"),
        "audio_artifact_id": os.getenv("AUDIO_ARTIFACT_ID", "REQUIRED_AUDIO_ARTIFACT_ID"),
        "external_provider_ok": True,
        "video": {
            "duration_sec": 10
        },
        "channel": "web",
        "country_code": "US",
        "pricing_confirmation": {
            "confirmed": True
        }
    },
    timeout_seconds=1200,
    poll_seconds=5,
)

if __name__ == "__main__":
    print(json.dumps(run_service_pricing_e2e(CONFIG), indent=2))
