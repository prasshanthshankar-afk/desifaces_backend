#!/usr/bin/env bash
set -euo pipefail
: "${DF_EMAIL:?set DF_EMAIL}"
: "${DF_PASSWORD:?set DF_PASSWORD}"
: "${CORE_URL:=https://api.desifaces.ai/core}"
: "${DASHBOARD_URL:=https://api.desifaces.ai/dashboard}"

TMP_DIR="/tmp/df_dashboard_video_v6_thumbnail_verify"
mkdir -p "$TMP_DIR"

login_json=$(curl -sS -X POST "$CORE_URL/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$DF_EMAIL\",\"password\":\"$DF_PASSWORD\"}")

echo "$login_json" > "$TMP_DIR/login.json"
TOKEN=$(jq -r '.access_token // .token // .jwt // empty' "$TMP_DIR/login.json")
USER_ID=$(jq -r '.user.id // .user_id // .id // .sub // empty' "$TMP_DIR/login.json")
if [[ -z "$TOKEN" || -z "$USER_ID" ]]; then
  echo "Login failed or token/user_id missing" >&2
  cat "$TMP_DIR/login.json" >&2
  exit 1
fi

echo "email=$DF_EMAIL"
echo "user_id=$USER_ID"

fetch_json() {
  local name="$1"
  local url="$2"
  local out="$TMP_DIR/$name.json"
  local code
  code=$(curl -sS -w '%{http_code}' -o "$out" "$url" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-User-Id: $USER_ID")
  echo "$name HTTP $code $url"
  if [[ "$code" != "200" ]]; then
    cat "$out" >&2
    exit 1
  fi
}

fetch_json home "$DASHBOARD_URL/api/dashboard/home"
fetch_json library_all "$DASHBOARD_URL/api/dashboard/library?type=all&limit=100&offset=0&final_only=1&exclude_child_segments=1&library_scope=final_outputs"
fetch_json library_video "$DASHBOARD_URL/api/dashboard/library?type=video&limit=100&offset=0&final_only=1&exclude_child_segments=1&library_scope=final_outputs"

jq_check='def arr: if type == "array" then . elif has("items") then .items elif has("video_carousel") then .video_carousel else [] end;
def videoish: map(select(((.studio // .asset_type // "") | ascii_downcase | test("video|fusion")) or (.video_url? != null) or (.reuse_payload.video_url? != null)));
def child: tostring | test("internal_child|child_render|child_job_of_billable_longform_parent|pricing_suppressed|suppress_pricing|segment_id");
def finalish: tostring | test("share_url|final_video|final_output|stitched_video|composed_video|timeline_output|render_kind.*final|output_role.*final|longform_jobs");
(arr | videoish) as $v | {
  total_items: (arr | length),
  video_items: ($v | length),
  child_hits: ($v | map(select(child)) | length),
  final_hits: ($v | map(select(finalish)) | length),
  thumbnail_present: ($v | map(select((.thumbnail_url // .poster_url // .reuse_payload.thumbnail_url // .reuse_payload.poster_url // .meta.thumbnail_url // .meta.poster_url // "") != "")) | length),
  sample: ($v | map({id:(.library_id // .id), title, studio, asset_type, source_job_id, thumbnail_url_present:((.thumbnail_url // .poster_url // .reuse_payload.thumbnail_url // .reuse_payload.poster_url // .meta.thumbnail_url // .meta.poster_url // "") != ""), preview_url_present:((.preview_url // "") != ""), download_url_present:((.download_url // "") != ""), video_url_present:((.video_url // .reuse_payload.video_url // .meta.video_url // "") != "")}) | .[0:5])
}'

echo
echo "== home.video_carousel =="
jq '.video_carousel // []' "$TMP_DIR/home.json" | jq "$jq_check"
echo
echo "== library.type_all =="
jq "$jq_check" "$TMP_DIR/library_all.json"
echo
echo "== library.type_video =="
jq "$jq_check" "$TMP_DIR/library_video.json"

echo
echo "Raw JSON saved in $TMP_DIR"
