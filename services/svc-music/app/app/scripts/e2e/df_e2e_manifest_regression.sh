#!/usr/bin/env bash
# services/svc-music/app/app/scripts/e2e/df_e2e_manifest_regression.sh
set -euo pipefail

MUSIC_BASE="${MUSIC_BASE:-http://localhost:8007}"
OPENAPI_JSON="${OPENAPI_JSON:-/tmp/svc-music-openapi.json}"
OUT_DIR="${OUT_DIR:-/tmp/df_music_manifest_regression}"
QUALITY="${QUALITY:-standard}"
LANG="${LANG:-en}"
DUET_LAYOUT="${DUET_LAYOUT:-split_screen}"

mkdir -p "$OUT_DIR"

AUTH=()
if [[ -n "${TOKEN:-}" ]]; then
  AUTH=(-H "Authorization: Bearer $TOKEN")
fi

py_builder="services/svc-music/app/app/scripts/e2e/df_openapi_payload_builder.py"
py_validate="services/svc-music/app/app/scripts/e2e/df_validate_manifest.py"

if [[ ! -f "$OPENAPI_JSON" ]]; then
  echo "OpenAPI not found at $OPENAPI_JSON"
  echo "Run: curl -fsS $MUSIC_BASE/openapi.json > $OPENAPI_JSON"
  exit 2
fi

curl_json_code() {
  # Writes body to stdout; prints HTTP code to stderr prefixed with __HTTP__
  local method="$1"; shift
  local url="$1"; shift
  local data_file="${1:-}"

  if [[ -n "$data_file" ]]; then
    curl -sS -X "$method" "$url" "${AUTH[@]}" \
      -H "Content-Type: application/json" --data-binary "@$data_file" \
      -w "\n__HTTP__%{http_code}\n"
  else
    curl -sS -X "$method" "$url" "${AUTH[@]}" \
      -w "\n__HTTP__%{http_code}\n"
  fi
}

curl_json_or_die() {
  local method="$1"; shift
  local url="$1"; shift
  local data_file="${1:-}"

  local out tmp http
  tmp="$(mktemp)"
  curl_json_code "$method" "$url" "$data_file" > "$tmp" || true
  http="$(grep -Eo "__HTTP__[0-9]{3}" "$tmp" | tail -n1 | sed 's/__HTTP__//')"
  # body is everything except the last __HTTP__ line
  sed '/^__HTTP__[0-9]\{3\}$/d' "$tmp"
  rm -f "$tmp"

  if [[ "$http" != "200" && "$http" != "201" ]]; then
    if [[ "$http" == "401" || "$http" == "403" ]]; then
      echo "ERROR: HTTP $http from $url (TOKEN missing/expired). Re-export TOKEN and re-run." >&2
    else
      echo "ERROR: HTTP $http from $url" >&2
    fi
    return 1
  fi
  return 0
}

extract_uuid_any() {
  python3 - "$1" <<'PY'
import json, re, sys
UUID_RE=re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")
p=sys.argv[1]
j=json.load(open(p,"r",encoding="utf-8"))
def walk(x):
  if isinstance(x,dict):
    for v in x.values():
      u=walk(v)
      if u: return u
  if isinstance(x,list):
    for v in x:
      u=walk(v)
      if u: return u
  if isinstance(x,str):
    m=UUID_RE.search(x)
    if m: return m.group(0)
  return None
print(walk(j) or "")
PY
}

extract_key_uuid() {
  # Prefer specific keys first, then UUID scan
  local json_path="$1"
  local key="$2"
  python3 - "$json_path" "$key" <<'PY'
import json, re, sys
UUID_RE=re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
p=sys.argv[1]; key=sys.argv[2]
j=json.load(open(p,"r",encoding="utf-8"))
v=j.get(key)
if isinstance(v,str) and UUID_RE.match(v):
  print(v); sys.exit(0)
print("")
PY
}

poll_job() {
  local job_id="$1"
  local out_json="$2"
  local tries="${3:-180}"

  for ((i=1;i<=tries;i++)); do
    # capture HTTP code safely
    local tmp http
    tmp="$(mktemp)"
    curl -sS -X GET "$MUSIC_BASE/api/music/jobs/$job_id/status" "${AUTH[@]}" \
      -o "$tmp" -w "%{http_code}" > "$tmp.http" || true
    http="$(cat "$tmp.http" 2>/dev/null || echo "")"
    rm -f "$tmp.http"

    if [[ "$http" != "200" ]]; then
      if [[ "$http" == "401" || "$http" == "403" ]]; then
        echo "ERROR: polling returned HTTP $http. TOKEN missing/expired. Re-export TOKEN and re-run." >&2
      else
        echo "ERROR: polling returned HTTP $http" >&2
        head -c 300 "$tmp" >&2 || true
        echo >&2
      fi
      rm -f "$tmp"
      return 1
    fi

    mv "$tmp" "$out_json"

    local info
    info="$(python3 - "$out_json" <<'PY'
import json,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
def first(*paths):
  for p in paths:
    cur=j
    ok=True
    for k in p:
      if isinstance(cur,dict) and k in cur: cur=cur[k]
      else:
        ok=False; break
    if ok: return cur
  return None
status = first(["status"],["job","status"],["data","status"],["result","status"],["job_status"]) or ""
stage  = first(["stage"],["job","stage"],["data","stage"],["result","stage"]) or ""
err    = first(["error"],["job","error"],["data","error"],["result","error"],["detail"],["message"]) or ""
print(f"{str(status).lower()}|{str(stage).lower()}|{str(err)[:200]}")
PY
)"

    local st="${info%%|*}"
    local rest="${info#*|}"
    local stage="${rest%%|*}"
    local err="${rest#*|}"

    if [[ "$st" == "succeeded" || "$st" == "success" || "$st" == "completed" ]]; then
      echo "Job $job_id status=$st stage=$stage"
      return 0
    fi
    if [[ "$st" == "failed" || "$st" == "error" ]]; then
      echo "Job $job_id status=$st stage=$stage error=${err}"
      return 1
    fi

    if (( i % 5 == 0 )); then
      echo "Polling job $job_id ... (try=$i status=$st stage=$stage err=${err})"
    fi
    sleep 1
  done

  echo "Timed out polling job $job_id"
  return 2
}

create_project() {
  local title="$1"
  local mode="$2"

  local req="$OUT_DIR/create_project_${mode}.json"
  local resp="$OUT_DIR/create_project_${mode}.resp.json"

  OPENAPI_JSON="$OPENAPI_JSON" python3 "$py_builder" create_project "$title" "$mode" "$LANG" "$DUET_LAYOUT" > "$req"
  curl_json_or_die POST "$MUSIC_BASE/api/music/projects" "$req" > "$resp"

  local pid
  pid="$(extract_key_uuid "$resp" "project_id")"
  [[ -z "$pid" ]] && pid="$(extract_key_uuid "$resp" "id")"
  [[ -z "$pid" ]] && pid="$(extract_uuid_any "$resp")"

  if [[ -z "$pid" ]]; then
    echo "Failed to extract project_id from $resp"
    cat "$resp"
    exit 2
  fi
  echo "$pid"
}

upload_byo_audio() {
  local wav="$OUT_DIR/byo.wav"
  python3 - "$wav" <<'PY'
import sys, wave
path=sys.argv[1]
sr=44100
duration_ms=3000
frames=int(sr*(duration_ms/1000.0))
silence=b"\x00\x00"*frames
with wave.open(path,"wb") as wf:
  wf.setnchannels(1)
  wf.setsampwidth(2)
  wf.setframerate(sr)
  wf.writeframes(silence)
print(duration_ms)
PY

  local resp="$OUT_DIR/assets_upload.resp.json"
  # NOTE: assets/upload is often auth-protected too
  curl -sS -X POST "$MUSIC_BASE/api/music/assets/upload" "${AUTH[@]}" \
    -F "file=@$wav;type=audio/wav;filename=byo.wav" \
    -o "$resp" -w "%{http_code}" > "$resp.http" || true

  local http
  http="$(cat "$resp.http" 2>/dev/null || echo "")"
  rm -f "$resp.http"

  if [[ "$http" != "200" && "$http" != "201" ]]; then
    echo "ERROR: assets/upload returned HTTP $http (TOKEN missing/expired?)" >&2
    head -c 300 "$resp" >&2 || true
    echo >&2
    exit 1
  fi

  local url
  url="$(python3 - "$resp" <<'PY'
import json,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
cands=[]
def walk(x):
  if isinstance(x,dict):
    for k,v in x.items():
      lk=str(k).lower()
      if lk in ("sas_url","url","asset_url","download_url","media_url","blob_url"):
        if isinstance(v,str) and v.startswith("http"):
          cands.append(v)
      walk(v)
  elif isinstance(x,list):
    for v in x: walk(v)
walk(j)
print(cands[0] if cands else "")
PY
)"
  if [[ -z "$url" ]]; then
    echo "Failed to extract uploaded audio URL from assets/upload response:"
    cat "$resp"
    exit 2
  fi
  echo "$url"
}

start_generate() {
  local project_id="$1"
  local case_name="$2"
  local mode_hint="$3"
  local seed="$4"
  local provider_hints_json="$5"
  local uploaded_audio_url="$6"
  local uploaded_audio_dur="$7"

  local req="$OUT_DIR/generate_${case_name}.json"
  local resp="$OUT_DIR/generate_${case_name}.resp.json"

  OPENAPI_JSON="$OPENAPI_JSON" python3 "$py_builder" generate "$QUALITY" "$seed" "$mode_hint" "$provider_hints_json" "$uploaded_audio_url" "$uploaded_audio_dur" > "$req"
  curl_json_or_die POST "$MUSIC_BASE/api/music/projects/$project_id/generate" "$req" > "$resp"

  local jid
  jid="$(extract_key_uuid "$resp" "job_id")"
  [[ -z "$jid" ]] && jid="$(extract_key_uuid "$resp" "id")"
  [[ -z "$jid" ]] && jid="$(extract_uuid_any "$resp")"

  if [[ -z "$jid" ]]; then
    echo "Failed to extract job_id from $resp"
    cat "$resp"
    exit 2
  fi
  echo "$jid"
}

run_case() {
  local case_name="$1"
  local mode="$2"
  local render_video="$3"
  local no_face="$4"
  local exports="$5"
  local seed="$6"
  local byo="$7"

  echo
  echo "===================="
  echo "CASE: $case_name"
  echo "===================="

  local pid
  pid="$(create_project "ManifestRegression-$case_name" "$mode")"
  echo "project_id=$pid"

  local uploaded_url="null"
  local uploaded_dur="null"
  if [[ "$byo" == "true" ]]; then
    uploaded_url="$(upload_byo_audio)"
    uploaded_dur="3000"
    echo "uploaded_audio_url=$uploaded_url"
  fi

  # ✅ force deterministic + fast audio: never hit external providers in regression
  local hints="{\"render_video\": $render_video"
  hints+=", \"no_face\": $no_face"
  hints+=", \"music_provider\": \"native\""
  if [[ -n "$exports" ]]; then
    hints+=", \"exports\": $exports"
  fi
  hints+="}"

  local jid
  jid="$(start_generate "$pid" "$case_name" "null" "$seed" "$hints" "$uploaded_url" "$uploaded_dur")"
  echo "job_id=$jid"

  local status_json="$OUT_DIR/status_${case_name}.json"
# ---------------------------
# Validate manifest existence
# ---------------------------
local expect_manifest="true"
if [[ "$render_video" != "true" ]]; then
  expect_manifest="false"
fi

# Try to fetch project JSON (may 401 if TOKEN missing/expired). Do NOT fail the test run.
local project_json="$OUT_DIR/project_${case_name}.json"
if curl -fsS --max-time 20 "${AUTH[@]}" \
  "$MUSIC_BASE/api/music/projects/$pid" > "$project_json"; then
  python3 "$py_validate" "$status_json" "$project_json" "$expect_manifest"
else
  rm -f "$project_json" 2>/dev/null || true
  python3 "$py_validate" "$status_json" "$expect_manifest"
fi

  local expect_manifest="true"
  if [[ "$render_video" != "true" ]]; then
    expect_manifest="false"
  fi

    project_json="$OUT_DIR/project_${case_name}.json"
    curl_json GET "$MUSIC_BASE/api/music/projects/$pid" > "$project_json"
    python3 "$py_validate" "$status_json" "$project_json" "$expect_manifest"
}

run_case "A1_autopilot_render_video_true"  "autopilot" "true"  "false" ""                    "123"  "false"
run_case "A2_autopilot_render_video_false" "autopilot" "false" "false" ""                    "123"  "false"
run_case "A3_autopilot_no_face_true"       "autopilot" "true"  "true"  ""                    "123"  "false"
run_case "A4_autopilot_exports_override"   "autopilot" "true"  "false" "[\"9:16\",\"1:1\"]"  "123"  "false"
run_case "B1_byo_uploaded_audio"           "byo"       "true"  "false" "[\"9:16\"]"          "123"  "true"
run_case "C1_weird_seed_1_0_exports_empty" "autopilot" "true"  "false" ""                    "1.0"  "false"

echo
echo "ALL CASES DONE. Output in: $OUT_DIR"