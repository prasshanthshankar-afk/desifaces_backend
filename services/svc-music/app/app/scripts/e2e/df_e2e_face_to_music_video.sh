#!/usr/bin/env bash
set -euo pipefail

# ==========================================================
# DesiFaces E2E: Face -> Music -> Publish -> svc-fusion-extension (Longform) -> Video
# Terminal-safe: NEVER dumps large JSON to terminal.
# All diagnostics saved under RUN_DIR.
# ==========================================================

# -----------------------------
# Config (override via env)
# -----------------------------
CORE_BASE="${CORE_BASE:-http://127.0.0.1:8000}"
FACE_BASE="${FACE_BASE:-http://127.0.0.1:8003}"
MUSIC_BASE="${MUSIC_BASE:-http://127.0.0.1:8007}"
FUSION_EXT_BASE="${FUSION_EXT_BASE:-http://127.0.0.1:8006}"   # svc-fusion-extension

MODE="${MODE:-autopilot}"          # autopilot | co_create | byo
QUALITY="${QUALITY:-standard}"     # draft | standard | pro
LANGUAGE_HINT="${LANGUAGE_HINT:-en}"

# India / regional controls
INDIA_PRESET="${INDIA_PRESET:-}"   # bollywood_pop | punjabi_bhangra | tamil_kuthu | telugu_mass | bengali_folk | marathi_lavani | malayalam_indie
LYRICS_LANG="${LYRICS_LANG:-}"     # hi | ta | te | kn | ml | bn | mr | pa
AUDIENCE="${AUDIENCE:-urban}"      # urban | rural
TRACK_PROMPT="${TRACK_PROMPT:-}"   # override prompt directly
GENRE_HINT="${GENRE_HINT:-}"       # override
VIBE_HINT="${VIBE_HINT:-}"         # override

# Auth
TOKEN="${TOKEN:-}"
EMAIL="${EMAIL:-}"
PASSWORD="${PASSWORD:-}"

# Performer knobs
USER_IS_OWNER="${USER_IS_OWNER:-false}"   # true|false
VOICE_MODE="${VOICE_MODE:-none}"          # uploaded|generated|none

# Face generation knobs
FACE_PROMPT="${FACE_PROMPT:-A photorealistic studio portrait, smiling, soft light, 50mm lens, high detail}"
FACE_N_VARIANTS="${FACE_N_VARIANTS:-1}"

# Optional pre-provided image asset/url
IMG_ASSET_ID="${IMG_ASSET_ID:-}"
IMG_URL="${IMG_URL:-}"

# BYO inputs (only for MODE=byo)
BYO_AUDIO_URL="${BYO_AUDIO_URL:-}"
BYO_AUDIO_ASSET_ID="${BYO_AUDIO_ASSET_ID:-}"

# Music outputs (valid: instrumental,vocals,full_mix,stems_zip,lyrics_json,timed_lyrics_json,cover_art)
MUSIC_OUTPUTS_JSON="${MUSIC_OUTPUTS_JSON:-}"

# Fusion-extension knobs (Longform)
ASPECT_RATIO="${ASPECT_RATIO:-9:16}"             # allowed: 16:9 | 9:16 | 1:1
SEGMENT_SECONDS="${SEGMENT_SECONDS:-10}"         # integer 1..120
MAX_SEGMENT_SECONDS="${MAX_SEGMENT_SECONDS:-20}" # integer 1..120 (>= SEGMENT_SECONDS)
VOICE_LOCALE="${VOICE_LOCALE:-en-US}"            # voice.locale
VOICE_GENDER_MODE="${VOICE_GENDER_MODE:-auto}"   # auto | manual
VOICE_GENDER="${VOICE_GENDER:-}"                 # male | female (if manual)
LONGFORM_SCRIPT="${LONGFORM_SCRIPT:-}"           # optional override script text
FUSION_DISABLE="${FUSION_DISABLE:-0}"            # 1 = skip fusion-extension submission/polling

# Timeouts
POLL_SECS="${POLL_SECS:-2}"
FACE_MAX_WAIT_SECS="${FACE_MAX_WAIT_SECS:-300}"
MUSIC_MAX_WAIT_SECS="${MUSIC_MAX_WAIT_SECS:-900}"
FUSION_MAX_WAIT_SECS="${FUSION_MAX_WAIT_SECS:-1200}"

RUN_DIR="${RUN_DIR:-/tmp/df_e2e_face_to_music_video_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$RUN_DIR"

HDR="$RUN_DIR/hdr.txt"
OUT="$RUN_DIR/out.json"

# Always-init globals (set -u safety)
PROJECT_ID=""
JOB_ID=""
FUSION_JOB_ID=""
VIDEO_URL=""

export CORE_BASE FACE_BASE MUSIC_BASE FUSION_EXT_BASE MODE QUALITY LANGUAGE_HINT TOKEN
export INDIA_PRESET LYRICS_LANG AUDIENCE TRACK_PROMPT GENRE_HINT VIBE_HINT
export FACE_PROMPT FACE_N_VARIANTS BYO_AUDIO_URL BYO_AUDIO_ASSET_ID
export USER_IS_OWNER VOICE_MODE MUSIC_OUTPUTS_JSON
export IMG_ASSET_ID IMG_URL RUN_DIR
export ASPECT_RATIO SEGMENT_SECONDS MAX_SEGMENT_SECONDS VOICE_LOCALE VOICE_GENDER_MODE VOICE_GENDER LONGFORM_SCRIPT
export FUSION_DISABLE

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
sanitize() { printf '%s' "${1:-}" | tr -d '[:space:]\r'; }

on_err() {
  local line="${1:-?}"
  log "ERROR at line $line"
  log "RUN_DIR=$RUN_DIR"
  if [[ -s "$OUT" ]]; then
    head -c 4000 "$OUT" > "$RUN_DIR/last_response_trunc.json" 2>/dev/null || true
    log "Saved response snippet: $RUN_DIR/last_response_trunc.json"
  fi
}
trap 'on_err $LINENO' ERR

require_cmds() {
  command -v curl >/dev/null || die "curl not found"
  command -v python3 >/dev/null || die "python3 not found"
}

http_json_allow_fail() {
  local method="$1"; shift
  local url="$1"; shift
  local data="${1:-}"

  : >"$HDR"; : >"$OUT"

  local code
  if [[ -n "$data" ]]; then
    code="$(curl -q -sS --max-time 180 --connect-timeout 6 \
      -D "$HDR" -o "$OUT" -w "%{http_code}" \
      -X "$method" "$url" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      --data "$data" || true)"
  else
    code="$(curl -q -sS --max-time 180 --connect-timeout 6 \
      -D "$HDR" -o "$OUT" -w "%{http_code}" \
      -X "$method" "$url" \
      -H "Authorization: Bearer $TOKEN" || true)"
  fi
  printf '%s' "$code"
}

http_get_allow_fail() {
  local url="$1"
  : >"$HDR"; : >"$OUT"
  local code
  code="$(curl -q -sS --max-time 60 --connect-timeout 4 \
    -D "$HDR" -o "$OUT" -w "%{http_code}" \
    -H "Authorization: Bearer $TOKEN" \
    "$url" || true)"
  printf '%s' "$code"
}

save_resp() {
  local file="$1"
  cp "$OUT" "$file" 2>/dev/null || true
}

py_get() {
  python3 - "$@" <<'PY'
import json,sys,re
p=sys.argv[1]
keys=sys.argv[2:]
j=json.load(open(p,"r",encoding="utf-8"))

def walk(x):
  if isinstance(x, dict):
    yield x
    for v in x.values(): yield from walk(v)
  elif isinstance(x, list):
    for v in x: yield from walk(v)

for obj in walk(j):
  if isinstance(obj, dict):
    for k in keys:
      if k in obj and obj[k] not in (None,""):
        print(obj[k]); sys.exit(0)

uuid_re=re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
for obj in walk(j):
  if isinstance(obj, dict):
    for v in obj.values():
      if isinstance(v,str) and uuid_re.match(v.strip()):
        print(v.strip()); sys.exit(0)
sys.exit(2)
PY
}

py_find_first_url() {
  python3 - "$1" <<'PY'
import json,re,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
def walk(x):
  if isinstance(x, dict):
    for v in x.values(): yield from walk(v)
  elif isinstance(x, list):
    for v in x: yield from walk(v)
  elif isinstance(x, str):
    yield x
img=re.compile(r"^https?://.*\.(png|jpg|jpeg|webp)(\?|$)", re.I)
for s in walk(j):
  s=s.strip()
  if s.startswith("http") and (img.search(s) or "blob.core.windows.net" in s):
    print(s); raise SystemExit
print("")
PY
}

py_extract_video_url_any() {
  python3 - "$1" <<'PY'
import json,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
def walk(x):
  if isinstance(x, dict):
    for k in ("final_video_url","video_url","mp4_url","result_url","output_url","url"):
      v=x.get(k)
      if isinstance(v,str) and v.startswith("http") and ".mp4" in v:
        return v
    for v in x.values():
      r=walk(v)
      if r: return r
  elif isinstance(x, list):
    for v in x:
      r=walk(v)
      if r: return r
  return ""
print(walk(j))
PY
}

py_json_contains_token() {
  python3 - "$1" "$2" <<'PY'
import json,sys
p=sys.argv[1]; needle=sys.argv[2]
try:
  j=json.load(open(p,"r",encoding="utf-8"))
except Exception:
  print("no"); raise SystemExit
def walk(x):
  if isinstance(x, dict):
    for v in x.values(): yield from walk(v)
  elif isinstance(x, list):
    for v in x: yield from walk(v)
  elif isinstance(x, str):
    yield x
for s in walk(j):
  if needle in s:
    print("yes"); raise SystemExit
print("no")
PY
}

# -----------------------------
# Preflight + Auth
# -----------------------------
preflight() {
  log "Preflight health checks..."
  curl -q -sS "$CORE_BASE/api/health" >/dev/null || die "svc-core health failed"
  curl -q -sS "$FACE_BASE/api/health" >/dev/null || die "svc-face health failed"
  curl -q -sS "$MUSIC_BASE/api/health" >/dev/null || die "svc-music health failed"
  if [[ "$FUSION_DISABLE" != "1" ]]; then
    curl -q -sS "$FUSION_EXT_BASE/api/health" >/dev/null || die "svc-fusion-extension health failed ($FUSION_EXT_BASE)"
  fi
}

login_if_needed() {
  if [[ -n "$TOKEN" ]]; then
    log "Using existing TOKEN"
    return
  fi
  [[ -n "$EMAIL" && -n "$PASSWORD" ]] || die "Set TOKEN or set EMAIL+PASSWORD (and export them to the process)"
  log "Logging in via svc-core..."
  local payload code
  payload="$(python3 - <<PY
import json,os
print(json.dumps({"email":os.environ["EMAIL"],"password":os.environ["PASSWORD"]}))
PY
)"
  code="$(http_json_allow_fail POST "$CORE_BASE/api/auth/login" "$payload")"
  save_resp "$RUN_DIR/login_out.json"
  [[ "$code" == 2* || "$code" == 3* ]] || die "login failed (see $RUN_DIR/login_out.json)"
  TOKEN="$(python3 - <<'PY'
import json
j=json.load(open("'"$RUN_DIR/login_out.json"'","r",encoding="utf-8"))
for k in ("access_token","token","jwt"):
  if k in j and j[k]:
    print(j[k]); raise SystemExit
raise SystemExit(2)
PY
)" || die "Could not find token field in login response ($RUN_DIR/login_out.json)"
  export TOKEN
  log "Authenticated."
}

# -----------------------------
# Face Studio (OpenAPI-driven minimal payload)
# -----------------------------
_face_make_payload() {
  local path="$1"
  local method="$2"

  python3 - "$path" "$method" <<'PY'
import json, os, urllib.request, sys

BASE=os.environ["FACE_BASE"].rstrip("/")
PATH=sys.argv[1]
METHOD=sys.argv[2].lower()
FACE_PROMPT=os.environ.get("FACE_PROMPT","")
FACE_N_VARIANTS=int(os.environ.get("FACE_N_VARIANTS","1"))
REF_KEY = "$" + "ref"

def fetch_openapi():
  for p in ("/openapi.json","/api/openapi.json"):
    url=BASE+p
    try:
      with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))
    except Exception:
      pass
  return None

spec=fetch_openapi()
if not spec:
  print("{}"); raise SystemExit(0)

schemas=(spec.get("components") or {}).get("schemas") or {}

def resolve_ref(ref):
  if not isinstance(ref,str) or not ref.startswith("#/components/schemas/"):
    return None
  return schemas.get(ref.split("/")[-1])

def pick(s):
  if not isinstance(s, dict): return {}
  if REF_KEY in s:
    r=resolve_ref(s[REF_KEY])
    return pick(r) if r else {}
  if "allOf" in s and s["allOf"]:
    out={"type":"object","properties":{}, "required":[]}
    for part in s["allOf"]:
      ps=pick(part)
      out["properties"].update(ps.get("properties") or {})
      out["required"] = list(dict.fromkeys((out.get("required") or []) + (ps.get("required") or [])))
    if "default" in s: out["default"]=s["default"]
    return out
  if "anyOf" in s and s["anyOf"]:
    for opt in s["anyOf"]:
      if isinstance(opt, dict) and opt.get("type")=="null": continue
      return pick(opt)
    return {}
  if "oneOf" in s and s["oneOf"]:
    return pick(s["oneOf"][0])
  return s

def example(s, name=""):
  s=pick(s)
  if "default" in s: return s["default"]
  if isinstance(s.get("enum"), list) and s["enum"]:
    return s["enum"][0]
  t=s.get("type")
  if t=="object" or "properties" in s:
    props=s.get("properties") or {}
    req=set(s.get("required") or [])
    out={}
    for k,ps in props.items():
      if k in req or (isinstance(ps, dict) and "default" in ps):
        lk=k.lower()
        if "prompt" in lk and FACE_PROMPT:
          out[k]=FACE_PROMPT; continue
        if any(x in lk for x in ("n_variants","num_variants","variant_count","n_outputs","num_outputs")):
          out[k]=FACE_N_VARIANTS; continue
        out[k]=example(ps, k)
    return out
  if t=="array":
    it=example(s.get("items") or {}, name)
    return [it] if it is not None else []
  if t=="string":
    if "prompt" in name.lower() and FACE_PROMPT:
      return FACE_PROMPT
    if s.get("format")=="uuid":
      return "00000000-0000-0000-0000-000000000000"
    return "string"
  if t=="integer": return 1
  if t=="number": return 1.0
  if t=="boolean": return False
  return None

op=((spec.get("paths") or {}).get(PATH) or {}).get(METHOD) or {}
schema=((((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {})
payload=example(schema, "root")
if not isinstance(payload, dict): payload={}
print(json.dumps(payload))
PY
}

face_create_job_and_get_asset_id() {
  log "Creating Face Studio image (to obtain IMG_ASSET_ID)..."

  local endpoint payload code
  endpoint="/api/face/creator/generate"
  payload="$(_face_make_payload "$endpoint" post)"
  printf '%s\n' "$payload" > "$RUN_DIR/face_generate_payload.json"

  code="$(http_json_allow_fail POST "$FACE_BASE$endpoint" "$payload")"
  save_resp "$RUN_DIR/face_generate_out.json"
  if [[ "$code" != 2* && "$code" != 3* ]]; then
    endpoint="/api/face/generate"
    payload="$(_face_make_payload "$endpoint" post)"
    printf '%s\n' "$payload" > "$RUN_DIR/face_generate_payload_fallback.json"
    code="$(http_json_allow_fail POST "$FACE_BASE$endpoint" "$payload")"
    save_resp "$RUN_DIR/face_generate_out_fallback.json"
    [[ "$code" == 2* || "$code" == 3* ]] || die "Face generation failed (see $RUN_DIR/face_generate_out*.json)"
    cp "$RUN_DIR/face_generate_out_fallback.json" "$RUN_DIR/face_generate_out.json"
  fi

  local FACE_JOB_ID
  FACE_JOB_ID="$(sanitize "$(py_get "$RUN_DIR/face_generate_out.json" job_id id || true)")"
  [[ -n "$FACE_JOB_ID" ]] || die "Could not parse FACE_JOB_ID ($RUN_DIR/face_generate_out.json)"
  log "FACE_JOB_ID=$FACE_JOB_ID"

  local start now
  start="$(date +%s)"
  while true; do
    if curl -q -sS "$FACE_BASE/api/face/creator/jobs/$FACE_JOB_ID/status" -H "Authorization: Bearer $TOKEN" > "$RUN_DIR/face_status.json"; then
      :
    elif curl -q -sS "$FACE_BASE/api/face/jobs/$FACE_JOB_ID" -H "Authorization: Bearer $TOKEN" > "$RUN_DIR/face_status.json"; then
      :
    else
      die "Failed to fetch face status ($FACE_JOB_ID)"
    fi

    IMG_ASSET_ID="$(sanitize "$(py_get "$RUN_DIR/face_status.json" media_asset_id image_asset_id asset_id id || true)")"
    IMG_URL="$(sanitize "$(py_find_first_url "$RUN_DIR/face_status.json")")"

    if [[ -n "$IMG_ASSET_ID" && "$IMG_ASSET_ID" != "00000000-0000-0000-0000-000000000000" ]]; then
      log "IMG_ASSET_ID=$IMG_ASSET_ID"
      [[ -n "$IMG_URL" ]] && log "IMG_URL=$IMG_URL"
      export IMG_ASSET_ID IMG_URL
      return
    fi

    local status
    status="$(python3 - "$RUN_DIR/face_status.json" <<'PY'
import json,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
print((j.get("status") or j.get("state") or "").lower())
PY
)" || status=""

    if [[ "$status" == "failed" ]]; then
      die "Face job failed. See $RUN_DIR/face_status.json"
    fi

    now="$(date +%s)"
    if (( now - start > FACE_MAX_WAIT_SECS )); then
      die "Face timed out after ${FACE_MAX_WAIT_SECS}s"
    fi
    sleep "$POLL_SECS"
  done
}

resolve_img_asset_id() {
  IMG_ASSET_ID="$(sanitize "${IMG_ASSET_ID:-}")"
  if [[ -n "$IMG_ASSET_ID" ]]; then
    log "Using provided IMG_ASSET_ID=$IMG_ASSET_ID"
    export IMG_ASSET_ID
    return
  fi
  face_create_job_and_get_asset_id
}

# -----------------------------
# Music: project -> performer -> generate -> publish
# -----------------------------
pick_music_outputs() {
  local default='["full_mix","timed_lyrics_json","lyrics_json","cover_art"]'
  MUSIC_OUTPUTS_JSON="$(sanitize "${MUSIC_OUTPUTS_JSON:-}")"
  [[ -z "$MUSIC_OUTPUTS_JSON" ]] && MUSIC_OUTPUTS_JSON="$default"

  python3 - <<'PY'
import json,os,sys
allowed={"instrumental","vocals","full_mix","stems_zip","lyrics_json","timed_lyrics_json","cover_art"}
raw=os.environ.get("MUSIC_OUTPUTS_JSON","").strip()
try:
  arr=json.loads(raw)
  assert isinstance(arr,list) and arr
except Exception:
  sys.exit(1)
if any(x not in allowed for x in arr):
  sys.exit(1)
print(json.dumps(arr))
PY
}

create_music_project() {
  log "Create music project (mode=$MODE)..."
  local payload code
  payload="$(python3 - <<PY
import json,os
print(json.dumps({
  "title": f"E2E {os.environ.get('MODE','autopilot')} Face->MusicVideo",
  "mode": os.environ.get("MODE","autopilot"),
  "duet_layout": "split_screen",
  "language_hint": os.environ.get("LANGUAGE_HINT","en"),
  "camera_edit": "beat_cut",
  "band_pack": []
}))
PY
)"
  code="$(http_json_allow_fail POST "$MUSIC_BASE/api/music/projects" "$payload")"
  save_resp "$RUN_DIR/music_project_create.json"
  [[ "$code" == 2* || "$code" == 3* ]] || die "create project failed (see $RUN_DIR/music_project_create.json)"
  PROJECT_ID="$(sanitize "$(py_get "$RUN_DIR/music_project_create.json" project_id id || true)")"
  [[ -n "$PROJECT_ID" ]] || die "Could not parse project_id ($RUN_DIR/music_project_create.json)"
  export PROJECT_ID
  log "PROJECT_ID=$PROJECT_ID"
}

upsert_performer() {
  log "Upsert performer (lead)..."
  local payload code
  payload="$(python3 - <<PY
import json,os
u=(os.environ.get("USER_IS_OWNER","false").lower()=="true")
vm=os.environ.get("VOICE_MODE","none")
if vm not in ("uploaded","generated","none"):
  vm="none"
print(json.dumps({
  "role":"lead",
  "image_asset_id": os.environ["IMG_ASSET_ID"],
  "voice_mode": vm,
  "user_is_owner": u
}))
PY
)"
  code="$(http_json_allow_fail POST "$MUSIC_BASE/api/music/projects/$PROJECT_ID/performers" "$payload")"
  save_resp "$RUN_DIR/music_performer_upsert.json"
  [[ "$code" == 2* || "$code" == 3* ]] || die "upsert performer failed (see $RUN_DIR/music_performer_upsert.json)"
}

generate_music_job() {
  log "Generate music job..."
  local outputs_json payload code
  outputs_json="$(pick_music_outputs)" || die "Invalid MUSIC_OUTPUTS_JSON"
  printf '%s\n' "$outputs_json" > "$RUN_DIR/music_outputs.json"

  payload="$(python3 - "$RUN_DIR/music_outputs.json" <<'PY'
import json,os,sys

aud=os.environ.get("AUDIENCE","urban").lower()
preset=(os.environ.get("INDIA_PRESET","") or "").strip().lower()
lyrics_lang=(os.environ.get("LYRICS_LANG") or os.environ.get("LANGUAGE_HINT","en")).strip()

track_prompt=(os.environ.get("TRACK_PROMPT","") or "").strip()
genre_hint=(os.environ.get("GENRE_HINT","") or "").strip()
vibe_hint=(os.environ.get("VIBE_HINT","") or "").strip()

presets={
  "bollywood_pop":{"genre":"bollywood_pop","vibe":"uplifting, hooky, radio-ready",
    "prompt_urban":"Modern Bollywood pop with Hindi sensibility, punchy drums, warm synths, subtle tabla fills, big chorus hook.",
    "prompt_rural":"Hindi folk-pop with dholak, harmonium, claps, earthy percussion, catchy singalong chorus."},
  "punjabi_bhangra":{"genre":"bhangra","vibe":"high energy, celebratory",
    "prompt_urban":"Punjabi bhangra-pop with dhol, tumbi, modern bass, festival vibe, chant-style hooks.",
    "prompt_rural":"Traditional Punjabi bhangra with dhol, algoza/tumbi, call-and-response, rustic groove."},
  "tamil_kuthu":{"genre":"tamil_kuthu","vibe":"mass, rhythmic, danceable",
    "prompt_urban":"Tamil kuthu-inspired dance track with heavy percussion, punchy bass, brass stabs, stadium chorus energy.",
    "prompt_rural":"Tamil folk-kuthu with parai/dappu style percussion, nadaswaram hints, earthy dance groove."},
  "telugu_mass":{"genre":"telugu_mass","vibe":"hero entry, cinematic",
    "prompt_urban":"Telugu mass song vibe: big cinematic drums, energetic synth arps, whistle motifs, explosive chorus.",
    "prompt_rural":"Telugu folk-mass: dappu percussion, rustic rhythm, celebratory village-fair vibe, powerful chorus."},
  "bengali_folk":{"genre":"bengali_folk","vibe":"warm, melodic, story-like",
    "prompt_urban":"Bengali folk-pop blend: ektara/dotara textures with modern pads, gentle groove, emotional chorus.",
    "prompt_rural":"Baul-inspired folk: ektara/dotara, hand percussion, intimate storytelling melody."},
  "marathi_lavani":{"genre":"lavani","vibe":"fast, percussive, theatrical",
    "prompt_urban":"Marathi lavani-pop fusion: fast dholki rhythms, theatrical melody, modern low-end, big hook.",
    "prompt_rural":"Traditional lavani feel: dholki-driven, bright rhythm, folk theatre energy."},
  "malayalam_indie":{"genre":"malayalam_indie","vibe":"smooth, modern, melodic",
    "prompt_urban":"Malayalam indie-pop: mellow groove, tasteful percussion, warm chords, catchy chorus.",
    "prompt_rural":"Malayalam folk-indie: chenda hints, acoustic textures, grounded groove."},
}

if preset in presets:
  p=presets[preset]
  if not genre_hint: genre_hint=p["genre"]
  if not vibe_hint: vibe_hint=p["vibe"]
  if not track_prompt:
    track_prompt = p["prompt_rural"] if aud=="rural" else p["prompt_urban"]

outputs=json.load(open(sys.argv[1],"r",encoding="utf-8"))
mode=os.environ.get("MODE","autopilot")

base={
  "seed": None,
  "quality": os.environ.get("QUALITY","standard"),
  "outputs": outputs,
  "provider_hints": {},
  "track_prompt": track_prompt or "Catchy upbeat pop track with a confident, cinematic vibe",
  "genre_hint": genre_hint or "pop",
  "vibe_hint": vibe_hint or "confident, uplifting",
  "lyrics_source": "generate",
  "lyrics_text": None,
  "lyrics_language_hint": lyrics_lang or "en",
  "uploaded_audio_asset_id": None,
  "uploaded_audio_url": None
}

if mode=="co_create":
  base["lyrics_source"]="upload"
  base["lyrics_text"]="(E2E) Short test lyric.\nMake it align to the beat.\n"
if mode=="byo":
  base["lyrics_source"]="none"
  base["uploaded_audio_url"]=os.environ.get("BYO_AUDIO_URL") or None
  base["uploaded_audio_asset_id"]=os.environ.get("BYO_AUDIO_ASSET_ID") or None

print(json.dumps(base))
PY
)"

  if [[ "$MODE" == "byo" ]]; then
    if [[ -z "$(sanitize "${BYO_AUDIO_URL:-}")" && -z "$(sanitize "${BYO_AUDIO_ASSET_ID:-}")" ]]; then
      die "BYO mode requires BYO_AUDIO_URL or BYO_AUDIO_ASSET_ID"
    fi
  fi

  code="$(http_json_allow_fail POST "$MUSIC_BASE/api/music/projects/$PROJECT_ID/generate" "$payload")"
  save_resp "$RUN_DIR/music_generate_out.json"
  [[ "$code" == 2* || "$code" == 3* ]] || die "generate failed (see $RUN_DIR/music_generate_out.json)"

  JOB_ID="$(sanitize "$(py_get "$RUN_DIR/music_generate_out.json" job_id id || true)")"
  [[ -n "$JOB_ID" ]] || die "Could not parse job_id ($RUN_DIR/music_generate_out.json)"
  export JOB_ID
  log "JOB_ID=$JOB_ID"
}

poll_music_job() {
  log "Polling music job status..."
  local start now status
  start="$(date +%s)"
  while true; do
    curl -q -sS "$MUSIC_BASE/api/music/jobs/$JOB_ID/status" -H "Authorization: Bearer $TOKEN" > "$RUN_DIR/music_job_status.json" || die "music status fetch failed"
    status="$(python3 - "$RUN_DIR/music_job_status.json" <<'PY'
import json,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
print((j.get("status") or "").lower())
PY
)"
    log "music.status=$status"
    if [[ "$status" == "succeeded" ]]; then return; fi
    if [[ "$status" == "failed" ]]; then die "Music job failed. See $RUN_DIR/music_job_status.json"; fi
    now="$(date +%s)"
    if (( now - start > MUSIC_MAX_WAIT_SECS )); then die "Music timed out after ${MUSIC_MAX_WAIT_SECS}s"; fi
    sleep "$POLL_SECS"
  done
}

publish_music_job() {
  log "Publishing music job..."
  local code
  code="$(http_json_allow_fail POST "$MUSIC_BASE/api/music/jobs/$JOB_ID/publish" "{}")"
  save_resp "$RUN_DIR/music_publish_out.json"

  if [[ "$code" == 2* || "$code" == 3* ]]; then
    log "Publish OK."
    return
  fi

  local has_consent
  has_consent="$(py_json_contains_token "$RUN_DIR/music_publish_out.json" "consent_required" || echo "no")"
  if [[ "$has_consent" == "yes" ]]; then
    log "Publish returned consent_required. Retrying with bare consent..."
    code="$(http_json_allow_fail POST "$MUSIC_BASE/api/music/jobs/$JOB_ID/publish" '{"consent":{"accepted":true}}')"
    save_resp "$RUN_DIR/music_publish_out_retry.json"
    [[ "$code" == 2* || "$code" == 3* ]] || die "Publish still blocked. See $RUN_DIR/music_publish_out*.json"
    cp "$RUN_DIR/music_publish_out_retry.json" "$RUN_DIR/music_publish_out.json"
    log "Publish OK after consent."
    return
  fi

  die "Publish failed. See $RUN_DIR/music_publish_out.json"
}

# -----------------------------
# Fusion-Extension Longform
# -----------------------------
build_longform_payload() {
  python3 - "$RUN_DIR/music_job_status.json" "$RUN_DIR/longform_create_payload.json" <<'PY'
import json,os,sys
status=json.load(open(sys.argv[1],"r",encoding="utf-8"))
out_path=sys.argv[2]

def clamp_int(v, lo, hi, default):
  try:
    iv=int(float(str(v).strip()))
    return max(lo, min(hi, iv))
  except Exception:
    return default

img_url=(os.environ.get("IMG_URL") or "").strip()
img_id=(os.environ.get("IMG_ASSET_ID") or "").strip()
image_ref = img_url or img_id or "unknown_image_ref"

computed=status.get("computed") or {}
script=(os.environ.get("LONGFORM_SCRIPT") or "").strip()
if not script:
  for k in ("plan_summary","lyrics_text","creative_brief"):
    v=computed.get(k)
    if isinstance(v,str) and v.strip():
      script=v.strip()
      break
if not script:
  script="Hello! This is an automated demo from DesiFaces. Generating a longform talking video from a face and script."

seg=clamp_int(os.environ.get("SEGMENT_SECONDS","10"), 1, 120, 10)
mx =clamp_int(os.environ.get("MAX_SEGMENT_SECONDS","20"), 1, 120, max(seg, 20))
if mx < seg:
  mx = seg

aspect=(os.environ.get("ASPECT_RATIO") or "9:16").strip()
if aspect not in ("16:9","9:16","1:1"):
  aspect="9:16"

voice_locale=(os.environ.get("VOICE_LOCALE") or "en-US").strip()
vgm=(os.environ.get("VOICE_GENDER_MODE") or "auto").strip()
if vgm not in ("auto","manual"):
  vgm="auto"
vg=(os.environ.get("VOICE_GENDER") or "").strip().lower()
if vg not in ("male","female"):
  vg=""

payload={
  "image_ref": image_ref,
  "script": script,
  "voice": {
    "locale": voice_locale,
    "output_format": "mp3",
  },
  "aspect_ratio": aspect,
  "segment_seconds": seg,
  "max_segment_seconds": mx,
  "tags": {
    "source": "e2e",
    "music_job_id": os.environ.get("JOB_ID"),
    "project_id": os.environ.get("PROJECT_ID"),
  },
  "voice_gender_mode": vgm,
  "voice_gender": (vg if vgm=="manual" else None),
}
if vgm=="manual" and vg:
  payload["voice"]["gender"]=vg

json.dump(payload, open(out_path,"w",encoding="utf-8"), indent=2, ensure_ascii=False)
PY
}

submit_longform_job() {
  if [[ "$FUSION_DISABLE" == "1" ]]; then
    log "FUSION_DISABLE=1 -> skipping fusion-extension submission"
    return
  fi

  log "Submitting fusion-extension longform job..."
  build_longform_payload
  local code
  code="$(http_json_allow_fail POST "$FUSION_EXT_BASE/api/longform/jobs" "$(cat "$RUN_DIR/longform_create_payload.json")")"
  save_resp "$RUN_DIR/longform_create_out.json"
  [[ "$code" == 2* || "$code" == 3* ]] || die "longform create failed (HTTP $code). See $RUN_DIR/longform_create_out.json"

  FUSION_JOB_ID="$(sanitize "$(py_get "$RUN_DIR/longform_create_out.json" job_id id || true)")"
  [[ -n "$FUSION_JOB_ID" ]] || die "Could not parse longform job_id. See $RUN_DIR/longform_create_out.json"
  export FUSION_JOB_ID
  log "LONGFORM_JOB_ID=$FUSION_JOB_ID"
}

poll_longform_job() {
  if [[ "$FUSION_DISABLE" == "1" ]]; then
    log "FUSION_DISABLE=1 -> skipping fusion-extension polling"
    return
  fi

  log "Polling fusion-extension longform status..."
  local start now code status
  start="$(date +%s)"
  while true; do
    code="$(http_get_allow_fail "$FUSION_EXT_BASE/api/longform/jobs/$FUSION_JOB_ID")"
    save_resp "$RUN_DIR/longform_status.json"
    [[ "$code" == 2* || "$code" == 3* ]] || die "longform status failed (HTTP $code). See $RUN_DIR/longform_status.json"

    VIDEO_URL="$(sanitize "$(py_extract_video_url_any "$RUN_DIR/longform_status.json")")"
    status="$(python3 - "$RUN_DIR/longform_status.json" <<'PY'
import json,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
print((j.get("status") or j.get("state") or "").lower())
PY
)" || status="running"

    log "longform.status=${status:-unknown}"

    if [[ -n "${VIDEO_URL:-}" ]]; then
      log "FINAL VIDEO URL: $VIDEO_URL"
      printf '%s\n' "$VIDEO_URL" > "$RUN_DIR/final_video_url.txt"
      # best-effort segments debug
      http_get_allow_fail "$FUSION_EXT_BASE/api/longform/jobs/$FUSION_JOB_ID/segments" >/dev/null 2>&1 || true
      save_resp "$RUN_DIR/longform_segments.json"
      return
    fi

    if [[ "$status" == "failed" ]]; then
      die "Longform job failed. See $RUN_DIR/longform_status.json"
    fi

    now="$(date +%s)"
    if (( now - start > FUSION_MAX_WAIT_SECS )); then
      die "Longform timed out after ${FUSION_MAX_WAIT_SECS}s"
    fi
    sleep "$POLL_SECS"
  done
}

main() {
  require_cmds
  preflight
  login_if_needed

  resolve_img_asset_id
  create_music_project
  upsert_performer
  generate_music_job
  poll_music_job
  publish_music_job

  submit_longform_job
  poll_longform_job

  log "DONE. Artifacts in: $RUN_DIR"
  if [[ -s "$RUN_DIR/final_video_url.txt" ]]; then
    log "FINAL VIDEO URL:"
    cat "$RUN_DIR/final_video_url.txt" >&2
  else
    log "NOTE: No video url produced yet. Check $RUN_DIR/longform_status.json"
  fi
}

main "$@"
