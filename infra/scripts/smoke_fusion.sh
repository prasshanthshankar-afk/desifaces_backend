# infra/scripts/smoke_fusion.sh
#!/usr/bin/env bash
set -euo pipefail

# -------------------------------
# Config (override via env)
# -------------------------------
BASE_URL="${BASE_URL:-http://localhost:8002}"   # svc-fusion base (or your nginx)
CREATE_PATH="${CREATE_PATH:-/api/studio/fusion/jobs}"
GET_PATH_PREFIX="${GET_PATH_PREFIX:-/api/studio/fusion/jobs}"

# Polling
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-600}"        # 10 minutes
POLL_SECONDS="${POLL_SECONDS:-4}"               # poll interval

# Inputs (override as needed)
FACE_IMAGE_URL="${FACE_IMAGE_URL:-https://example.com/sample-face.jpg}"  # must be reachable by HeyGen
VOICE_MODE="${VOICE_MODE:-tts}"                                     # tts | audio
VOICE_ID="${VOICE_ID:-}"                                            # required if tts
SCRIPT_TEXT="${SCRIPT_TEXT:-Hello from DesiFaces. This is a Fusion smoke test.}"
AUDIO_URL="${AUDIO_URL:-}"                                          # required if audio

ASPECT_RATIO="${ASPECT_RATIO:-9:16}"

python3 - <<'PY'
import os, sys, time, json
import urllib.request

base = os.environ.get("BASE_URL","http://localhost:8002").rstrip("/")
create_path = os.environ.get("CREATE_PATH","/api/studio/fusion/jobs")
get_prefix = os.environ.get("GET_PATH_PREFIX","/api/studio/fusion/jobs")

timeout = int(os.environ.get("TIMEOUT_SECONDS","600"))
poll_s = float(os.environ.get("POLL_SECONDS","4"))

face_url = os.environ.get("FACE_IMAGE_URL","").strip()
voice_mode = os.environ.get("VOICE_MODE","tts").strip()
voice_id = os.environ.get("VOICE_ID","").strip()
script_text = os.environ.get("SCRIPT_TEXT","Hello").strip()
audio_url = os.environ.get("AUDIO_URL","").strip()
aspect = os.environ.get("ASPECT_RATIO","9:16").strip()

if not face_url:
    print("ERROR: FACE_IMAGE_URL is required (must be publicly reachable by HeyGen).", file=sys.stderr)
    sys.exit(1)

if voice_mode not in ("tts","audio"):
    print("ERROR: VOICE_MODE must be 'tts' or 'audio'", file=sys.stderr)
    sys.exit(1)

if voice_mode == "tts" and not voice_id:
    print("ERROR: VOICE_ID is required when VOICE_MODE=tts (e.g., set VOICE_ID env var).", file=sys.stderr)
    sys.exit(1)

if voice_mode == "audio" and not audio_url:
    print("ERROR: AUDIO_URL is required when VOICE_MODE=audio.", file=sys.stderr)
    sys.exit(1)

payload = {
    "face_image_url": face_url,
    "voice_mode": voice_mode,
    "voice_audio": {"type":"audio","audio_url": audio_url} if voice_mode=="audio" else None,
    "voice_tts": {"type":"tts","voice_id": voice_id, "script": script_text} if voice_mode=="tts" else None,
    "video": {"aspect_ratio": aspect},
    "consent": {"external_provider_ok": True},
    "provider": "heygen_av4",
    "tags": {"smoke_test": True}
}
# remove nulls
payload = {k:v for k,v in payload.items() if v is not None}

def http_json(method, url, body=None):
    data = None
    headers = {"Accept":"application/json","Content-Type":"application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)

create_url = base + create_path
print(f"POST {create_url}")
print("Request payload:", json.dumps(payload, indent=2))

try:
    created = http_json("POST", create_url, payload)
except Exception as e:
    print(f"ERROR creating job: {e}", file=sys.stderr)
    sys.exit(1)

job_id = created.get("job_id") or created.get("id")
if not job_id:
    print("ERROR: create response missing job_id. Response:", created, file=sys.stderr)
    sys.exit(1)

print(f"\n✅ Created fusion job_id={job_id}")

get_url = f"{base}{get_prefix}/{job_id}"

def find_artifacts(resp):
    arts = resp.get("artifacts") or []
    video = None
    share = None
    for a in arts:
        if (a.get("kind") or "").lower() == "video":
            video = a.get("url")
        if (a.get("kind") or "").lower() == "share_url":
            share = a.get("url")
    return video, share

deadline = time.time() + timeout
last_status = None

print(f"\nPolling: {get_url}")
while True:
    if time.time() > deadline:
        print("\n❌ TIMEOUT waiting for video + share_url artifacts.")
        sys.exit(2)

    try:
        resp = http_json("GET", get_url)
    except Exception as e:
        print(f"WARN: GET failed: {e}")
        time.sleep(poll_s)
        continue

    status = resp.get("status")
    if status != last_status:
        last_status = status
        print(f"Status: {status}")

    video_url, share_url = find_artifacts(resp)

    # Print provider_job_id if available (nice for debugging)
    provider_job_id = resp.get("provider_job_id")
    if provider_job_id:
        print(f"provider_job_id: {provider_job_id}")

    if video_url and share_url:
        print("\n✅ SUCCESS: Found both artifacts")
        print("video_url:", video_url)
        print("share_url:", share_url)
        sys.exit(0)

    if status == "failed":
        print("\n❌ Job failed.")
        print("error_code:", resp.get("error_code"))
        print("error_message:", resp.get("error_message"))
        print("Full response:", json.dumps(resp, indent=2))
        sys.exit(3)

    time.sleep(poll_s)
PY