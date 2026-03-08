#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
from pathlib import Path

import httpx

FUSION_BASE = os.getenv("FUSION_BASE", "http://localhost:8002")
DF_TOKEN = os.getenv("DF_TOKEN")
HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY")
JOB_ID = os.getenv("JOB_ID")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
TIMEOUT_SECONDS = int(os.getenv("TIMEOUT_SECONDS", "900"))
VIDEO_WIDTH = int(os.getenv("VIDEO_WIDTH", "720"))
VIDEO_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "1280"))
TEST_MODE = os.getenv("TEST_MODE", "false").strip().lower() == "true"

if not DF_TOKEN:
    raise SystemExit("DF_TOKEN is required")
if not HEYGEN_API_KEY:
    raise SystemExit("HEYGEN_API_KEY is required")
if not JOB_ID:
    raise SystemExit("JOB_ID is required")

ts = time.strftime("%Y%m%d_%H%M%S")
out_dir = Path(f"/tmp/heygen_v2_test_{JOB_ID}_{ts}")
out_dir.mkdir(parents=True, exist_ok=True)

job_json_path = out_dir / "fusion_job.json"
face_path = out_dir / "face_input.png"
upload_json_path = out_dir / "heygen_upload.json"
submit_payload_path = out_dir / "submit_payload.json"
submit_json_path = out_dir / "heygen_submit.json"
status_json_path = out_dir / "heygen_status_final.json"

def pick_first_artifact(job: dict, *kinds: str) -> str:
    artifacts = job.get("artifacts") or []
    for kind in kinds:
        for a in artifacts:
            if a.get("kind") == kind and a.get("url"):
                return str(a["url"])
    return ""

print("========================================")
print("HeyGen V2 direct test from Fusion job")
print("========================================")
print("FUSION_BASE     :", FUSION_BASE)
print("JOB_ID          :", JOB_ID)
print("OUT_DIR         :", out_dir)
print("VIDEO_DIMENSION :", f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}")
print("POLL_SECONDS    :", POLL_SECONDS)
print("TIMEOUT_SECONDS :", TIMEOUT_SECONDS)
print()

with httpx.Client(timeout=120) as client:
    print("[1] Fetching Fusion job...")
    r = client.get(
        f"{FUSION_BASE}/jobs/{JOB_ID}",
        headers={"Authorization": f"Bearer {DF_TOKEN}"},
    )
    r.raise_for_status()
    job = r.json()
    job_json_path.write_text(json.dumps(job, indent=2))
    print(json.dumps(job, indent=2))

    face_url = pick_first_artifact(job, "provider_face_ref", "resolved_face_sas_url", "face_image_url")
    audio_url = pick_first_artifact(job, "provider_audio_ref", "resolved_audio_sas_url", "heygen_audio_url")

    if not face_url:
        raise SystemExit("Could not find face URL in Fusion job artifacts")
    if not audio_url:
        raise SystemExit("Could not find audio URL in Fusion job artifacts")

    print()
    print("[2] Resolved refs")
    print("FACE_URL :", face_url)
    print("AUDIO_URL:", audio_url)

    print()
    print("[3] Downloading face image...")
    r = client.get(face_url)
    r.raise_for_status()
    face_path.write_bytes(r.content)
    print("Saved:", face_path, "bytes:", face_path.stat().st_size)

mime = mimetypes.guess_type(str(face_path))[0] or "image/png"

print()
print("[4] Uploading face image to HeyGen asset API...")
with face_path.open("rb") as fh, httpx.Client(timeout=120) as client:
    r = client.post(
        "https://upload.heygen.com/v1/asset",
        headers={
            "X-Api-Key": HEYGEN_API_KEY,
            "Accept": "application/json",
        },
        files={"file": (face_path.name, fh, mime)},
    )

upload_text = r.text
try:
    upload_json = r.json()
except Exception:
    upload_json = {"raw_text": upload_text}

upload_json_path.write_text(json.dumps(upload_json, indent=2))
print(json.dumps(upload_json, indent=2))

if r.status_code >= 400:
    raise SystemExit(f"HeyGen upload failed {r.status_code}: {upload_text}")

image_key = (
    upload_json.get("data", {}).get("image_key")
    or upload_json.get("image_key")
    or upload_json.get("data", {}).get("asset_id")
    or upload_json.get("asset_id")
    or upload_json.get("data", {}).get("id")
    or upload_json.get("id")
)

if not image_key:
    raise SystemExit(f"image_key missing from upload response: {upload_json}")

print()
print("IMAGE_KEY:", image_key)

submit_payload = {
    "test": TEST_MODE,
    "video_title": f"desifaces_fusion_v2_test_{JOB_ID}",
    "image_key": image_key,
    "audio_url": audio_url,
    "dimension": {"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT},
    "use_avatar_iv_model": True,
}
submit_payload_path.write_text(json.dumps(submit_payload, indent=2))

print()
print("[5] Submitting /v2/video/generate ...")
print(json.dumps(submit_payload, indent=2))

idempotency_key = f"desifaces-fusion-v2-test-{JOB_ID}-{ts}"
with httpx.Client(timeout=120) as client:
    r = client.post(
        "https://api.heygen.com/v2/video/generate",
        headers={
            "X-Api-Key": HEYGEN_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        json=submit_payload,
    )

submit_text = r.text
try:
    submit_json = r.json()
except Exception:
    submit_json = {"raw_text": submit_text}

submit_json_path.write_text(json.dumps(submit_json, indent=2))
print(json.dumps(submit_json, indent=2))

if r.status_code >= 400:
    raise SystemExit(f"HeyGen submit failed {r.status_code}: {submit_text}")

video_id = (
    submit_json.get("data", {}).get("video_id")
    or submit_json.get("video_id")
    or submit_json.get("data", {}).get("id")
    or submit_json.get("id")
)

if not video_id:
    raise SystemExit(f"video_id missing from submit response: {submit_json}")

print()
print("VIDEO_ID:", video_id)

print()
print("[6] Polling HeyGen status...")
start = time.time()
final_status = {}

with httpx.Client(timeout=120) as client:
    while True:
        elapsed = int(time.time() - start)
        r = client.get(
            "https://api.heygen.com/v1/video_status.get",
            headers={
                "X-Api-Key": HEYGEN_API_KEY,
                "Accept": "application/json",
            },
            params={"video_id": video_id},
        )

        text = r.text
        try:
            final_status = r.json()
        except Exception:
            final_status = {"raw_text": text}

        status = (
            str(final_status.get("data", {}).get("status") or "")
            or str(final_status.get("status") or "")
            or str(final_status.get("data", {}).get("video_status") or "")
            or str(final_status.get("video_status") or "")
        ).strip().lower()

        print(f"status={status or 'unknown'} elapsed={elapsed}s")

        if status in {"completed", "succeeded", "success", "failed", "error"}:
            break
        if elapsed >= TIMEOUT_SECONDS:
            print("Timed out waiting for final status")
            break

        time.sleep(POLL_SECONDS)

status_json_path.write_text(json.dumps(final_status, indent=2))
print()
print("[7] Final HeyGen status")
print(json.dumps(final_status, indent=2))

print()
print("Saved files:")
print(" ", job_json_path)
print(" ", face_path)
print(" ", upload_json_path)
print(" ", submit_payload_path)
print(" ", submit_json_path)
print(" ", status_json_path)