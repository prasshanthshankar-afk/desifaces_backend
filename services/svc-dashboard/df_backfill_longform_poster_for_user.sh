#!/usr/bin/env bash
set -euo pipefail

: "${DF_EMAIL:=user_android_iap_test1@desifaces.ai}"
: "${DF_PASSWORD:?Set DF_PASSWORD first}"
: "${CORE_URL:=https://api.desifaces.ai/core}"
: "${DASHBOARD_URL:=https://api.desifaces.ai/dashboard}"
: "${DASHBOARD_CONTAINER:=df-svc-dashboard}"
: "${DB_CONTAINER:=desifaces-db}"
: "${DB_USER:=desifaces_admin}"
: "${DB_NAME:=desifaces}"

WORK="/tmp/df_longform_poster_backfill"
mkdir -p "$WORK"
LOGIN_JSON="$WORK/login.json"
VIDEO_JSON="$WORK/library_video.json"
MP4="$WORK/final_video.mp4"
POSTER="$WORK/final_video_poster.jpg"
UPLOAD_RESULT="$WORK/upload_result.json"

curl -sS -X POST "$CORE_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$DF_EMAIL\",\"password\":\"$DF_PASSWORD\"}" > "$LOGIN_JSON"

TOKEN="$(jq -r '.access_token // .token // .data.access_token // empty' "$LOGIN_JSON")"
USER_ID="$(jq -r '.user.id // .user_id // .data.user.id // .data.user_id // empty' "$LOGIN_JSON")"

if [ -z "$TOKEN" ] || [ -z "$USER_ID" ]; then
  echo "Login failed or token/user_id not found"
  cat "$LOGIN_JSON"
  exit 1
fi

echo "email=$DF_EMAIL"
echo "user_id=$USER_ID"

curl -sS "$DASHBOARD_URL/api/dashboard/library?type=video&limit=10&offset=0&final_only=1&exclude_child_segments=1&library_scope=final_outputs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-User-Id: $USER_ID" > "$VIDEO_JSON"

ITEM_JSON="$(
  jq -c '
    (.items // [])
    | map(
        select(
          ((.thumbnail_url // "") | length) == 0
          and
          ((.source_job_id // "") | length) > 0
          and
          (
            ((.reuse_payload.video_url // "") | length) > 0
            or ((.video_url // "") | length) > 0
            or ((.preview_url // "") | length) > 0
            or ((.download_url // "") | length) > 0
          )
        )
      )
    | .[0] // empty
  ' "$VIDEO_JSON"
)"

JOB_ID="$(
  echo "$ITEM_JSON" |
  jq -r '.source_job_id // empty'
)"

VIDEO_URL="$(
  echo "$ITEM_JSON" |
  jq -r '
    .reuse_payload.video_url
    // .video_url
    // .preview_url
    // .download_url
    // empty
  '
)"

if [ -z "$JOB_ID" ] || [ -z "$VIDEO_URL" ]; then
  echo "No final video row found in dashboard library response."
  jq . "$VIDEO_JSON"
  exit 1
fi

echo "longform_job_id=$JOB_ID"
echo "Downloading final video..."
curl -L --fail --retry 3 -o "$MP4" "$VIDEO_URL"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required on the host to extract a poster frame. Install ffmpeg or run this from a host/container with ffmpeg."
  exit 1
fi

echo "Extracting poster frame..."
ffmpeg -y -ss 00:00:01 -i "$MP4" -frames:v 1 -vf "scale=720:-2" -q:v 3 "$POSTER" >/tmp/df_ffmpeg_poster.log 2>&1 || {
  echo "ffmpeg failed; log follows:"
  cat /tmp/df_ffmpeg_poster.log
  exit 1
}

file "$POSTER"

CONTAINER_POSTER="/tmp/${JOB_ID}_poster.jpg"
docker cp "$POSTER" "$DASHBOARD_CONTAINER:$CONTAINER_POSTER"

cat > "$WORK/upload_poster.py" <<'PY'
import json
import os
import sys
from azure.storage.blob import BlobServiceClient, ContentSettings

if len(sys.argv) != 3:
    raise SystemExit("usage: upload_poster.py <job_id> <poster_path>")
job_id = sys.argv[1]
poster_path = sys.argv[2]
conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
if not conn:
    raise SystemExit("AZURE_STORAGE_CONNECTION_STRING is not set in dashboard container")
container_name = os.environ.get("AZURE_VIDEO_OUTPUT_CONTAINER", "video-output")
blob_name = f"longform/posters/{job_id}.jpg"
service = BlobServiceClient.from_connection_string(conn)
blob = service.get_blob_client(container=container_name, blob=blob_name)
with open(poster_path, "rb") as f:
    blob.upload_blob(
        f,
        overwrite=True,
        content_settings=ContentSettings(content_type="image/jpeg"),
    )
account_name = service.account_name
url = f"https://{account_name}.blob.core.windows.net/{container_name}/{blob_name}"
print(json.dumps({"container": container_name, "blob": blob_name, "url": url}, sort_keys=True))
PY

docker cp "$WORK/upload_poster.py" "$DASHBOARD_CONTAINER:/tmp/upload_poster.py"
POSTER_JSON="$(docker exec "$DASHBOARD_CONTAINER" python /tmp/upload_poster.py "$JOB_ID" "$CONTAINER_POSTER")"
echo "$POSTER_JSON" | tee "$UPLOAD_RESULT"
POSTER_URL="$(echo "$POSTER_JSON" | jq -r '.url')"
POSTER_BLOB="$(echo "$POSTER_JSON" | jq -r '.blob')"

if [ -z "$POSTER_URL" ] || [ "$POSTER_URL" = "null" ]; then
  echo "Poster upload did not return a URL"
  exit 1
fi

echo "Updating longform_jobs.tags with poster URL..."
cat > "$WORK/update_poster.sql" <<SQL
UPDATE public.longform_jobs
SET tags = jsonb_set(
             jsonb_set(
               jsonb_set(
                 jsonb_set(
                   coalesce(tags, '{}'::jsonb),
                   '{thumbnail_url}', to_jsonb('$POSTER_URL'::text), true
                 ),
                 '{poster_url}', to_jsonb('$POSTER_URL'::text), true
               ),
               '{cover_url}', to_jsonb('$POSTER_URL'::text), true
             ),
             '{thumbnail_blob_path}', to_jsonb('$POSTER_BLOB'::text), true
           )
WHERE id = '$JOB_ID'::uuid
RETURNING id, status, tags->>'thumbnail_url' AS thumbnail_url, tags->>'poster_url' AS poster_url, tags->>'thumbnail_blob_path' AS thumbnail_blob_path;
SQL

docker cp "$WORK/update_poster.sql" "$DB_CONTAINER:/tmp/update_poster.sql"
docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -f /tmp/update_poster.sql

echo "Backfill complete. Poster URL: $POSTER_URL"
echo "Now restart svc-dashboard after applying dashboard_service v7, then run thumbnail verifier again."
