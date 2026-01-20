#!/usr/bin/env bash
set -euo pipefail

# df_face_e2e.sh — End-to-end test for svc-face Creator Platform (T2I + I2I)
# Usage:
#   ./df_face_e2e.sh t2i
#   ./df_face_e2e.sh i2i
#
# Examples:
#   ./df_face_e2e.sh t2i
#
#   ./df_face_e2e.sh i2i \
#     SOURCE_IMAGE_URL="https://.../img.jpg?<sas>" \
#     USER_PROMPT="EDIT THE INPUT PHOTO: keep the SAME person/identity. Change outfit to a traditional Bengali look: red saree with gold border, gold jewelry, red bindi. Studio portrait lighting, photorealistic." \
#     PRESERVATION_STRENGTH=0.85
#
# Notes:
# - Optional config codes are included ONLY if provided:
#   USE_CASE_CODE, IMAGE_FORMAT_CODE, AGE_RANGE_CODE, SKIN_TONE_CODE, REGION_CODE
# - Gender policy:
#     * T2I: gender is INCLUDED (default "female" if not set)
#     * I2I: gender is OMITTED by default (backend should infer from source image)
#            Override with SEND_GENDER_I2I=1 to force include gender
# - Smoke-check: HEAD (or Range GET fallback) on artifact URLs; warns on 403, fails on 404/000/etc.

TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(TS)] $*"; }
die() { log "ERROR: $*"; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

need_cmd curl
need_cmd jq

: "${CORE_BASE:=http://localhost:8000}"
: "${FACE_BASE:=http://localhost:8003}"

EMAIL="${EMAIL:-user1@desifaces.ai}"
PASSWORD="${PASSWORD:-password1}"

# Mode can be passed as positional arg OR env MODE
MODE_RAW="${1:-${MODE:-t2i}}"
shift || true

# Allow passing KEY=VALUE after the mode (so multi-line usage works)
for kv in "$@"; do
  if [[ "$kv" == *=* ]]; then
    k="${kv%%=*}"
    v="${kv#*=}"
    export "$k=$v"
  else
    die "Unexpected arg '$kv' (expected KEY=VALUE)"
  fi
done

NUM_VARIANTS="${NUM_VARIANTS:-2}"

# Gender policy
GENDER="${GENDER:-}"                    # female|male|person
SEND_GENDER_I2I="${SEND_GENDER_I2I:-0}" # 0 omit gender in i2i, 1 include

# Optional config codes
USE_CASE_CODE="${USE_CASE_CODE:-}"
IMAGE_FORMAT_CODE="${IMAGE_FORMAT_CODE:-}"
AGE_RANGE_CODE="${AGE_RANGE_CODE:-}"
SKIN_TONE_CODE="${SKIN_TONE_CODE:-}"
REGION_CODE="${REGION_CODE:-}"

# I2I inputs
SOURCE_IMAGE_URL="${SOURCE_IMAGE_URL:-}"
PRESERVATION_STRENGTH="${PRESERVATION_STRENGTH:-0.25}"

# Prompt
USER_PROMPT="${USER_PROMPT:-confident entrepreneur headshot, studio lighting}"

POLL_INTERVAL_SECS="${POLL_INTERVAL_SECS:-2}"
TIMEOUT_SECS="${TIMEOUT_SECS:-600}"

SMOKE_CHECK="${SMOKE_CHECK:-1}"
HEAD_TIMEOUT_SECS="${HEAD_TIMEOUT_SECS:-15}"
HEAD_MAX_REDIRECTS="${HEAD_MAX_REDIRECTS:-3}"

WORKDIR="${WORKDIR:-/tmp/df_e2e_$(date +%s)}"
mkdir -p "$WORKDIR"

normalize_mode() {
  local m="${1:-}"
  m="$(echo "$m" | tr '[:upper:]' '[:lower:]' | tr '_' '-' | xargs)"
  case "$m" in
    i2i|image-to-image|img2img) echo "image-to-image" ;;
    t2i|text-to-image|txt2img|"") echo "text-to-image" ;;
    *) echo "unknown" ;;
  esac
}

MODE_NORM="$(normalize_mode "$MODE_RAW")"
[[ "$MODE_NORM" == "unknown" ]] && die "Invalid MODE='$MODE_RAW'. Use: t2i|i2i|text-to-image|image-to-image"

# Enforce/default gender for T2I
if [[ "$MODE_NORM" == "text-to-image" ]]; then
  [[ -z "${GENDER}" ]] && GENDER="female"
fi

log "============================================================"
log "DesiFaces Face Studio E2E"
log "CORE_BASE=${CORE_BASE}"
log "FACE_BASE=${FACE_BASE}"
log "MODE_RAW=${MODE_RAW}"
log "MODE_NORM=${MODE_NORM}"
log "EMAIL=${EMAIL}"
log "NUM_VARIANTS=${NUM_VARIANTS}"
log "WORKDIR=${WORKDIR}"
log "SMOKE_CHECK=${SMOKE_CHECK} (HEAD)"
if [[ "$MODE_NORM" == "text-to-image" ]]; then
  log "GENDER=${GENDER} (included for T2I)"
else
  log "SEND_GENDER_I2I=${SEND_GENDER_I2I} (0 omit gender; 1 include gender)"
fi
log "============================================================"

# Validate i2i inputs early
if [[ "$MODE_NORM" == "image-to-image" ]]; then
  [[ -z "$SOURCE_IMAGE_URL" ]] && die "MODE=i2i requires SOURCE_IMAGE_URL env var"
  if ! jq -e -n --arg s "$PRESERVATION_STRENGTH" '($s|tonumber) >= 0' >/dev/null 2>&1; then
    die "PRESERVATION_STRENGTH must be a number (got '$PRESERVATION_STRENGTH')"
  fi
fi

# ------------------------
# [1/6] Login
# ------------------------
log "[1/6] Login to CORE auth..."
LOGIN_RESP="$WORKDIR/login.json"

curl -sS -X POST "${CORE_BASE}/api/auth/login" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg email "$EMAIL" --arg password "$PASSWORD" '{email:$email, password:$password}')" \
  > "$LOGIN_RESP"

TOKEN="$(jq -r '.access_token // .token // .data.access_token // .data.token // empty' "$LOGIN_RESP")"
if [[ -z "${TOKEN}" ]]; then
  log "Login response:"
  cat "$LOGIN_RESP" >&2 || true
  die "login_failed (saved: $LOGIN_RESP)"
fi
AUTH_HEADER="Authorization: Bearer ${TOKEN}"
log "Login OK. Token acquired."

# ------------------------
# [2/6] Build request payload
# ------------------------
log "[2/6] Build create-job request payload..."
REQ_JSON="$WORKDIR/create_job_request.json"

jq -n \
  --arg language "en" \
  --arg user_prompt "$USER_PROMPT" \
  --arg gender "$GENDER" \
  --argjson send_gender_i2i "$SEND_GENDER_I2I" \
  --argjson num_variants "${NUM_VARIANTS}" \
  --arg use_case_code "$USE_CASE_CODE" \
  --arg image_format_code "$IMAGE_FORMAT_CODE" \
  --arg age_range_code "$AGE_RANGE_CODE" \
  --arg skin_tone_code "$SKIN_TONE_CODE" \
  --arg region_code "$REGION_CODE" \
  --arg mode "$MODE_NORM" \
  --arg source_image_url "$SOURCE_IMAGE_URL" \
  --argjson preservation_strength "$PRESERVATION_STRENGTH" \
  '
  def add_if($cond; $obj): if $cond then $obj else {} end;

  (
    {
      language: $language,
      user_prompt: $user_prompt,
      num_variants: $num_variants
    }

    # gender policy:
    + add_if(($mode == "text-to-image"); {gender: $gender})
    + add_if(($mode == "image-to-image") and ($send_gender_i2i == 1); {gender: $gender})

    # optional config codes
    + add_if(($use_case_code|length) > 0; {use_case_code: $use_case_code})
    + add_if(($image_format_code|length) > 0; {image_format_code: $image_format_code})
    + add_if(($age_range_code|length) > 0; {age_range_code: $age_range_code})
    + add_if(($skin_tone_code|length) > 0; {skin_tone_code: $skin_tone_code})
    + add_if(($region_code|length) > 0; {region_code: $region_code})

    # mode block
    + add_if(($mode == "image-to-image");
        {mode: "image-to-image", source_image_url: $source_image_url, preservation_strength: $preservation_strength}
      )
    + add_if(($mode == "text-to-image");
        {mode: "text-to-image"}
      )
  )
  ' > "$REQ_JSON"

log "Request payload saved: $REQ_JSON"
log "Payload preview:"
jq '.' "$REQ_JSON" | sed -e 's/^/  /'

# ------------------------
# [3/6] Submit job
# ------------------------
log "[3/6] Submit job to svc-face..."
CREATE_RESP="$WORKDIR/create_job_response.json"

curl -sS -X POST "${FACE_BASE}/api/face/creator/generate" \
  -H "Content-Type: application/json" \
  -H "$AUTH_HEADER" \
  -d @"$REQ_JSON" \
  > "$CREATE_RESP"

JOB_ID="$(jq -r '.job_id // empty' "$CREATE_RESP")"
if [[ -z "$JOB_ID" ]]; then
  log "Create-job response:"
  cat "$CREATE_RESP" >&2 || true
  die "create_job_failed (saved: $CREATE_RESP)"
fi

log "Job created: $JOB_ID"
log "Create response saved: $CREATE_RESP"

# ------------------------
# [4/6] Poll status
# ------------------------
log "[4/6] Poll status until terminal state..."
STATUS_JSON="$WORKDIR/status_${JOB_ID}.json"
start_ts="$(date +%s)"

while true; do
  curl -sS "${FACE_BASE}/api/face/creator/jobs/${JOB_ID}/status" \
    -H "$AUTH_HEADER" > "$STATUS_JSON"

  status="$(jq -r '.status // "unknown"' "$STATUS_JSON")"

  now="$(date +%s)"
  elapsed="$((now - start_ts))"
  log "Job ${JOB_ID} status=${status} (elapsed ${elapsed}s)"

  if [[ "$status" == "succeeded" || "$status" == "failed" || "$status" == "cancelled" ]]; then
    break
  fi

  if (( elapsed > TIMEOUT_SECS )); then
    die "timeout waiting for job (>${TIMEOUT_SECS}s). Last status: $STATUS_JSON"
  fi

  sleep "$POLL_INTERVAL_SECS"
done

log "Final status saved: $STATUS_JSON"

# ------------------------
# [5/6] Artifacts summary
# ------------------------
log "[5/6] Artifacts summary:"
ARTIFACTS_JSON="$WORKDIR/artifacts_${JOB_ID}.json"

jq -r '
  (.variants // [])[]
  | "variant=\(.variant_number)  url=\(.image_url // .url // "")  face_profile_id=\(.face_profile_id // "")  media_asset_id=\(.media_asset_id // "")"
' "$STATUS_JSON" | tee "$ARTIFACTS_JSON" >/dev/null || true

log "Artifacts list saved: $ARTIFACTS_JSON"

# Also print prompt_used (variant 1) if present
pu="$(jq -r '(.variants // [])[0].prompt_used // empty' "$STATUS_JSON" 2>/dev/null || true)"
if [[ -n "$pu" ]]; then
  log "Prompt used (variant 1):"
  echo "  $pu"
fi

# ------------------------
# [6/6] Smoke-check URLs (HEAD)
# ------------------------
head_status_for_url() {
  local url="$1"
  if [[ -z "$url" ]]; then
    echo "000 -"
    return 0
  fi

  local out http_code effective_url
  out="$(curl -sS -I -L \
    --max-redirs "$HEAD_MAX_REDIRECTS" \
    --connect-timeout "$HEAD_TIMEOUT_SECS" \
    --max-time "$HEAD_TIMEOUT_SECS" \
    -o /dev/null \
    -w '%{http_code} %{url_effective}' \
    "$url" 2>/dev/null || true)"

  http_code="$(echo "$out" | awk '{print $1}')"
  effective_url="$(echo "$out" | awk '{print $2}')"

  if [[ "$http_code" == "405" || "$http_code" == "000" ]]; then
    out="$(curl -sS -L \
      --max-redirs "$HEAD_MAX_REDIRECTS" \
      --connect-timeout "$HEAD_TIMEOUT_SECS" \
      --max-time "$HEAD_TIMEOUT_SECS" \
      -H 'Range: bytes=0-0' \
      -o /dev/null \
      -w '%{http_code} %{url_effective}' \
      "$url" 2>/dev/null || true)"
    http_code="$(echo "$out" | awk '{print $1}')"
    effective_url="$(echo "$out" | awk '{print $2}')"
  fi

  echo "${http_code:-000} ${effective_url:-$url}"
}

if [[ "$SMOKE_CHECK" == "1" ]]; then
  log "[6/6] Smoke-check: verifying artifact URLs reachable (HEAD)..."

  SMOKE_JSON="$WORKDIR/smoke_${JOB_ID}.jsonl"
  : > "$SMOKE_JSON"

  mapfile -t URLS < <(jq -r '(.variants // [])[] | (.image_url // .url // "")' "$STATUS_JSON" | sed '/^$/d')

  if [[ "${#URLS[@]}" -eq 0 ]]; then
    log "Smoke-check: no URLs found in status response."
  else
    ok=0; warn=0; fail=0; idx=0

    for u in "${URLS[@]}"; do
      idx=$((idx + 1))
      read -r code eff <<<"$(head_status_for_url "$u")"

      jq -n --arg url "$u" --arg effective_url "$eff" --arg http_code "$code" \
        '{url:$url, effective_url:$effective_url, http_code:($http_code|tonumber? // $http_code)}' \
        >> "$SMOKE_JSON" || true

      if [[ "$code" == "200" || "$code" == "206" ]]; then
        log "Smoke-check [${idx}/${#URLS[@]}] OK   ${code}  ${u}"
        ok=$((ok + 1))
      elif [[ "$code" == "403" ]]; then
        log "Smoke-check [${idx}/${#URLS[@]}] WARN ${code}  ${u}"
        log "  -> Looks private / missing SAS. If expected public, check container ACL or ensure SAS is included."
        warn=$((warn + 1))
      elif [[ "$code" == "404" ]]; then
        log "Smoke-check [${idx}/${#URLS[@]}] FAIL ${code}  ${u}"
        log "  -> Not found. Check Azure upload path or artifact URL persistence."
        fail=$((fail + 1))
      elif [[ "$code" == "000" ]]; then
        log "Smoke-check [${idx}/${#URLS[@]}] FAIL ${code}  ${u}"
        log "  -> Network/timeout/DNS. Try: curl -I '$u'"
        fail=$((fail + 1))
      else
        log "Smoke-check [${idx}/${#URLS[@]}] WARN ${code}  ${u}"
        warn=$((warn + 1))
      fi
    done

    log "Smoke-check results: OK=${ok} WARN=${warn} FAIL=${fail}"
    log "Smoke-check details saved: $SMOKE_JSON"

    if (( fail > 0 )); then
      log "Smoke-check: hard failures detected (FAIL=${fail})."
      exit 3
    fi
  fi
else
  log "[6/6] Smoke-check disabled (SMOKE_CHECK=0)"
fi

final_status="$(jq -r '.status // "unknown"' "$STATUS_JSON")"
if [[ "$final_status" == "failed" ]]; then
  err="$(jq -r '.error // .message // .detail // empty' "$STATUS_JSON")"
  log "Failure reason: ${err:-<none>}"
  exit 2
fi

log "Saved outputs in $WORKDIR"
log "DONE ✅  (${MODE_NORM})"