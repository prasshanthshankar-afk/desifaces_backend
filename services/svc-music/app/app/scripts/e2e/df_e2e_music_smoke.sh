#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8007}"
TOKEN="${TOKEN:-}"
MODE="${1:-autopilot}"                 # autopilot | co_create | bring_your_own
TITLE="${TITLE:-Smoke Test $(date +%F_%H%M%S)}"
DUET_LAYOUT="${DUET_LAYOUT:-split_screen}"
LANGUAGE_HINT="${LANGUAGE_HINT:-en}"

VOICE_REF_FILE="${VOICE_REF_FILE:-}"   # optional: path to mp3/wav
BYO_SONG_FILE="${BYO_SONG_FILE:-}"     # optional: path to mp3/wav for BYO mode

POLL_SECS="${POLL_SECS:-5}"
POLL_MAX="${POLL_MAX:-80}"             # ~6-7 mins at 5s polling

require() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1" >&2; exit 1; }; }
require curl
require jq
require python3

if [[ -z "$TOKEN" ]]; then
  echo "ERROR: set TOKEN env var (Bearer token)"; exit 1
fi

authH=(-H "Authorization: Bearer $TOKEN")

http_post_json () {
  local url="$1"
  local json="$2"
  curl -sS -D /tmp/hdr.txt -o /tmp/body.txt -w "%{http_code}" \
    -X POST "$url" "${authH[@]}" -H "Content-Type: application/json" \
    --data "$json"
}

http_get () {
  local url="$1"
  curl -sS -D /tmp/hdr.txt -o /tmp/body.txt -w "%{http_code}" \
    -X GET "$url" "${authH[@]}"
}

echo "== Preflight =="
curl -sS "$BASE_URL/api/health" >/dev/null || { echo "svc-music health failed"; exit 1; }
echo "OK: $BASE_URL/api/health"

echo
echo "== Create project/job =="
create_payload=$(jq -nc \
  --arg title "$TITLE" \
  --arg mode "$MODE" \
  --arg duet_layout "$DUET_LAYOUT" \
  --arg language_hint "$LANGUAGE_HINT" \
  '{title:$title, mode:$mode, duet_layout:$duet_layout, language_hint:$language_hint}'
)

code=$(http_post_json "$BASE_URL/api/music/projects" "$create_payload")
cat /tmp/body.txt | jq . || true
if [[ "$code" != "200" && "$code" != "201" ]]; then
  echo "Create project failed: HTTP $code"
  cat /tmp/body.txt
  exit 1
fi

# Robust extraction: some APIs return {project_id, job_id}, others nest it.
PROJECT_ID=$(cat /tmp/body.txt | jq -r '.project_id // .project.id // .id // empty')
JOB_ID=$(cat /tmp/body.txt | jq -r '.job_id // .job.id // .studio_job_id // empty')

if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: could not extract PROJECT_ID from create response"; exit 1
fi
if [[ -z "$JOB_ID" ]]; then
  echo "WARN: could not extract JOB_ID from create response; will try to locate via status endpoint later."
fi

echo "PROJECT_ID=$PROJECT_ID"
echo "JOB_ID=${JOB_ID:-<missing from create response>}"

echo
echo "== Optional: upload voice reference =="
if [[ -n "$VOICE_REF_FILE" ]]; then
  if [[ ! -f "$VOICE_REF_FILE" ]]; then
    echo "VOICE_REF_FILE not found: $VOICE_REF_FILE"; exit 1
  fi
  code=$(curl -sS -o /tmp/body.txt -w "%{http_code}" \
    -X POST "$BASE_URL/api/music/projects/$PROJECT_ID/voice-reference" \
    "${authH[@]}" \
    -F "file=@${VOICE_REF_FILE}")
  cat /tmp/body.txt | jq . || true
  if [[ "$code" != "200" && "$code" != "201" ]]; then
    echo "Voice ref upload failed: HTTP $code"
    exit 1
  fi
else
  echo "(skipped)"
fi

echo
echo "== Optional: upload BYO song audio (only if provided) =="
if [[ -n "$BYO_SONG_FILE" ]]; then
  if [[ ! -f "$BYO_SONG_FILE" ]]; then
    echo "BYO_SONG_FILE not found: $BYO_SONG_FILE"; exit 1
  fi

  # Try a small set of likely endpoints (adjust to your actual one).
  # The first that returns 200/201 wins.
  candidates=(
    "$BASE_URL/api/music/projects/$PROJECT_ID/byo-audio"
    "$BASE_URL/api/music/projects/$PROJECT_ID/song"
    "$BASE_URL/api/music/projects/$PROJECT_ID/tracks/upload"
    "$BASE_URL/api/music/projects/$PROJECT_ID/tracks"
  )

  ok=""
  for url in "${candidates[@]}"; do
    code=$(curl -sS -o /tmp/body.txt -w "%{http_code}" \
      -X POST "$url" "${authH[@]}" -F "file=@${BYO_SONG_FILE}")
    if [[ "$code" == "200" || "$code" == "201" ]]; then
      ok="$url"
      echo "Uploaded BYO audio via: $url"
      cat /tmp/body.txt | jq . || true
      break
    fi
  done
  if [[ -z "$ok" ]]; then
    echo "BYO upload failed on all candidate endpoints."
    echo "Last response:"
    cat /tmp/body.txt
    echo "TIP: check $BASE_URL/docs for the correct BYO upload route, then update candidates[]"
    exit 1
  fi
else
  echo "(skipped)"
fi

echo
echo "== Publish =="
# Minimal publish payload (you MUST align this with OpenAPI required fields).
# Adjust these keys after running the openapi introspection step.
publish_payload=$(jq -nc \
  --arg intent "smoke test: generate a short music video" \
  '{intent:$intent}'
)

if [[ -z "${JOB_ID:-}" ]]; then
  echo "ERROR: No JOB_ID available for publish. If your API publishes by project_id, update script accordingly."
  exit 1
fi

code=$(http_post_json "$BASE_URL/api/music/jobs/$JOB_ID/publish" "$publish_payload")
cat /tmp/body.txt | jq . || true
if [[ "$code" != "200" && "$code" != "201" ]]; then
  echo "Publish failed: HTTP $code"
  cat /tmp/body.txt
  echo "TIP: run OpenAPI inspector to learn required PublishMusicIn fields."
  exit 1
fi

echo
echo "== Poll status until done =="
i=0
while [[ $i -lt $POLL_MAX ]]; do
  code=$(http_get "$BASE_URL/api/music/jobs/$JOB_ID/status")
  if [[ "$code" != "200" ]]; then
    echo "Status failed: HTTP $code"
    cat /tmp/body.txt
    exit 1
  fi

  status=$(cat /tmp/body.txt | jq -r '.status // empty')
  stage=$(cat /tmp/body.txt | jq -r '.stage // empty')
  progress=$(cat /tmp/body.txt | jq -r '.progress // empty')
  err=$(cat /tmp/body.txt | jq -r '.error // empty')

  echo "[$i/$POLL_MAX] status=$status stage=$stage progress=$progress"
  if [[ -n "$err" && "$err" != "null" ]]; then
    echo "ERROR: $err"
    exit 1
  fi

  if [[ "$status" == "succeeded" || "$status" == "failed" ]]; then
    break
  fi

  sleep "$POLL_SECS"
  i=$((i+1))
done

echo
echo "== Validate outputs =="
cat /tmp/body.txt > /tmp/music_status.json

python3 - <<'PY'
import json
p="/tmp/music_status.json"
j=json.load(open(p,"r",encoding="utf-8"))

def must(path, ok):
    if not ok:
        raise SystemExit(f"Missing/empty: {path}")

must("$.status", j.get("status"))
computed = j.get("computed") or {}
clip = j.get("clip_manifest") or {}

# Key guarantees for svc-fusion-extension handoff
must("$.computed.music_plan", computed.get("music_plan"))
must("$.computed.plan_summary", computed.get("plan_summary"))
must("$.clip_manifest", clip)

tracks = j.get("tracks") or []
must("$.tracks[]", len(tracks) > 0)

# find some URL-ish fields
urls=[]
for t in tracks:
    for k in ("url","asset_url","artifact_url","audio_url","video_url"):
        u=t.get(k)
        if isinstance(u,str) and u.startswith("http"):
            urls.append(u)

print("Tracks:", len(tracks))
print("URLs found:", len(urls))
for u in urls[:5]:
    print(" -", u)
PY

echo
echo "== HEAD-check URLs (first few) =="
urls=$(python3 - <<'PY'
import json
j=json.load(open("/tmp/music_status.json","r",encoding="utf-8"))
tracks=j.get("tracks") or []
out=[]
for t in tracks:
    for k in ("url","asset_url","artifact_url","audio_url","video_url"):
        u=t.get(k)
        if isinstance(u,str) and u.startswith("http"):
            out.append(u)
print("\n".join(out[:5]))
PY
)

if [[ -n "$urls" ]]; then
  while IFS= read -r u; do
    [[ -z "$u" ]] && continue
    code=$(curl -sS -I -o /dev/null -w "%{http_code}" "$u")
    echo "HEAD $code  $u"
  done <<< "$urls"
else
  echo "No URLs found in tracks to HEAD-check."
fi

echo
echo "✅ Smoke test completed."