#!/usr/bin/env bash
set -euo pipefail

# Auto:
# 1) login to svc-core as user2@desifaces.ai / password2
# 2) discover latest completed uploaded HeyGen talking photo id
# 3) resolve latest face/audio artifact ids from Fusion DB
# 4) submit Fusion job with heygen_talking_photo_id
# 5) poll until done

CORE_URL="${CORE_URL:-http://localhost:8000}"
FUSION_BASE="${FUSION_BASE:-http://localhost:8002}"
FUSION_CONTAINER="${FUSION_CONTAINER:-df-svc-fusion}"
FUSION_WORKER_CONTAINER="${FUSION_WORKER_CONTAINER:-df-svc-fusion-worker}"

DF_EMAIL="${DF_EMAIL:-user2@desifaces.ai}"
DF_PASSWORD="${DF_PASSWORD:-password2}"
HEYGEN_API_KEY="${HEYGEN_API_KEY:?HEYGEN_API_KEY is required}"

VOICE_MODE="${VOICE_MODE:-audio}"          # audio | tts
EXTERNAL_PROVIDER_OK="${EXTERNAL_PROVIDER_OK:-true}"
ASPECT_RATIO="${ASPECT_RATIO:-9:16}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-1800}"
POLL_SECONDS="${POLL_SECONDS:-5}"

require_bin() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing required command: $1"
    exit 1
  }
}

require_bin curl
require_bin jq
require_bin docker
require_bin python3

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

echo "========================================"
echo "DesiFaces Fusion E2E (auto talking_photo)"
echo "========================================"
echo "CORE_URL               : ${CORE_URL}"
echo "FUSION_BASE            : ${FUSION_BASE}"
echo "FUSION_CONTAINER       : ${FUSION_CONTAINER}"
echo "FUSION_WORKER_CONTAINER: ${FUSION_WORKER_CONTAINER}"
echo "DF_EMAIL               : ${DF_EMAIL}"
echo "VOICE_MODE             : ${VOICE_MODE}"
echo "ASPECT_RATIO           : ${ASPECT_RATIO}"
echo

echo "[0] Check Fusion backend..."
docker exec -i "${FUSION_WORKER_CONTAINER}" python - <<'PY'
from app.config import settings
print("HEYGEN_EXECUTION_BACKEND =", getattr(settings, "HEYGEN_EXECUTION_BACKEND", None))
PY

echo
echo "[1] Login to svc-core..."
LOGIN_RESP="$(
  curl -sS -X POST "${CORE_URL}/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"${DF_EMAIL}\",\"password\":\"${DF_PASSWORD}\"}"
)"
echo "$LOGIN_RESP" | jq .

DF_TOKEN="$(echo "$LOGIN_RESP" | jq -r '.access_token // empty')"
if [[ -z "${DF_TOKEN}" ]]; then
  echo "ERROR: access_token missing from login response"
  exit 1
fi

echo
echo "[2] Discover latest HeyGen talking_photo_id..."
TMP_GROUPS="$(mktemp)"
curl -sS "https://api.heygen.com/v2/avatar_group.list" \
  -H "X-Api-Key: ${HEYGEN_API_KEY}" \
  -H "Accept: application/json" > "${TMP_GROUPS}"

echo "[debug] avatar_group.list saved to ${TMP_GROUPS}"

GROUP_IDS="$(
  jq -r '
    (
      .data.avatar_group_list
      // .data.list
      // .data.groups
      // []
    )[]? | .id // empty
  ' "${TMP_GROUPS}"
)"

if [[ -z "${GROUP_IDS}" ]]; then
  echo "ERROR: no avatar group ids found"
  cat "${TMP_GROUPS}" | jq .
  exit 1
fi

BEST_JSON="$(mktemp)"
printf '[]' > "${BEST_JSON}"

while IFS= read -r GROUP_ID; do
  [[ -z "${GROUP_ID}" ]] && continue

  RESP="$(mktemp)"
  curl -sS "https://api.heygen.com/v2/avatar_group/${GROUP_ID}/avatars" \
    -H "X-Api-Key: ${HEYGEN_API_KEY}" \
    -H "Accept: application/json" > "${RESP}"

  jq '
    (
      .data.avatar_list
      // .data.list
      // []
    )
    | map(select(
        ((.status // "") == "completed")
        and ((.business_type // "") == "uploaded")
      ))
    | map({
        id,
        group_id,
        name,
        status,
        business_type,
        created_at,
        image_url,
        default_voice_id
      })
  ' "${RESP}" > "${RESP}.parsed"

  python3 - <<PY
import json, pathlib
best_path = pathlib.Path("${BEST_JSON}")
resp_path = pathlib.Path("${RESP}.parsed")

best = json.loads(best_path.read_text())
new = json.loads(resp_path.read_text())
best.extend(new)
best_path.write_text(json.dumps(best))
PY
done <<< "${GROUP_IDS}"

TALKING_PHOTO_ID="$(
  python3 - <<PY
import json
items = json.loads(open("${BEST_JSON}").read())
items = [x for x in items if x.get("id")]
items.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
print(items[0]["id"] if items else "")
PY
)"

if [[ -z "${TALKING_PHOTO_ID}" ]]; then
  echo "ERROR: could not find a completed uploaded talking photo"
  cat "${BEST_JSON}" | jq .
  exit 1
fi

echo "TALKING_PHOTO_ID = ${TALKING_PHOTO_ID}"
echo "Talking photo candidates:"
cat "${BEST_JSON}" | jq 'sort_by(.created_at) | reverse | .[:10]'

echo
echo "[3] Resolve latest face/audio artifact ids from Fusion DB..."
RESOLVE_JSON="$(docker exec -i "${FUSION_CONTAINER}" python - <<'PY'
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
        "face_url": face["url"] if face else None,
        "audio_url": audio["url"] if audio else None,
    }
    print(json.dumps(out))
asyncio.run(main())
PY
)"

FACE_ARTIFACT_ID="$(echo "$RESOLVE_JSON" | jq -r '.face_artifact_id // empty')"
AUDIO_ARTIFACT_ID="$(echo "$RESOLVE_JSON" | jq -r '.audio_artifact_id // empty')"

if [[ -z "$FACE_ARTIFACT_ID" ]]; then
  echo "ERROR: could not find latest face artifact"
  exit 1
fi
if [[ -z "$AUDIO_ARTIFACT_ID" ]]; then
  echo "ERROR: could not find latest audio artifact"
  exit 1
fi

echo "FACE_ARTIFACT_ID  = ${FACE_ARTIFACT_ID}"
echo "AUDIO_ARTIFACT_ID = ${AUDIO_ARTIFACT_ID}"

echo
echo "[4] Build Fusion request..."
PAYLOAD="$(python3 - <<PY
import json
voice_mode = "${VOICE_MODE}"
aspect_ratio = "${ASPECT_RATIO}"
external_ok = "${EXTERNAL_PROVIDER_OK}" == "true"
talking_photo_id = "${TALKING_PHOTO_ID}"

payload = {
  "face_artifact_id": "${FACE_ARTIFACT_ID}",
  "heygen_talking_photo_id": talking_photo_id,
  "voice_mode": voice_mode,
  "consent": {"external_provider_ok": external_ok},
  "video": {"aspect_ratio": aspect_ratio},
}

if voice_mode == "audio":
  payload["voice_audio"] = {
    "type": "audio",
    "audio_artifact_id": "${AUDIO_ARTIFACT_ID}"
  }

print(json.dumps(payload, ensure_ascii=False))
PY
)"

echo "$PAYLOAD" | jq .

echo
echo "[5] Create Fusion job..."
CREATE_RESP="$(curl -sS -X POST "${FUSION_BASE}/jobs" \
  -H "Authorization: Bearer ${DF_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")"

echo "$CREATE_RESP" | jq .

JOB_ID="$(echo "$CREATE_RESP" | jq -r '.job_id // empty')"
if [[ -z "$JOB_ID" ]]; then
  echo "ERROR: job_id missing in create response"
  exit 1
fi

echo
echo "[6] Poll Fusion job..."
START="$(now_epoch)"
LAST_RESP=""

while true; do
  LAST_RESP="$(curl -sS "${FUSION_BASE}/jobs/${JOB_ID}" \
    -H "Authorization: Bearer ${DF_TOKEN}")"

  STATUS="$(echo "$LAST_RESP" | jq -r '.status // empty')"

  if [[ "$STATUS" == "succeeded" ]]; then
    echo "status=succeeded"
    break
  fi

  if [[ "$STATUS" == "failed" ]]; then
    echo "status=failed"
    echo "$LAST_RESP" | jq .
    exit 1
  fi

  NOW="$(now_epoch)"
  ELAPSED=$((NOW - START))
  if (( ELAPSED >= TIMEOUT_SECONDS )); then
    echo "ERROR: timeout waiting for Fusion job"
    echo "$LAST_RESP" | jq .
    exit 1
  fi

  echo "status=${STATUS:-unknown} (t=${ELAPSED}s)"
  sleep "$POLL_SECONDS"
done

DEBUG_JSON="/tmp/df_fusion_${JOB_ID}_status.json"
echo "$LAST_RESP" | jq . > "${DEBUG_JSON}"

echo
echo "[7] Final steps"
echo "$LAST_RESP" | jq '.steps'

echo
echo "[8] Final artifacts"
echo "$LAST_RESP" | jq '.artifacts'

VIDEO_URL="$(echo "$LAST_RESP" | jq -r '.artifacts[]? | select(.kind=="video") | .url' | head -n 1 || true)"
if [[ -z "$VIDEO_URL" ]]; then
  echo "WARN: no final video artifact found"
  echo "Saved debug JSON: ${DEBUG_JSON}"
  exit 0
fi

OUT="/tmp/df_fusion_${JOB_ID}.mp4"
echo
echo "[9] Download video"
echo "VIDEO_URL = ${VIDEO_URL}"
echo "OUT       = ${OUT}"
curl -sS -L -o "${OUT}" "${VIDEO_URL}"

if [[ ! -s "${OUT}" ]]; then
  echo "ERROR: downloaded video is empty"
  exit 1
fi

file "${OUT}" || true

echo
echo "Saved debug JSON: ${DEBUG_JSON}"
echo "========================================"
echo "E2E PASS ✅  job_id=${JOB_ID}"
echo "========================================"