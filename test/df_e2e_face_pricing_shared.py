
from __future__ import annotations

import json
from common_pricing_e2e_runner import ServiceConfig, run_service_pricing_e2e

CONFIG = ServiceConfig(
    name="face",
    base_url_env="FACE_URL",
    preview_path="/api/face/creator/pricing/preview",
    generate_path="/api/face/creator/generate",
    status_path_template="/api/face/creator/jobs/{job_id}/status",
    preview_payload={
        "mode": "text-to-image",
        "num_variants": 1,
        "gender": "female",
        "age_group": "adult",
        "style_preset": "studio_portrait",
        "country_code": "US",
        "channel": "web",
        "pricing_confirmation": {
            "confirmed": False
        }
    },
    generate_payload={
        "mode": "text-to-image",
        "num_variants": 1,
        "gender": "female",
        "age_group": "adult",
        "style_preset": "studio_portrait",
        "country_code": "US",
        "channel": "web",
        "pricing_confirmation": {
            "confirmed": True
        }
    },
)

if __name__ == "__main__":
    print(json.dumps(run_service_pricing_e2e(CONFIG), indent=2))
