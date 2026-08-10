#!/usr/bin/env bash
set -euo pipefail

# DesiFaces dashboard/video proof script.
# Runs against the deployed API and proves whether dashboard home, Saved Work all,
# and Saved Work videos are returning child longform segment renders.
#
# Usage:
#   export DF_EMAIL='user_android_iap_test1@desifaces.ai'
#   export DF_PASSWORD='...'
#   export CORE_URL='https://api.desifaces.ai/core'
#   export DASHBOARD_URL='https://api.desifaces.ai/dashboard'
#   bash df_dashboard_video_final_proof.sh

: "${DF_EMAIL:?Set DF_EMAIL}"
: "${DF_PASSWORD:?Set DF_PASSWORD}"
CORE_URL="${CORE_URL:-https://api.desifaces.ai/core}"
DASHBOARD_URL="${DASHBOARD_URL:-https://api.desifaces.ai/dashboard}"
LOGIN_PATH="${LOGIN_PATH:-/api/auth/login}"
TMP_DIR="${TMP_DIR:-/tmp/df_dashboard_video_proof}"
mkdir -p "$TMP_DIR"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing required command: $1" >&2; exit 2; }
}
need curl
need jq
need python3

login_payload="$TMP_DIR/login_payload.json"
login_response="$TMP_DIR/login_response.json"
cat > "$login_payload" <<JSON
{"email":"$DF_EMAIL","password":"$DF_PASSWORD"}
JSON

login_url="${CORE_URL%/}${LOGIN_PATH}"
echo "== Login: $login_url"
http_code=$(curl -sS -o "$login_response" -w '%{http_code}' \
  -X POST "$login_url" \
  -H 'Content-Type: application/json' \
  -d @"$login_payload")

if [[ "$http_code" -lt 200 || "$http_code" -ge 300 ]]; then
  echo "Login failed HTTP $http_code"
  cat "$login_response" | jq . 2>/dev/null || cat "$login_response"
  exit 1
fi

TOKEN=$(jq -r '.access_token // .token // .data.access_token // .data.token // .auth.access_token // empty' "$login_response")
USER_ID=$(jq -r '.user_id // .user.id // .data.user_id // .data.user.id // empty' "$login_response")

if [[ -z "$TOKEN" || "$TOKEN" == "null" ]]; then
  echo "Could not extract token from login response"
  cat "$login_response" | jq .
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
  echo "Could not determine USER_ID from response/JWT"
  cat "$login_response" | jq .
  exit 1
fi

echo "== User"
echo "email=$DF_EMAIL"
echo "user_id=$USER_ID"

fetch_dashboard() {
  local name="$1"
  local path="$2"
  local out="$TMP_DIR/$name.json"
  local url="${DASHBOARD_URL%/}$path"
  local code
  code=$(curl -sS -o "$out" -w '%{http_code}' \
    "$url" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-User-Id: $USER_ID" \
    -H 'X-Country-Code: US')
  echo "$name HTTP $code $url"
  if [[ "$code" -lt 200 || "$code" -ge 300 ]]; then
    cat "$out" | jq . 2>/dev/null || cat "$out"
    exit 1
  fi
}

fetch_dashboard home '/api/dashboard/home'
fetch_dashboard library_all '/api/dashboard/library?type=all&limit=100&offset=0&final_only=1&exclude_child_segments=1&library_scope=final_outputs'
fetch_dashboard library_video '/api/dashboard/library?type=video&limit=100&offset=0&final_only=1&exclude_child_segments=1&library_scope=final_outputs'

python3 - "$TMP_DIR" <<'PY'
import json, pathlib, re, sys
root = pathlib.Path(sys.argv[1])

CHILD_RE = re.compile(r'internal_child|child_render|child_job_of_billable_longform_parent|"suppress_pricing"\s*:\s*true|"pricing_suppressed"\s*:\s*true|"segment_id"\s*:', re.I)
FINAL_RE = re.compile(r'share_url|final_video|final_output|final_video_url|stitched_video|composed_video|timeline_output|"render_kind"\s*:\s*"final"|"output_role"\s*:\s*"final"', re.I)


def load(name):
    with open(root / f'{name}.json', 'r', encoding='utf-8') as f:
        return json.load(f)

def as_items(payload, source):
    if source == 'home_video_carousel':
        return payload.get('video_carousel') or []
    return payload.get('items') or payload.get('assets') or payload.get('data') or []

def item_id(x, idx):
    return str(x.get('source_job_id') or x.get('library_id') or x.get('artifact_id') or x.get('media_asset_id') or idx)

def summarize(name, items):
    videos = [x for x in items if str(x.get('studio') or x.get('asset_type') or '').lower() in ('video', 'fusion') or name == 'home.video_carousel']
    child = [x for x in videos if CHILD_RE.search(json.dumps(x, default=str))]
    final = [x for x in videos if FINAL_RE.search(json.dumps(x, default=str))]
    print(f'\n== {name}')
    print(f'items={len(items)} videos={len(videos)} child_hits={len(child)} final_marker_hits={len(final)}')
    for label, rows in [('child_sample', child[:5]), ('video_sample', videos[:5])]:
        if rows:
            print(f'-- {label}')
            for i, x in enumerate(rows):
                print(json.dumps({
                    'id': item_id(x, i),
                    'title': x.get('title'),
                    'created_at': x.get('created_at'),
                    'studio': x.get('studio'),
                    'asset_type': x.get('asset_type'),
                    'preview_url_present': bool(x.get('preview_url')),
                    'download_url_present': bool(x.get('download_url')),
                    'source_job_id': x.get('source_job_id'),
                }, default=str))
    return videos, child, final

home = load('home')
allp = load('library_all')
vidp = load('library_video')

home_videos, home_child, home_final = summarize('home.video_carousel', as_items(home, 'home_video_carousel'))
all_videos, all_child, all_final = summarize('library.type_all', as_items(allp, 'library'))
video_videos, video_child, video_final = summarize('library.type_video', as_items(vidp, 'library'))

all_ids = {item_id(x, i) for i, x in enumerate(all_videos)}
video_ids = {item_id(x, i) for i, x in enumerate(video_videos)}
missing_from_all = sorted(video_ids - all_ids)

print('\n== Cross-check')
print(f'video_ids_missing_from_type_all={len(missing_from_all)}')
if missing_from_all[:10]:
    print('missing_sample=' + ','.join(missing_from_all[:10]))

print('\n== Diagnosis')
if video_child:
    print('FAIL: Saved Work video endpoint is still returning child/segment renders. svc-dashboard backend patch is not deployed, not active, or Saved Work is hitting a different endpoint/service.')
elif not video_videos:
    print('WARN: Saved Work video endpoint returns zero videos. Children may be hidden, but final longform outputs are not exposed by the dashboard asset source.')
else:
    print('PASS: Saved Work video endpoint has no child markers in returned videos.')

if not home_videos and video_videos:
    print('FAIL: Dashboard Recent Videos is using home/video_carousel or type=all data that does not include the same displayable videos as type=video. Fix frontend to merge a dedicated type=video fetch, or fix backend type=all to match type=video.')
elif home_child:
    print('FAIL: Dashboard home video_carousel still returns child markers.')
else:
    print('PASS/WARN: Dashboard home has no child video markers; if empty, it is hiding children but missing final videos.')

if missing_from_all:
    print('FAIL: /api/dashboard/library?type=all is inconsistent with ?type=video. Dashboard currently fetches type=all, so Recent Videos can show empty while Saved Work Videos shows rows.')
PY

echo "\nRaw responses saved under $TMP_DIR"
