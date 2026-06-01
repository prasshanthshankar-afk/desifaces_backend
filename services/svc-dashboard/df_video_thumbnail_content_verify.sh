#!/usr/bin/env bash
set -euo pipefail

: "${DF_EMAIL:=user_android_iap_test1@desifaces.ai}"
: "${DF_PASSWORD:?Set DF_PASSWORD first}"
: "${CORE_URL:=https://api.desifaces.ai/core}"
: "${DASHBOARD_URL:=https://api.desifaces.ai/dashboard}"

WORK=/tmp/df_video_thumbnail_content_verify_v7
mkdir -p "$WORK"
LOGIN_JSON="$WORK/login.json"
HOME_JSON="$WORK/home.json"
SAVED_JSON="$WORK/saved_video.json"

curl -sS -X POST "$CORE_URL/api/auth/login" -H "Content-Type: application/json" -d "{\"email\":\"$DF_EMAIL\",\"password\":\"$DF_PASSWORD\"}" > "$LOGIN_JSON"
TOKEN="$(jq -r '.access_token // .token // .data.access_token // empty' "$LOGIN_JSON")"
USER_ID="$(jq -r '.user.id // .user_id // .data.user.id // .data.user_id // empty' "$LOGIN_JSON")"

curl -sS "$DASHBOARD_URL/api/dashboard/home" -H "Authorization: Bearer $TOKEN" -H "X-User-Id: $USER_ID" > "$HOME_JSON"
curl -sS "$DASHBOARD_URL/api/dashboard/library?type=video&limit=100&offset=0&final_only=1&exclude_child_segments=1&library_scope=final_outputs" -H "Authorization: Bearer $TOKEN" -H "X-User-Id: $USER_ID" > "$SAVED_JSON"

jq '{
  home_video: ((.video_carousel // [])[0] | {id:(.library_id // .id), title, thumbnail_url, video_url, preview_url, download_url}),
}' "$HOME_JSON" 2>/dev/null || true

HOME_THUMB="$(jq -r '(.video_carousel // [])[0].thumbnail_url // (.video_carousel // [])[0].poster_url // empty' "$HOME_JSON")"
SAVED_THUMB="$(jq -r '(.items // [])[0].thumbnail_url // (.items // [])[0].poster_url // empty' "$SAVED_JSON")"

check_url() {
  local label="$1" url="$2"
  echo "== $label =="
  if [ -z "$url" ]; then
    echo "missing"
    return 1
  fi
  echo "${url%%\?*}?..."
  local out="$WORK/${label}.bin"
  curl -L -sS --fail -o "$out" -D "$WORK/${label}.headers" "$url"
  grep -i '^content-type:' "$WORK/${label}.headers" || true
  file "$out"
  if file "$out" | grep -Eiq 'JPEG image|PNG image|Web/P image|RIFF.*Web/P'; then
    echo "PASS_IMAGE"
  else
    echo "FAIL_NOT_IMAGE"
    return 1
  fi
}

check_url home_thumbnail "$HOME_THUMB"
check_url saved_thumbnail "$SAVED_THUMB"

echo "Raw JSON saved in $WORK"
