#!/usr/bin/env bash
set -euo pipefail

# --------------------------------------------
# DesiFaces - svc-fusion end-to-end smoke test
# --------------------------------------------
# UI Flow (how the app should behave):
#  1) Face Studio: user selects a face -> you have a face artifact_id
#  2) Fusion Studio: user generates audio via svc-audio -> you have an audio artifact_id
#  3) Fusion Studio: UI calls svc-fusion /jobs with those artifact IDs + consent + video settings
#
# This script simulates step (3) and also resolves "latest artifacts" for convenience.

FUSION_BASE="${FUSION_BASE:-http://localhost:8002}"
: "${DF_TOKEN:?DF_TOKEN is required}"

# UI Inputs (what Fusion Studio collects)
VOICE_MODE="${VOICE_MODE:-audio}"                # audio | tts
EXTERNAL_PROVIDER_OK="${EXTERNAL_PROVIDER_OK:-true}"
ASPECT_RATIO="${ASPECT_RATIO:-9:16}"            # MUST match AspectRatio enum values
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
POLL_SECONDS="${POLL_SECONDS:-3}"

AUTH=(-H "Authorization: Bearer ${DF_TOKEN}" -H "Content-Type: application/json")

bool_norm() {
  local v="${1:-}"
  v="$(echo "$v" | tr '[:upper:]' '[:lower:]' | xargs)"
  if [[ "$v" == "1" || "$v" == "true" || "$v" == "yes" || "$v" == "y" ]]; then
    echo "true"
  else
    echo "false"
  fi
}

now_epoch() { date +%s; }

EXTERNAL_PROVIDER_OK="$(bool_norm "$EXTERNAL_PROVIDER_OK")"

echo "=============================="
echo "DesiFaces svc-fusion E2E Test"
echo "=============================="
echo "FUSION_BASE    : ${FUSION_BASE}"
echo
echo "Fusion Studio UI Inputs:"
echo "  voice_mode                   : ${VOICE_MODE}"
echo "  consent.external_provider_ok  : ${EXTERNAL_PROVIDER_OK}"
echo "  aspect_ratio                 : ${ASPECT_RATIO}"
echo "------------------------------"

# ------------------------------------------------------------
# Resolve latest artifacts from shared DB (inside df-svc-fusion)
# ------------------------------------------------------------
echo "[resolve] picking latest artifacts from shared DB via df-svc-fusion container..."

RESOLVE_JSON="$(docker exec -i df-svc-fusion python - <<'PY'
import asyncio, asyncpg, json
from app.config import settings

async def main():
    pool = await asyncpg.create_pool(settings.DATABASE_URL)
    async with pool.acquire() as conn:
        face = await conn.fetchrow("""
            SELECT id::text AS id, kind, url
            FROM artifacts
            WHERE kind IN ('face','image','face_image')
            ORDER BY created_at DESC
            LIMIT 1
        """)
        audio = await conn.fetchrow("""
            SELECT id::text AS id, kind, url
            FROM artifacts
            WHERE kind = 'audio'
            ORDER BY created_at DESC
            LIMIT 1
        """)
    await pool.close()

    out = {
        "face_artifact_id": face["id"] if face else None,
        "audio_artifact_id": audio["id"] if audio else None,
    }
    print(json.dumps(out))
asyncio.run(main())
PY
)"

FACE_ARTIFACT_ID="$(echo "$RESOLVE_JSON" | jq -r '.face_artifact_id // empty')"
AUDIO_ARTIFACT_ID="$(echo "$RESOLVE_JSON" | jq -r '.audio_artifact_id // empty')"

if [[ -z "$FACE_ARTIFACT_ID" ]]; then
  echo "ERROR: could not find latest face artifact (kind in face/image/face_image)"
  exit 1
fi
if [[ -z "$AUDIO_ARTIFACT_ID" ]]; then
  echo "ERROR: could not find latest audio artifact (kind='audio')"
  exit 1
fi

echo "[resolve] face_artifact_id  = $FACE_ARTIFACT_ID"
echo "[resolve] audio_artifact_id = $AUDIO_ARTIFACT_ID"
echo

# ------------------------------------------------------------
# Build request payload (artifact IDs only, stable + clean)
# ------------------------------------------------------------
PAYLOAD="$(python3 - <<PY
import json
voice_mode = "${VOICE_MODE}"
aspect_ratio = "${ASPECT_RATIO}"
external_ok = "${EXTERNAL_PROVIDER_OK}" == "true"

payload = {
  "face_artifact_id": "${FACE_ARTIFACT_ID}",
  "voice_mode": voice_mode,
  "consent": {"external_provider_ok": external_ok},
  "video": {"aspect_ratio": aspect_ratio},
}

if voice_mode == "audio":
  payload["voice_audio"] = {
    "type": "audio",
    "audio_artifact_id": "${AUDIO_ARTIFACT_ID}",
  }

print(json.dumps(payload, ensure_ascii=False))
PY
)"

echo "Request payload (UI -> svc-fusion):"
echo "$PAYLOAD" | jq
echo

# ------------------------------------------------------------
# 1) Create fusion job
# ------------------------------------------------------------
echo "[1] Create fusion job: POST /jobs"
CREATE_RESP="$(curl -sS -X POST "${FUSION_BASE}/jobs" "${AUTH[@]}" -d "$PAYLOAD")"
echo "$CREATE_RESP" | jq

JOB_ID="$(echo "$CREATE_RESP" | jq -r '.job_id // empty')"
if [[ -z "$JOB_ID" ]]; then
  echo "ERROR: job_id missing in create response"
  exit 1
fi

echo
echo "[2] Polling job: GET /jobs/${JOB_ID}"
START="$(now_epoch)"

STATUS=""
LAST_RESP=""

while true; do
  LAST_RESP="$(curl -sS "${FUSION_BASE}/jobs/${JOB_ID}" -H "Authorization: Bearer ${DF_TOKEN}")"
  STATUS="$(echo "$LAST_RESP" | jq -r '.status // empty')"

  if [[ "$STATUS" == "succeeded" ]]; then
    echo "status=succeeded"
    break
  fi

  if [[ "$STATUS" == "failed" ]]; then
    echo "status=failed"
    echo "$LAST_RESP" | jq
    exit 1
  fi

  NOW="$(now_epoch)"
  ELAPSED=$((NOW - START))
  if (( ELAPSED >= TIMEOUT_SECONDS )); then
    echo "ERROR: timeout waiting for job to finish (status=${STATUS:-unknown})"
    echo "$LAST_RESP" | jq
    exit 1
  fi

  echo "status=${STATUS:-unknown} (t=${ELAPSED}s)"
  sleep "$POLL_SECONDS"
done

echo
echo "[3] Steps:"
echo "$LAST_RESP" | jq '.steps'

echo
echo "[4] Artifacts:"
echo "$LAST_RESP" | jq '.artifacts'

VIDEO_URL="$(echo "$LAST_RESP" | jq -r '.artifacts[]? | select(.kind=="video") | .url' | head -n 1 || true)"
if [[ -z "$VIDEO_URL" ]]; then
  echo
  echo "WARN: no video artifact found in response (job succeeded but missing video artifact?)"
  echo "E2E PASS ✅  job_id=${JOB_ID}"
  exit 0
fi

OUT="/tmp/df_fusion_${JOB_ID}.mp4"
echo
echo "[5] Downloading video:"
echo "  url : ${VIDEO_URL}"
echo "  out : ${OUT}"
curl -sS -L -o "$OUT" "$VIDEO_URL"

if [[ ! -s "$OUT" ]]; then
  echo "ERROR: downloaded video is empty: $OUT"
  exit 1
fi

echo "Downloaded: $OUT"
file "$OUT" || true

echo
echo "=============================="
echo "E2E PASS ✅  job_id=${JOB_ID}"
echo "=============================="