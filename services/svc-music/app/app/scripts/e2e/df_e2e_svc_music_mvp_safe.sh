#!/usr/bin/env bash
# File: services/svc-music/app/app/scripts/e2e/df_e2e_svc_music_mvp_safe.sh
set -Eeuo pipefail
IFS=$'\n\t'

# -----------------------------
# Terminal safety guarantee
# -----------------------------
# - We NEVER pipe curl -> python (avoids curl(23)/broken pipe).
# - All stdout/stderr go to a log file.
# - Only say() prints short sanitized ASCII lines to /dev/tty.
# -----------------------------

# -----------------------------
# Terminal safety guarantee
# -----------------------------
# We NEVER pipe curl -> python (avoids curl(23)/broken pipe).
#
# export DF_EMAIL="user2@desifaces.ai"
# export DF_PASSWORD="password2"
# export RUN_MODES="autopilot,co_create"
# export VOICE_REF="./tmp/voice_ref.mp3"

# the user's "intent"
export INTENT_TEXT="Create a 25–30 sec upbeat promo track for DesiFaces demo. Modern pop + desi percussion, catchy hook, high energy."
export OUTPUTS="full_mix"
export LYRICS_SOURCE="generate"


# -------- user-configurable env (defaults) --------
: "${MUSIC_BASE_URL:=http://localhost:8007}"
: "${CORE_BASE_URL:=http://localhost:8000}"
: "${ENV_FILE:=./infra/.env}"                      # set ENV_FILE="" to disable
: "${RUN_MODES:=autopilot,co_create}"              # autopilot,co_create,byo
: "${DUET_LAYOUT:=split_screen}"                   # split_screen|alternating|same_stage
: "${LANGUAGE_HINT:=en}"
: "${CAMERA_EDIT:=beat_cut}"                       # smooth|beat_cut|aggressive
: "${SCENE_PACK_ID:=}"                             # optional
: "${BAND_PACK:=}"                                 # optional comma list, e.g. "tabla,drums"

# auth
: "${TOKEN:=}"
: "${MUSIC_TOKEN:=}"                               # alias
: "${DF_EMAIL:=}"
: "${DF_PASSWORD:=}"
: "${AUTH_ENDPOINTS:=/api/auth/login /api/auth/signin /api/auth/token /api/login /auth/login}"

# generation (GenerateMusicIn)
: "${INTENT_TEXT:=Create a 25–30 sec upbeat promo track for DesiFaces demo. Modern pop + desi percussion, catchy hook, high energy.}"
: "${GENRE_HINT:=pop}"
: "${VIBE_HINT:=energetic}"
: "${LYRICS_SOURCE:=generate}"                     # generate|upload|none
: "${LYRICS_TEXT:=}"                               # required if LYRICS_SOURCE=upload
: "${LYRICS_LANGUAGE_HINT:=en}"
: "${QUALITY:=standard}"                           # draft|standard|pro
: "${SEED:=12345}"
: "${OUTPUTS:=full_mix}"                           # comma list; will be coerced to JSON array

# co_create voice reference
: "${VOICE_REF:=}"                                 # file path (mp3/wav)
: "${VOICE_REF_MIME:=}"                            # override, else guessed
: "${VOICE_REF_MAX_BYTES:=10485760}"               # 10MB safety

# byo (schema supports uploaded_audio_url or uploaded_audio_asset_id; no upload endpoint guaranteed)
: "${BYO_UPLOADED_AUDIO_URL:=}"
: "${BYO_UPLOADED_AUDIO_ASSET_ID:=}"

# polling + publish
: "${MAX_WAIT_SECS:=600}"
: "${POLL_SECS:=2}"
: "${PUBLISH_TARGET:=fusion}"                      # fusion|viewer
: "${PUBLISH_CONSENT_ACCEPTED:=true}"              # JSON boolean

# -------- run dir + logs --------
ts() { date +"%Y-%m-%d_%H-%M-%S"; }

make_run_dir() {
  local d
  d="${RUN_DIR:-/tmp/df_music_e2e_$(ts)}"
  if mkdir -p "$d" 2>/dev/null; then
    echo "$d"
    return 0
  fi
  d="./tmp/df_music_e2e_$(ts)"
  mkdir -p "$d"
  echo "$d"
}

RUN_DIR="$(make_run_dir)"
LOG="$RUN_DIR/e2e.log"
: >"$LOG"
exec >>"$LOG" 2>&1

say() {
  local msg="$*"
  printf "%s\n" "$msg" | LC_ALL=C sed 's/[^[:print:]\t]//g' | tee /dev/tty >/dev/null
}

die() {
  say "ERROR: $*"
  say "Run dir: $RUN_DIR"
  say "Log: $LOG"
  exit 1
}

on_err() {
  local code=$?
  say "FAILED (exit=$code)."
  say "Run dir: $RUN_DIR"
  say "Log: $LOG"
  exit $code
}
trap on_err ERR

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

preflight() {
  need_cmd curl
  need_cmd python3
  need_cmd sed
  need_cmd tee
  need_cmd wc
}

# -------- optional env file --------
load_env_file() {
  [[ -z "${ENV_FILE:-}" ]] && return 0
  [[ ! -f "$ENV_FILE" ]] && return 0
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE" || true
  set +a
}

# -------- safe curl wrappers (no pipes) --------
curl_json() {
  # usage: curl_json METHOD URL DATAFILE OUTFILE
  local method="$1" url="$2" data="${3:-}" out="${4:-}"
  [[ -n "$out" ]] || out="$RUN_DIR/resp_$(date +%s%N).json"
  local http="000"
  if [[ -n "$data" ]]; then
    http=$(curl -sS -o "$out" -w "%{http_code}" \
      --connect-timeout 5 --max-time 120 --retry 2 --retry-connrefused \
      -X "$method" "$url" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      --data @"$data" || true)
  else
    http=$(curl -sS -o "$out" -w "%{http_code}" \
      --connect-timeout 5 --max-time 120 --retry 2 --retry-connrefused \
      -X "$method" "$url" \
      -H "Authorization: Bearer $TOKEN" || true)
  fi
  echo "$http $out"
}

curl_multipart_file() {
  # usage: curl_multipart_file URL FILEPATH MIMETYPE FILENAME OUTFILE
  local url="$1" file="$2" mime="$3" fname="$4" out="$5"
  local http="000"
  http=$(curl -sS -o "$out" -w "%{http_code}" \
    --connect-timeout 5 --max-time 120 --retry 2 --retry-connrefused \
    -X POST "$url" \
    -H "Authorization: Bearer $TOKEN" \
    -F "file=@${file};type=${mime};filename=${fname}" || true)
  echo "$http $out"
}

json_get() {
  # usage: json_get FILE key
  local file="$1" key="$2"
  python3 - <<'PY' "$file" "$key"
import json,sys
p,k=sys.argv[1],sys.argv[2]
try:
  j=json.load(open(p,"r",encoding="utf-8"))
  v=j.get(k)
  print(v if isinstance(v,(str,int,float)) and v is not None else "")
except Exception:
  print("")
PY
}

truncate() {
  local s="$1" n="${2:-160}"
  if (( ${#s} > n )); then
    echo "${s:0:n}..."
  else
    echo "$s"
  fi
}

# -------- token handling --------
resolve_token() {
  if [[ -n "${MUSIC_TOKEN:-}" ]]; then TOKEN="$MUSIC_TOKEN"; fi
  if [[ -n "${TOKEN:-}" ]]; then return 0; fi

  [[ -n "${DF_EMAIL:-}" && -n "${DF_PASSWORD:-}" ]] || die "Missing TOKEN. Set TOKEN or set DF_EMAIL/DF_PASSWORD."
  say "TOKEN not set; attempting login via svc-core at $CORE_BASE_URL ..."

  local payload="$RUN_DIR/login.json"
  local out="$RUN_DIR/auth.json"
  cat >"$payload" <<JSON
{"email":"$DF_EMAIL","password":"$DF_PASSWORD"}
JSON

  local ep http token=""
  for ep in $AUTH_ENDPOINTS; do
    http=$(curl -sS -o "$out" -w "%{http_code}" \
      --connect-timeout 5 --max-time 60 \
      -X POST "$CORE_BASE_URL$ep" \
      -H "Content-Type: application/json" \
      --data @"$payload" || true)
    if [[ "$http" == "200" || "$http" == "201" ]]; then
      token=$(python3 - <<'PY' "$out"
import json,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
for k in ("access_token","token","jwt"):
  v=j.get(k)
  if isinstance(v,str) and v:
    print(v); raise SystemExit(0)
raise SystemExit(1)
PY
) || true
      if [[ -n "$token" ]]; then
        TOKEN="$token"
        printf "%s" "$TOKEN" >"$RUN_DIR/token.txt"
        say "Auth OK via $ep"
        return 0
      fi
    fi
  done

  die "Login failed. Check svc-core auth endpoint or credentials."
}

# -------- svc-music health --------
health_check() {
  local out="$RUN_DIR/health.json"
  local http
  http=$(curl -sS -o "$out" -w "%{http_code}" "$MUSIC_BASE_URL/api/health" || true)
  [[ "$http" == "200" ]] || die "svc-music health failed (HTTP $http). See $out"
}

# -------- build JSON payloads --------
create_project_payload() {
  local mode="$1" out="$2"
  python3 - <<'PY' "$mode" "$out"
import json,sys
mode,out=sys.argv[1],sys.argv[2]
payload={
  "title": f"E2E {mode} project",
  "mode": mode,
  "duet_layout": __import__("os").environ.get("DUET_LAYOUT","split_screen"),
  "language_hint": __import__("os").environ.get("LANGUAGE_HINT","en"),
  "camera_edit": __import__("os").environ.get("CAMERA_EDIT","beat_cut"),
}
scene=__import__("os").environ.get("SCENE_PACK_ID","").strip()
if scene: payload["scene_pack_id"]=scene
band=__import__("os").environ.get("BAND_PACK","").strip()
if band:
  payload["band_pack"]=[x.strip() for x in band.split(",") if x.strip()]
json.dump(payload, open(out,"w",encoding="utf-8"))
PY
}

generate_payload() {
  local mode="$1" out="$2"
  python3 - <<'PY' "$mode" "$out"
import json,sys,os
mode,out=sys.argv[1],sys.argv[2]

outputs=[x.strip() for x in os.environ.get("OUTPUTS","full_mix").split(",") if x.strip()]

payload={
  "seed": int(os.environ.get("SEED","12345")),
  "quality": os.environ.get("QUALITY","standard"),
  "outputs": outputs,
  "provider_hints": {},
  "track_prompt": os.environ.get("INTENT_TEXT",""),
  "genre_hint": os.environ.get("GENRE_HINT",""),
  "vibe_hint": os.environ.get("VIBE_HINT",""),
  "lyrics_source": os.environ.get("LYRICS_SOURCE","generate"),
  "lyrics_text": os.environ.get("LYRICS_TEXT","") or None,
  "lyrics_language_hint": os.environ.get("LYRICS_LANGUAGE_HINT","en"),
}

# BYO mode support per schema (no upload endpoint guaranteed)
if mode=="byo":
  aid=os.environ.get("BYO_UPLOADED_AUDIO_ASSET_ID","").strip() or None
  url=os.environ.get("BYO_UPLOADED_AUDIO_URL","").strip() or None
  payload["uploaded_audio_asset_id"]=aid
  payload["uploaded_audio_url"]=url

# drop empty strings -> null for nicer API
for k in ("track_prompt","genre_hint","vibe_hint","lyrics_language_hint"):
  if payload.get(k)=="":
    payload[k]=None

# if lyrics_source=upload, lyrics_text must be present
if payload.get("lyrics_source")=="upload" and not (payload.get("lyrics_text") or "").strip():
  raise SystemExit("ERROR: LYRICS_SOURCE=upload requires LYRICS_TEXT to be set")

json.dump(payload, open(out,"w",encoding="utf-8"))
PY
}

publish_payload() {
  local out="$1"
  cat >"$out" <<JSON
{"target":"$PUBLISH_TARGET","consent":{"accepted":${PUBLISH_CONSENT_ACCEPTED}}}
JSON
}

# -------- flow --------
create_project() {
  local mode="$1"
  local payload="$RUN_DIR/create_project_${mode}.json"
  create_project_payload "$mode" "$payload"

  local out="$RUN_DIR/create_project_${mode}_resp.json"
  local http resp
  resp="$(curl_json "POST" "$MUSIC_BASE_URL/api/music/projects" "$payload" "$out")"
  http="${resp%% *}"

  if [[ "$http" == "401" ]]; then
    # token invalid; retry login if possible
    if [[ -n "${DF_EMAIL:-}" && -n "${DF_PASSWORD:-}" ]]; then
      say "Got 401 on create_project; re-authenticating..."
      TOKEN=""
      resolve_token
      resp="$(curl_json "POST" "$MUSIC_BASE_URL/api/music/projects" "$payload" "$out")"
      http="${resp%% *}"
    fi
  fi

  [[ "$http" == "200" || "$http" == "201" ]] || die "Create project failed (HTTP $http). See $out"

  local pid
  pid="$(json_get "$out" "project_id")"
  [[ -n "$pid" ]] || pid="$(json_get "$out" "id")"
  [[ -n "$pid" ]] || die "Create project response missing project_id/id. See $out"

  say "Project created: mode=$mode pid=$pid"
  echo "$pid"
}

upload_voice_ref() {
  local pid="$1"
  [[ -n "${VOICE_REF:-}" ]] || { say "VOICE_REF not set; skipping voice reference upload."; return 0; }
  [[ -f "$VOICE_REF" ]] || die "VOICE_REF not found: $VOICE_REF"

  local bytes
  bytes=$(wc -c <"$VOICE_REF" 2>/dev/null || echo 0)
  if [[ "$bytes" -gt "$VOICE_REF_MAX_BYTES" ]]; then
    die "VOICE_REF too large (${bytes} bytes). Reduce size or raise VOICE_REF_MAX_BYTES."
  fi

  local mime="$VOICE_REF_MIME"
  if [[ -z "$mime" ]]; then
    if command -v file >/dev/null 2>&1; then
      mime=$(file --mime-type -b "$VOICE_REF" 2>/dev/null || true)
    fi
    [[ -n "$mime" ]] || mime="audio/mpeg"
  fi

  local out="$RUN_DIR/voice_ref_resp.json"
  local http
  say "Uploading voice reference: $(truncate "$VOICE_REF" 120) (mime=$mime bytes=$bytes)"
  read -r http _ < <(curl_multipart_file "$MUSIC_BASE_URL/api/music/projects/$pid/voice-reference" "$VOICE_REF" "$mime" "voice_ref.mp3" "$out")

  [[ "$http" == "200" || "$http" == "201" ]] || die "Voice ref upload failed (HTTP $http). See $out"
  say "Voice reference uploaded."
}

create_job() {
  local pid="$1" mode="$2"
  local payload="$RUN_DIR/generate_${mode}.json"
  generate_payload "$mode" "$payload"

  local out="$RUN_DIR/generate_${mode}_resp.json"
  local http
  http=$(curl -sS -o "$out" -w "%{http_code}" \
    --connect-timeout 5 --max-time 120 --retry 2 --retry-connrefused \
    -X POST "$MUSIC_BASE_URL/api/music/projects/$pid/generate" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    --data @"$payload" || true)

  [[ "$http" == "200" || "$http" == "201" ]] || die "Generate failed (HTTP $http). See $out (payload=$payload)"
  local jid
  jid="$(json_get "$out" "job_id")"
  [[ -n "$jid" ]] || jid="$(json_get "$out" "id")"
  [[ -n "$jid" ]] || die "Generate response missing job_id/id. See $out"
  say "Job created: mode=$mode jid=$jid"
  echo "$jid"
}

poll_job() {
  local jid="$1"
  local start now
  start=$(date +%s)

  while true; do
    local out="$RUN_DIR/status_${jid}.json"
    local http
    http=$(curl -sS -o "$out" -w "%{http_code}" \
      --connect-timeout 5 --max-time 60 --retry 2 --retry-connrefused \
      -H "Authorization: Bearer $TOKEN" \
      "$MUSIC_BASE_URL/api/music/jobs/$jid/status" || true)
    [[ "$http" == "200" ]] || die "Status failed (HTTP $http). See $out"

    local status stage
    status=$(python3 - <<'PY' "$out"
import json,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
print(j.get("status",""))
PY
)
    stage=$(python3 - <<'PY' "$out"
import json,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
print(j.get("stage",""))
PY
)
    say "  status=$status stage=$stage"

    [[ "$status" == "succeeded" ]] && return 0
    [[ "$status" == "failed" ]] && die "Job failed. See $out"

    now=$(date +%s)
    if (( now - start > MAX_WAIT_SECS )); then
      die "Timed out waiting for job $jid"
    fi
    sleep "$POLL_SECS"
  done
}

publish_job() {
  local jid="$1"
  local payload="$RUN_DIR/publish_${jid}.json"
  publish_payload "$payload"

  local out="$RUN_DIR/publish_${jid}_resp.json"
  local http
  http=$(curl -sS -o "$out" -w "%{http_code}" \
    --connect-timeout 5 --max-time 120 --retry 2 --retry-connrefused \
    -X POST "$MUSIC_BASE_URL/api/music/jobs/$jid/publish" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    --data @"$payload" || true)

  [[ "$http" == "200" || "$http" == "201" ]] || die "Publish failed (HTTP $http). See $out"

  # Print short summary (truncate long SAS URLs to keep terminal safe)
  python3 - <
::contentReference[oaicite:0]{index=0}