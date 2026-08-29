#!/usr/bin/env bash
set -euo pipefail

# desifaces Dashboard/Saved Work customer-media certification.
#
# Proves the product contract, not merely endpoint availability:
#   - internal Story/Fusion audio turns are absent from Saved Work;
#   - scene/segment/child videos are absent;
#   - Saved Work Videos and Dashboard Home use customer_final_outputs;
#   - every returned final video has a playable video URL and an image thumbnail.
#
# Usage:
#   export DF_EMAIL='...'
#   export DF_PASSWORD='...'
#   export CORE_URL='http://127.0.0.1:18000'
#   export DASHBOARD_URL='http://127.0.0.1:18005'
#   bash services/svc-dashboard/df_dashboard_video_final_proof.sh

: "${DF_EMAIL:?Set DF_EMAIL}"
: "${DF_PASSWORD:?Set DF_PASSWORD}"
CORE_URL="${CORE_URL:-https://api.desifaces.ai/core}"
DASHBOARD_URL="${DASHBOARD_URL:-https://api.desifaces.ai/dashboard}"
LOGIN_PATH="${LOGIN_PATH:-/api/auth/login}"
TMP_DIR="${TMP_DIR:-/tmp/df_dashboard_customer_media_proof}"
mkdir -p "$TMP_DIR"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing required command: $1" >&2; exit 2; }
}
need curl
need jq
need python3

login_payload="$TMP_DIR/login_payload.json"
login_response="$TMP_DIR/login_response.json"
printf '%s' "{\"email\":\"$DF_EMAIL\",\"password\":\"$DF_PASSWORD\"}" > "$login_payload"

login_url="${CORE_URL%/}${LOGIN_PATH}"
echo "== Login: $login_url"
http_code=$(curl -sS -o "$login_response" -w '%{http_code}' \
  -X POST "$login_url" \
  -H 'Content-Type: application/json' \
  -d @"$login_payload")

if [[ "$http_code" -lt 200 || "$http_code" -ge 300 ]]; then
  echo "Login failed HTTP $http_code"
  jq . "$login_response" 2>/dev/null || cat "$login_response"
  exit 1
fi

TOKEN=$(jq -r '.access_token // .token // .data.access_token // .data.token // .auth.access_token // empty' "$login_response")
USER_ID=$(jq -r '.user_id // .user.id // .data.user_id // .data.user.id // empty' "$login_response")

if [[ -z "$TOKEN" || "$TOKEN" == "null" ]]; then
  echo "Could not extract token from login response"
  exit 1
fi

if [[ -z "$USER_ID" || "$USER_ID" == "null" ]]; then
  USER_ID=$(python3 - "$TOKEN" <<'PY'
import base64, json, sys
parts = sys.argv[1].split('.')
if len(parts) < 2:
    print('')
    raise SystemExit
p = parts[1] + '=' * (-len(parts[1]) % 4)
try:
    claims = json.loads(base64.urlsafe_b64decode(p.encode()).decode())
    print(claims.get('sub') or claims.get('user_id') or '')
except Exception:
    print('')
PY
)
fi

if [[ -z "$USER_ID" || "$USER_ID" == "null" ]]; then
  echo "Could not determine user id"
  exit 1
fi

echo "== Authenticated dashboard certification user resolved"

fetch_dashboard() {
  local name="$1"
  local path="$2"
  local out="$TMP_DIR/$name.json"
  local url="${DASHBOARD_URL%/}$path"
  local code
  code=$(curl -sS -o "$out" -w '%{http_code}' \
    "$url" \
    -H "Authorization: Bearer $TOKEN")
  echo "$name HTTP $code"
  if [[ "$code" -lt 200 || "$code" -ge 300 ]]; then
    jq . "$out" 2>/dev/null || cat "$out"
    exit 1
  fi
}

fetch_dashboard home '/api/dashboard/home?force=false'
fetch_dashboard library_all '/api/dashboard/library?type=all&limit=100&offset=0'
fetch_dashboard library_face '/api/dashboard/library?type=face&limit=100&offset=0'
fetch_dashboard library_audio '/api/dashboard/library?type=audio&limit=100&offset=0'
fetch_dashboard library_video '/api/dashboard/library?type=video&limit=100&offset=0'

python3 - "$TMP_DIR" <<'PY'
import json, pathlib, re, sys
root = pathlib.Path(sys.argv[1])

INTERNAL_RE = re.compile(
    r'story_dialogue_workflow_id|dialogue_turn_id|story_audio|story_voice|'
    r'child_render|child_role|internal_child|child_job_of_billable_longform_parent|'
    r'suppress_pricing|pricing_suppressed|parent_longform_job_id|billing_parent_job_id|parent_story_job_id|'
    r'segment_id|segment_index|segment_number|scene_id|scene_index|shot_id|shot_index',
    re.I,
)
VIDEO_EXT_RE = re.compile(r'\.(mp4|mov|m4v|webm)(?:\?|#|$)', re.I)
IMAGE_EXT_RE = re.compile(r'\.(jpg|jpeg|png|webp|avif)(?:\?|#|$)', re.I)


def load(name):
    with open(root / f'{name}.json', 'r', encoding='utf-8') as f:
        return json.load(f)


def items(payload):
    return payload.get('items') or []


def fail(message):
    raise SystemExit('FAIL: ' + message)


def stable_id(x):
    return str(x.get('library_id') or x.get('source_job_id') or x.get('artifact_id') or x.get('media_asset_id') or '')

home = load('home')
allp = load('library_all')
facep = load('library_face')
audiop = load('library_audio')
vidp = load('library_video')

for name, payload in [('all', allp), ('audio', audiop), ('video', vidp)]:
    scope = payload.get('display_scope')
    if scope != 'customer_final_outputs':
        fail(f'{name} display_scope={scope!r}, expected customer_final_outputs')

for name, rows in [('all', items(allp)), ('audio', items(audiop)), ('video', items(vidp))]:
    bad = [x for x in rows if INTERNAL_RE.search(json.dumps(x, default=str))]
    if bad:
        sample = [stable_id(x) or str(x.get('title')) for x in bad[:5]]
        fail(f'{name} contains internal Story/Fusion artifacts: {sample}')

# Face and standalone Audio remain valid customer library asset types.
if any(str(x.get('studio') or '').lower() not in ('face', '') for x in items(facep)):
    fail('face endpoint returned non-face rows')
if any(str(x.get('studio') or '').lower() not in ('audio', '') for x in items(audiop)):
    fail('audio endpoint returned non-audio rows')

video_rows = items(vidp)
for x in video_rows:
    studio = str(x.get('studio') or x.get('asset_type') or '').lower()
    if studio not in ('video', 'fusion'):
        fail(f'video endpoint returned unexpected studio={studio!r}')

    reuse = x.get('reuse_payload') or {}
    video_url = str(reuse.get('video_url') or x.get('preview_url') or x.get('download_url') or '').strip()
    thumb = str(x.get('thumbnail_url') or reuse.get('thumbnail_url') or reuse.get('poster_url') or x.get('poster_url') or '').strip()

    if not video_url or not VIDEO_EXT_RE.search(video_url):
        fail(f'final video lacks playable video URL: {stable_id(x)}')
    if not thumb:
        fail(f'final video lacks thumbnail: {stable_id(x)}')
    if VIDEO_EXT_RE.search(thumb):
        fail(f'final video thumbnail points to video content: {stable_id(x)}')
    if not IMAGE_EXT_RE.search(thumb):
        fail(f'final video thumbnail is not an image URL: {stable_id(x)}')

home_rows = home.get('video_carousel') or []
if home.get('video_display_scope') != 'customer_final_outputs':
    fail(f'home video_display_scope={home.get("video_display_scope")!r}')

home_bad = [x for x in home_rows if INTERNAL_RE.search(json.dumps(x, default=str))]
if home_bad:
    fail('Dashboard Home contains internal child/scene artifacts')

if video_rows and not home_rows:
    fail('final videos exist but Dashboard Recent Videos is empty')

video_ids = {stable_id(x) for x in video_rows if stable_id(x)}
for x in home_rows:
    home_id = stable_id(x)
    if home_id and video_ids and home_id not in video_ids:
        fail(f'Dashboard Home video not present in final video library: {home_id}')
    video_url = str(x.get('video_url') or x.get('preview_url') or x.get('download_url') or '').strip()
    thumb = str(x.get('thumbnail_url') or x.get('poster_url') or '').strip()
    if not video_url:
        fail(f'Dashboard Home video lacks URL: {home_id}')
    if not thumb or VIDEO_EXT_RE.search(thumb):
        fail(f'Dashboard Home video lacks image thumbnail: {home_id}')

print('PASS: Saved Work internal Story/Fusion artifacts absent')
print('PASS: standalone Face/Audio contracts preserved')
print(f'PASS: final video library count={len(video_rows)}')
print(f'PASS: Dashboard Recent Videos count={len(home_rows)}')
print('PASS: returned final videos carry image thumbnails')
PY

echo "PASS: Dashboard/Saved Work customer-media contract certified"
