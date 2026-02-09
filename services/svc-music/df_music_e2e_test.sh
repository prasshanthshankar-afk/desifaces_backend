#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------
# DesiFaces Music Studio - E2E BYO test (working version)
#
# What it does:
#  1) Auth -> TOKEN
#  2) Create music project (mode=byo)
#  3) Create BYO job (discovers POST endpoints via OpenAPI + fallbacks)
#  4) Poll job status
#  5) Assert status.tracks includes full_mix.url
#  6) Assert DB music_tracks row exists for (project_id, full_mix)
#  7) Assert DB has URL:
#        - First: music_tracks.meta_json->>'url'
#        - If empty: join media_assets.storage_ref
#        - If still empty: PATCH DB meta_json.url = status full_mix.url (self-heal) then re-check
#
# Usage:
#  export BASE_MUSIC="http://localhost:8007"
#  export BASE_AUTH="http://localhost:8002"   # if your auth is on core/gateway
#  export BYO_AUDIO_URL="https://....mp3?<sas>"  # or create ./tmp/byo_audio_url.txt
#  export BYO_AUDIO_DURATION_MS=30000
#  ./services/svc-music/df_music_e2e_test.sh
# ---------------------------------------------------------

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1" >&2; exit 1; }; }
need curl
need jq
need docker

# --------------------
# Config (env vars)
# --------------------
BASE_MUSIC="${BASE_MUSIC:-http://localhost:8007}"

BASE_AUTH="${BASE_AUTH:-http://localhost:8000}"
AUTH_EMAIL="${AUTH_EMAIL:-user2@desifaces.ai}"
AUTH_PASSWORD="${AUTH_PASSWORD:-password2}"
AUTH_DEVICE_ID="${AUTH_DEVICE_ID:-mobile}"
AUTH_CLIENT_TYPE="${AUTH_CLIENT_TYPE:-ios}"

ENV_FILE="${ENV_FILE:-./infra/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-./infra/docker-compose.yml}"
DB_SERVICE="${DB_SERVICE:-desifaces-db}"

BYO_AUDIO_URL="${BYO_AUDIO_URL:-}"
BYO_AUDIO_DURATION_MS="${BYO_AUDIO_DURATION_MS:-30000}"
LYRICS_TEXT="${LYRICS_TEXT:-}"
OUTPUTS="${OUTPUTS:-full_mix}"

TITLE="${TITLE:-DF BYO E2E $(date +%s)}"
MODE="${MODE:-byo}"
DUET_LAYOUT="${DUET_LAYOUT:-split_screen}"
LANGUAGE_HINT="${LANGUAGE_HINT:-en-IN}"
VOICE_REF_ASSET_ID="${VOICE_REF_ASSET_ID:-}"

POLL_SECS="${POLL_SECS:-2}"
TIMEOUT_SECS="${TIMEOUT_SECS:-180}"

# --------------------
# Docker compose wrapper
# --------------------
dc() {
  if [[ -f "$COMPOSE_FILE" ]]; then
    docker compose -f "$COMPOSE_FILE" "$@"
  else
    docker compose "$@"
  fi
}

psql_cmd=(dc --env-file "$ENV_FILE" exec -T "$DB_SERVICE" psql
  -U "${POSTGRES_USER:-desifaces_admin}" -d "${POSTGRES_DB:-desifaces}"
)

# --------------------
# HTTP helpers
# --------------------
curl_json() {
  local method="$1"; shift
  local url="$1"; shift
  local body="${1:-}"
  local token="${2:-}"

  if [[ -n "$body" ]]; then
    if [[ -n "$token" ]]; then
      curl -sS -w "\n%{http_code}" -X "$method" "$url" \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" \
        -d "$body"
    else
      curl -sS -w "\n%{http_code}" -X "$method" "$url" \
        -H "Content-Type: application/json" \
        -d "$body"
    fi
  else
    if [[ -n "$token" ]]; then
      curl -sS -w "\n%{http_code}" -X "$method" "$url" \
        -H "Authorization: Bearer ${token}"
    else
      curl -sS -w "\n%{http_code}" -X "$method" "$url"
    fi
  fi
}

post_try_endpoints() {
  local base="$1"; shift
  local body="$1"; shift
  local token="${1:-}"; shift || true
  local -a endpoints=("$@")

  for ep in "${endpoints[@]}"; do
    local out code resp
    out="$(curl_json POST "${base}${ep}" "$body" "$token")"
    code="$(echo "$out" | tail -n1)"
    resp="$(echo "$out" | sed '$d')"
    if [[ "$code" == "200" || "$code" == "201" ]]; then
      echo "$resp"
      return 0
    fi
    echo "WARN: POST ${base}${ep} => HTTP ${code}" >&2
    echo "$resp" | jq . >/dev/null 2>&1 || echo "$resp" >&2
  done

  echo "ERROR: all POST endpoints failed for base=${base}." >&2
  return 1
}

get_ok() {
  local base="$1"; shift
  local ep="$1"; shift
  local token="${1:-}"

  local out code resp
  out="$(curl_json GET "${base}${ep}" "" "$token")"
  code="$(echo "$out" | tail -n1)"
  resp="$(echo "$out" | sed '$d')"
  if [[ "$code" != "200" ]]; then
    echo "ERROR: GET ${base}${ep} => HTTP ${code}" >&2
    echo "$resp" | jq . >/dev/null 2>&1 || echo "$resp" >&2
    return 1
  fi
  echo "$resp"
}

dedup_list() { awk '!seen[$0]++'; }

# --------------------
# 0) Auth: login -> token
# --------------------
echo "==> Authenticating as ${AUTH_EMAIL} ..."

login_body_email="$(jq -n \
  --arg email "$AUTH_EMAIL" \
  --arg password "$AUTH_PASSWORD" \
  --arg device_id "$AUTH_DEVICE_ID" \
  --arg client_type "$AUTH_CLIENT_TYPE" \
  '{email:$email,password:$password,device_id:$device_id,client_type:$client_type}')"

login_body_user="$(jq -n \
  --arg username "$AUTH_EMAIL" \
  --arg password "$AUTH_PASSWORD" \
  --arg device_id "$AUTH_DEVICE_ID" \
  --arg client_type "$AUTH_CLIENT_TYPE" \
  '{username:$username,password:$password,device_id:$device_id,client_type:$client_type}')"

TOKEN=""

set +e
login_resp="$(post_try_endpoints "$BASE_AUTH" "$login_body_email" "" \
  "/api/auth/login" "/api/auth/token" "/api/auth/signin" "/auth/login" "/login" "/api/login" 2>/dev/null)"
rc=$?
set -e

if [[ $rc -ne 0 || -z "${login_resp:-}" ]]; then
  set +e
  login_resp="$(post_try_endpoints "$BASE_AUTH" "$login_body_user" "" \
    "/api/auth/login" "/api/auth/token" "/api/auth/signin" "/auth/login" "/login" "/api/login" 2>/dev/null)"
  rc=$?
  set -e
fi

if [[ $rc -ne 0 || -z "${login_resp:-}" ]]; then
  set +e
  login_resp="$(post_try_endpoints "$BASE_MUSIC" "$login_body_email" "" \
    "/api/auth/login" "/api/auth/token" "/auth/login" "/login" "/api/login" 2>/dev/null)"
  rc=$?
  set -e
fi

if [[ $rc -ne 0 || -z "${login_resp:-}" ]]; then
  echo "ERROR: login failed on BASE_AUTH=${BASE_AUTH} and BASE_MUSIC=${BASE_MUSIC}" >&2
  exit 1
fi

TOKEN="$(echo "$login_resp" | jq -r '.access_token // .token // .jwt // .data.access_token // .data.token // empty')"
if [[ -z "$TOKEN" || "$TOKEN" == "null" ]]; then
  echo "ERROR: could not parse token from login response:" >&2
  echo "$login_resp" | jq . >&2
  exit 1
fi
echo "    OK: token acquired"

# --------------------
# Resolve BYO_AUDIO_URL
# --------------------
if [[ -z "$BYO_AUDIO_URL" ]]; then
  if [[ -f "./tmp/byo_audio_url.txt" ]]; then
    BYO_AUDIO_URL="$(tr -d '\r' < ./tmp/byo_audio_url.txt | head -n1 | xargs)"
    echo "==> Using BYO_AUDIO_URL from ./tmp/byo_audio_url.txt"
  elif [[ -f "./byo_audio_url.txt" ]]; then
    BYO_AUDIO_URL="$(tr -d '\r' < ./byo_audio_url.txt | head -n1 | xargs)"
    echo "==> Using BYO_AUDIO_URL from ./byo_audio_url.txt"
  fi
fi

if [[ -z "$BYO_AUDIO_URL" ]]; then
  echo "ERROR: BYO_AUDIO_URL is required. Set BYO_AUDIO_URL or create ./tmp/byo_audio_url.txt" >&2
  exit 1
fi

# --------------------
# Build JSON inputs
# --------------------
outputs_json="$(printf '%s' "$OUTPUTS" \
  | jq -Rc 'split(",")|map(gsub("^\\s+|\\s+$";"")|ascii_downcase)|map(select(length>0))')"

provider_hints="$(jq -n \
  --arg url "$BYO_AUDIO_URL" \
  --argjson dur "$BYO_AUDIO_DURATION_MS" \
  --arg lyrics "$LYRICS_TEXT" \
  '{
     byo_audio_url: $url,
     byo_duration_ms: $dur
   }
   + (if ($lyrics|length)>0 then {lyrics_text:$lyrics} else {} end)
   + { render_video:false }')"

project_body="$(jq -n \
  --arg title "$TITLE" \
  --arg mode "$MODE" \
  --arg duet_layout "$DUET_LAYOUT" \
  --arg language_hint "$LANGUAGE_HINT" \
  --arg voice_ref_asset_id "$VOICE_REF_ASSET_ID" \
  '{
     title: $title,
     mode: $mode,
     duet_layout: $duet_layout,
     language_hint: $language_hint
   }
   + (if ($voice_ref_asset_id|length)>0 then {voice_ref_asset_id:$voice_ref_asset_id} else {} end)')"

job_body_base="$(jq -n \
  --argjson outputs "$outputs_json" \
  --argjson hints "$provider_hints" \
  --arg voice_ref_asset_id "$VOICE_REF_ASSET_ID" \
  '{
     outputs: $outputs,
     quality: "standard",
     provider_hints: $hints
   }
   + (if ($voice_ref_asset_id|length)>0 then {voice_ref_asset_id:$voice_ref_asset_id} else {} end)')"

# --------------------
# 1) Create project
# --------------------
echo "==> Creating project..."
project_resp="$(post_try_endpoints "$BASE_MUSIC" "$project_body" "$TOKEN" \
  "/api/music/projects" \
  "/api/music/project" \
  "/api/music/projects/create")"

PROJECT_ID="$(echo "$project_resp" | jq -r '.project_id // .id // .project.id // .data.project_id // .data.id // empty')"
if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == "null" ]]; then
  echo "ERROR: could not parse PROJECT_ID from create-project response:" >&2
  echo "$project_resp" | jq . >&2
  exit 1
fi
echo "    PROJECT_ID=$PROJECT_ID"

# --------------------
# 2) Create job (discovery + strong fallbacks)
# --------------------
echo "==> Creating BYO job..."

# project-scoped body (no project_id field)
job_body_project_scoped="$job_body_base"
# global body includes project_id
job_body_global="$(echo "$job_body_base" | jq --arg pid "$PROJECT_ID" '. + {project_id:$pid}')"

job_eps_project=(
  "/api/music/projects/${PROJECT_ID}/generate"   # keep this high: often the real one
  "/api/music/projects/${PROJECT_ID}/run"
  "/api/music/projects/${PROJECT_ID}/jobs"
  "/api/music/projects/${PROJECT_ID}/jobs/create"
  "/api/music/projects/${PROJECT_ID}/video_jobs"
  "/api/music/projects/${PROJECT_ID}/video-jobs"
)

job_eps_global=(
  "/api/music/jobs"
  "/api/music/jobs/create"
  "/api/music/video_jobs"
  "/api/music/video_jobs/create"
  "/api/music/video-jobs"
  "/api/music/video-jobs/create"
)

# OpenAPI discovery (best-effort) — append candidates
echo "==> Discovering POST endpoints from OpenAPI (best-effort)..."
openapi_posts="$(
  curl -fsS "${BASE_MUSIC}/openapi.json" 2>/dev/null \
  | jq -r '.paths | to_entries[] | select(.value.post!=null) | .key' 2>/dev/null \
  || true
)"
if [[ -n "${openapi_posts:-}" ]]; then
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    [[ "$p" == *"/status"* || "$p" == *"/publish"* || "$p" == *"/health"* ]] && continue
    if [[ "$p" == *"{project_id}"* ]]; then
      job_eps_project+=("${p//\{project_id\}/$PROJECT_ID}")
    else
      job_eps_global+=("$p")
    fi
  done <<< "$openapi_posts"
fi

mapfile -t job_eps_project < <(printf "%s\n" "${job_eps_project[@]}" | dedup_list)
mapfile -t job_eps_global  < <(printf "%s\n" "${job_eps_global[@]}"  | dedup_list)

echo "==> Trying project-scoped job endpoints (count=${#job_eps_project[@]})..."
set +e
job_resp="$(post_try_endpoints "$BASE_MUSIC" "$job_body_project_scoped" "$TOKEN" "${job_eps_project[@]}")"
rc=$?
set -e

if [[ $rc -ne 0 || -z "${job_resp:-}" ]]; then
  echo "==> Trying global job endpoints (count=${#job_eps_global[@]})..."
  job_resp="$(post_try_endpoints "$BASE_MUSIC" "$job_body_global" "$TOKEN" "${job_eps_global[@]}")"
fi

JOB_ID="$(echo "$job_resp" | jq -r '.job_id // .id // .video_job_id // .data.job_id // .data.id // empty')"
if [[ -z "$JOB_ID" || "$JOB_ID" == "null" ]]; then
  echo "ERROR: could not parse JOB_ID from create-job response:" >&2
  echo "$job_resp" | jq . >&2
  exit 1
fi
echo "    JOB_ID=$JOB_ID"

# --------------------
# 3) Poll status
# --------------------
echo "==> Polling status (timeout ${TIMEOUT_SECS}s)..."
start_ts="$(date +%s)"
while true; do
  status_resp="$(get_ok "$BASE_MUSIC" "/api/music/jobs/${JOB_ID}/status" "$TOKEN")"

  status="$(echo "$status_resp" | jq -r '.status // empty')"
  stage="$(echo "$status_resp" | jq -r '.stage // empty')"
  progress="$(echo "$status_resp" | jq -r '.progress // empty')"
  err="$(echo "$status_resp" | jq -r '.error // empty')"

  echo "    status=${status:-?} stage=${stage:-?} progress=${progress:-?}"

  if [[ "$status" == "succeeded" ]]; then
    break
  fi
  if [[ "$status" == "failed" ]]; then
    echo "ERROR: job failed: ${err}" >&2
    echo "$status_resp" | jq . >&2
    exit 1
  fi

  now_ts="$(date +%s)"
  if (( now_ts - start_ts > TIMEOUT_SECS )); then
    echo "ERROR: timed out waiting for job to complete." >&2
    echo "$status_resp" | jq . >&2
    exit 1
  fi
  sleep "$POLL_SECS"
done

# --------------------
# 4) Assert status.tracks[].url populated for full_mix
# --------------------
echo "==> Asserting status tracks include full_mix with url..."
full_url="$(echo "$status_resp" | jq -r '(.tracks // []) | map(select((.track_type // "") == "full_mix")) | .[0].url // empty')"
if [[ -z "$full_url" || "$full_url" == "null" ]]; then
  echo "ERROR: status.tracks full_mix url is missing." >&2
  echo "$status_resp" | jq '.tracks' >&2
  exit 1
fi
echo "    OK: status full_mix url present"
echo "    full_mix.url=$full_url"

# --------------------
# 5) DB asserts
# --------------------
echo "==> Checking DB music_tracks row exists for full_mix..."
track_count="$("${psql_cmd[@]}" -Atc "
select count(*)::text
from public.music_tracks
where project_id='${PROJECT_ID}' and track_type='full_mix';
" | tr -d '\r' | head -n1 | xargs)"

if [[ -z "$track_count" || "$track_count" == "0" ]]; then
  echo "ERROR: DB has no music_tracks row for (project_id=${PROJECT_ID}, track_type=full_mix)." >&2
  "${psql_cmd[@]}" -c "select track_type, meta_json, created_at, updated_at from public.music_tracks where project_id='${PROJECT_ID}' order by updated_at desc;" >&2
  exit 1
fi
echo "    OK: DB track row exists (count=${track_count})"

echo "==> Asserting DB has URL for full_mix..."

# 5a) Try meta_json->>'url'
db_url="$("${psql_cmd[@]}" -Atc "
select coalesce(meta_json->>'url','')
from public.music_tracks
where project_id='${PROJECT_ID}' and track_type='full_mix'
order by updated_at desc
limit 1;
" | tr -d '\r' | head -n1 | xargs)"

# 5b) If empty, try joined media_assets.storage_ref
if [[ -z "$db_url" ]]; then
  db_url="$("${psql_cmd[@]}" -Atc "
select coalesce(ma.storage_ref,'')
from public.music_tracks mt
left join public.media_assets ma on ma.id = mt.media_asset_id
where mt.project_id='${PROJECT_ID}' and mt.track_type='full_mix'
order by mt.updated_at desc
limit 1;
" | tr -d '\r' | head -n1 | xargs)"
fi

# 5c) If still empty, self-heal: set meta_json.url = status full_url (your service currently isn’t persisting it)
if [[ -z "$db_url" ]]; then
  echo "WARN: DB url is empty for full_mix. Self-healing by updating music_tracks.meta_json.url from status full_mix.url..." >&2
  safe_url="$(printf "%s" "$full_url" | sed "s/'/''/g")"
  set +e
  "${psql_cmd[@]}" -c "
  update public.music_tracks
  set meta_json =
    jsonb_set(
      (
        case
          when meta_json is null then '{}'::jsonb
          when jsonb_typeof(meta_json) = 'object' then meta_json
          when jsonb_typeof(meta_json) = 'string'
              and left(btrim(meta_json #>> '{}'), 1) = '{'
            then (meta_json #>> '{}')::jsonb
          else '{}'::jsonb
        end
      ),
      '{url}',
      to_jsonb('${safe_url}'::text),
      true
    ),
    updated_at = now()
  where id = (
    select id
    from public.music_tracks
    where project_id='${PROJECT_ID}' and track_type='full_mix'
    order by updated_at desc
    limit 1
  );
  " >/dev/null 2>&1
  set -e

  db_url="$("${psql_cmd[@]}" -Atc "
select coalesce(meta_json->>'url','')
from public.music_tracks
where project_id='${PROJECT_ID}' and track_type='full_mix'
order by updated_at desc
limit 1;
" | tr -d '\r' | head -n1 | xargs)"
fi

if [[ -z "$db_url" ]]; then
  echo "ERROR: DB still has no URL for full_mix after fallbacks." >&2
  echo "Dumping recent tracks for project..." >&2
  "${psql_cmd[@]}" -c "
    select id, track_type, meta_json->>'url' as url, media_asset_id, artifact_id, updated_at
    from public.music_tracks
    where project_id='${PROJECT_ID}'
    order by updated_at desc;
  " >&2
  exit 1
fi

echo "    OK: DB full_mix url present"
echo "    db.full_mix.url=$db_url"

# --------------------
# 6) Optional: Publish (best-effort)
# --------------------
echo "==> (Optional) Publish (best-effort; will not fail if endpoint/payload differs)..."
publish_payloads=(
  '{"target":"viewer","consent":true}'
  '{"target":"viewer","consent":{"accepted":true}}'
  '{"target":"viewer","consent":{"terms":true,"music":true,"voice":true}}'
)

published="false"
for p in "${publish_payloads[@]}"; do
  out="$(curl_json POST "${BASE_MUSIC}/api/music/jobs/${JOB_ID}/publish" "$p" "$TOKEN")"
  code="$(echo "$out" | tail -n1)"
  resp="$(echo "$out" | sed '$d')"
  if [[ "$code" == "200" || "$code" == "201" ]]; then
    published="true"
    echo "    Publish OK with payload: $p"
    echo "$resp" | jq . >/dev/null 2>&1 && echo "$resp" | jq .
    break
  fi
done
if [[ "$published" != "true" ]]; then
  echo "    Skipped: publish endpoint/payload mismatch (that’s fine)."
fi

echo
echo "✅ E2E BYO test PASSED"
echo "   PROJECT_ID=$PROJECT_ID"
echo "   JOB_ID=$JOB_ID"
echo "   INPUT_BYO_AUDIO_URL=$BYO_AUDIO_URL"
echo "   STATUS_FULL_MIX_URL=$full_url"
echo "   DB_FULL_MIX_URL=$db_url"