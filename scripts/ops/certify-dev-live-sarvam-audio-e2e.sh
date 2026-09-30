#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

CORE_CONTAINER="${CORE_CONTAINER:-df-svc-core}"
AUDIO_API="${AUDIO_API:-df-svc-audio}"

discover_host_url() {
  local container="$1"
  local container_port="$2"
  local explicit="$3"

  if [[ -n "$explicit" ]]; then
    printf '%s' "$explicit"
    return 0
  fi

  docker inspect "$container" >/dev/null 2>&1 || {
    echo "FAIL: missing container $container" >&2
    return 1
  }

  local binding host port
  binding="$(docker port "$container" "$container_port/tcp" 2>/dev/null | head -1 || true)"
  [[ -n "$binding" ]] || {
    echo "FAIL: no host binding for $container:$container_port" >&2
    return 1
  }

  host="${binding%:*}"
  port="${binding##*:}"
  host="${host#0.0.0.0}"
  host="${host#[::]}"
  [[ -n "$host" ]] || host="127.0.0.1"
  [[ "$host" == "::" ]] && host="127.0.0.1"

  printf 'http://%s:%s' "$host" "$port"
}

CORE_URL="$(discover_host_url "$CORE_CONTAINER" 8000 "${CORE_URL:-}")"
AUDIO_URL="$(discover_host_url "$AUDIO_API" 8004 "${AUDIO_URL:-}")"

DF_EMAIL="${DF_EMAIL:-user2@desifaces.ai}"
DF_PASSWORD="${DF_PASSWORD:-password2}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
MAX_POLLS="${MAX_POLLS:-80}"
POLL_SECS="${POLL_SECS:-2}"

OUT_DIR="${OUT_DIR:-/tmp/df-sarvam-live-smoke-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

echo "============================================================"
echo " desifaces DEV — LIVE SARVAM AUDIO E2E"
echo " ACTUAL PROVIDER CALLS / PRICING / ARTIFACT VERIFICATION"
echo "============================================================"
echo "core_url=$CORE_URL"
echo "audio_url=$AUDIO_URL"
echo "email=$DF_EMAIL"
echo "production=UNTOUCHED"
echo "face_video_workflows=FROZEN"

for cmd in curl python3 docker; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "FAIL: missing command $cmd"; exit 2; }
done
docker inspect "$CORE_CONTAINER" >/dev/null 2>&1 || { echo "FAIL: missing $CORE_CONTAINER"; exit 2; }
docker inspect "$AUDIO_API" >/dev/null 2>&1 || { echo "FAIL: missing $AUDIO_API"; exit 2; }
docker inspect "$DB_CONTAINER" >/dev/null 2>&1 || { echo "FAIL: missing $DB_CONTAINER"; exit 2; }

CORE_HEALTH="$(curl -sS -o /dev/null -w '%{http_code}' "$CORE_URL/api/health" || true)"
AUDIO_HEALTH="$(curl -sS -o /dev/null -w '%{http_code}' "$AUDIO_URL/api/health" || true)"
[[ "$CORE_HEALTH" =~ ^2 ]] || { echo "FAIL: core endpoint unreachable url=$CORE_URL http=$CORE_HEALTH"; exit 2; }
[[ "$AUDIO_HEALTH" =~ ^2 ]] || { echo "FAIL: audio endpoint unreachable url=$AUDIO_URL http=$AUDIO_HEALTH"; exit 2; }
echo "DEV_ENDPOINT_DISCOVERY=PASS core=$CORE_URL audio=$AUDIO_URL"

json_value() {
  python3 - "$1" "$2" <<'PY'
import json,sys
path,expr=sys.argv[1],sys.argv[2]
try:
    cur=json.load(open(path,encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit
for p in expr.split("."):
    if not p: continue
    cur=cur.get(p) if isinstance(cur,dict) else None
    if cur is None: break
if cur is None: print("")
elif isinstance(cur,(dict,list)): print(json.dumps(cur,separators=(",",":")))
else: print(cur)
PY
}

echo
echo "===== 1. LOGIN ====="
LOGIN="$OUT_DIR/login.json"
HTTP="$(curl -sS -o "$LOGIN" -w '%{http_code}'   -X POST "$CORE_URL/api/auth/login"   -H 'Content-Type: application/json'   --data "$(python3 - "$DF_EMAIL" "$DF_PASSWORD" <<'PY'
import json,sys
print(json.dumps({"email":sys.argv[1],"password":sys.argv[2],"device_id":"dev-sarvam-cert","client_type":"web"}))
PY
)")"
[[ "$HTTP" == "200" ]] || { echo "FAIL: login HTTP=$HTTP"; cat "$LOGIN"; exit 3; }

TOKEN="$(json_value "$LOGIN" access_token)"
USER_ID="$(json_value "$LOGIN" user_id)"
if [[ -z "$USER_ID" ]]; then
  USER_ID="$(python3 - "$TOKEN" <<'PY'
import base64,json,sys
p=sys.argv[1].split(".")[1]
p += "="*(-len(p)%4)
print(json.loads(base64.urlsafe_b64decode(p)).get("sub",""))
PY
)"
fi
[[ -n "$TOKEN" && -n "$USER_ID" ]] || { echo "FAIL: login token/user missing"; exit 3; }
echo "LOGIN=PASS user_id=$USER_ID"

DB_URL="$(docker exec "$AUDIO_API" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

run_case() {
  local name="$1"
  local locale="$2"
  local style="$3"
  local voice="$4"
  local text="$5"
  local expected_provider="$6"
  local expected_model="$7"

  local prefix="$OUT_DIR/$name"
  local preview_req="$prefix.preview.req.json"
  local preview_resp="$prefix.preview.resp.json"
  local gen_req="$prefix.generate.req.json"
  local gen_resp="$prefix.generate.resp.json"
  local status_resp="$prefix.status.json"

  echo
  echo "============================================================"
  echo " CASE=$name locale=$locale style=$style voice=${voice:-AUTO}"
  echo "============================================================"

  python3 - "$preview_req" "$locale" "$style" "$voice" "$text" <<'PY'
import json,sys
out,locale,style,voice,text=sys.argv[1:]
obj={
  "text":text,
  "target_locale":locale,
  "source_language":locale.split("-",1)[0],
  "translate":False,
  "style":style,
  "output_format":"mp3",
}
if voice:
    obj["voice"]=voice
    obj["voice_id"]=voice
    obj["voice_locale"]=locale
json.dump(obj,open(out,"w"),ensure_ascii=False,indent=2)
PY

  HTTP="$(curl -sS -o "$preview_resp" -w '%{http_code}'     -X POST "$AUDIO_URL/api/audio/tts/pricing/preview"     -H "Authorization: Bearer $TOKEN"     -H "X-User-Id: $USER_ID"     -H 'Content-Type: application/json'     --data @"$preview_req")"
  [[ "$HTTP" == "200" ]] || {
    echo "FAIL: $name preview HTTP=$HTTP"
    cat "$preview_resp"
    exit 10
  }

  QUOTE_ID="$(python3 - "$preview_resp" <<'PY'
import json,sys
j=json.load(open(sys.argv[1]))
print((j.get("pricing") or {}).get("quote_id") or j.get("quote_id") or "")
PY
)"
  FP="$(python3 - "$preview_resp" <<'PY'
import json,sys
j=json.load(open(sys.argv[1]))
print((j.get("pricing") or {}).get("preview_fingerprint") or j.get("preview_fingerprint") or "")
PY
)"
  CREDITS="$(python3 - "$preview_resp" <<'PY'
import json,sys
j=json.load(open(sys.argv[1]))
p=j.get("pricing") or {}
s=j.get("pricing_summary") or {}
print(p.get("amount") or p.get("estimated_amount") or s.get("estimated_credits") or s.get("credits") or "")
PY
)"
  [[ -n "$QUOTE_ID" && -n "$FP" ]] || { echo "FAIL: $name missing quote/fingerprint"; cat "$preview_resp"; exit 10; }
  echo "PRICING_PREVIEW=PASS case=$name quoted=${CREDITS:-unknown}"

  python3 - "$preview_req" "$gen_req" "$QUOTE_ID" "$FP" <<'PY'
import json,sys
src,out,q,fp=sys.argv[1:]
j=json.load(open(src))
j["pricing_confirmation"]={"quote_id":q,"preview_fingerprint":fp}
json.dump(j,open(out,"w"),ensure_ascii=False,indent=2)
PY

  HTTP="$(curl -sS -o "$gen_resp" -w '%{http_code}'     -X POST "$AUDIO_URL/api/audio/tts"     -H "Authorization: Bearer $TOKEN"     -H "X-User-Id: $USER_ID"     -H 'Content-Type: application/json'     --data @"$gen_req")"
  [[ "$HTTP" == "200" || "$HTTP" == "201" || "$HTTP" == "202" ]] || {
    echo "FAIL: $name generate HTTP=$HTTP"
    cat "$gen_resp"
    exit 11
  }

  JOB_ID="$(json_value "$gen_resp" job_id)"
  [[ -n "$JOB_ID" ]] || { echo "FAIL: $name missing job_id"; cat "$gen_resp"; exit 11; }
  echo "JOB_CREATED=PASS case=$name job_id=$JOB_ID"

  local terminal=""
  local pricing_state=""
  for i in $(seq 1 "$MAX_POLLS"); do
    HTTP="$(curl -sS -o "$status_resp" -w '%{http_code}'       "$AUDIO_URL/api/audio/jobs/$JOB_ID/status"       -H "Authorization: Bearer $TOKEN"       -H "X-User-Id: $USER_ID")"
    [[ "$HTTP" == "200" ]] || { echo "FAIL: $name status HTTP=$HTTP"; cat "$status_resp"; exit 12; }
    terminal="$(json_value "$status_resp" status)"
    pricing_state="$(json_value "$status_resp" pricing.state)"
    echo "poll=$i case=$name status=$terminal pricing_state=${pricing_state:-unknown}"
    if [[ "$terminal" == "succeeded" || "$terminal" == "failed" || "$terminal" == "cancelled" ]]; then break; fi
    sleep "$POLL_SECS"
  done

  [[ "$terminal" == "succeeded" ]] || {
    echo "FAIL: $name terminal=$terminal"
    cat "$status_resp"
    "${PSQL[@]}" -P pager=off -c "
      select id,status,error_code,error_message,payload_json->>'target_locale' locale,
             payload_json->>'style' style,payload_json->>'voice' voice
      from public.studio_jobs where id='$JOB_ID'::uuid;
    " || true
    exit 13
  }
  [[ "$pricing_state" == "committed" ]] || {
    echo "FAIL: $name succeeded but pricing_state=$pricing_state"
    cat "$status_resp"
    exit 14
  }

  DB_ROW="$("${PSQL[@]}" -AtF '|' -c "
    select
      coalesce(a.meta_json->>'provider_code',''),
      coalesce(a.meta_json->>'model_code',''),
      coalesce(a.meta_json->>'voice',''),
      coalesce(a.bytes,0)::text,
      coalesce(sj.payload_json->'pricing'->>'state',''),
      coalesce(sj.payload_json->'pricing'->>'final_amount',sj.payload_json->'pricing'->>'amount',''),
      coalesce(sj.payload_json->'pricing'->>'billed_units','')
    from public.studio_jobs sj
    join lateral (
      select * from public.artifacts
      where job_id=sj.id and kind='audio'
      order by created_at desc limit 1
    ) a on true
    where sj.id='$JOB_ID'::uuid;
  ")"

  IFS='|' read -r ACTUAL_PROVIDER ACTUAL_MODEL ACTUAL_VOICE BYTES DB_PRICE_STATE FINAL_AMOUNT BILLED_UNITS <<< "$DB_ROW"

  [[ "$ACTUAL_PROVIDER" == "$expected_provider" ]] || {
    echo "FAIL: $name provider expected=$expected_provider actual=$ACTUAL_PROVIDER"
    exit 15
  }
  [[ "$ACTUAL_MODEL" == "$expected_model" ]] || {
    echo "FAIL: $name model expected=$expected_model actual=$ACTUAL_MODEL"
    exit 15
  }
  [[ "${BYTES:-0}" =~ ^[0-9]+$ && "${BYTES:-0}" -gt 0 ]] || {
    echo "FAIL: $name artifact bytes=$BYTES"
    exit 16
  }
  [[ "$DB_PRICE_STATE" == "committed" ]] || {
    echo "FAIL: $name DB pricing state=$DB_PRICE_STATE"
    exit 17
  }

  echo "LIVE_TTS=PASS case=$name provider=$ACTUAL_PROVIDER model=$ACTUAL_MODEL voice=$ACTUAL_VOICE bytes=$BYTES"
  echo "PRICING_COMMIT=PASS case=$name final_amount=${FINAL_AMOUNT:-unknown} billed_units=${BILLED_UNITS:-unknown}"
  echo "$JOB_ID" >> "$OUT_DIR/job_ids.txt"
}

echo
echo "===== 2. LIVE GENERATION CASES ====="

run_case   "hi_conversational_sarvam"   "hi-IN"   "Conversational"   "shubh"   "नमस्ते। यह desifaces के लिए एक छोटा सा हिंदी ऑडियो परीक्षण है।"   "sarvam"   "bulbul_v3"

run_case   "ta_narration_sarvam"   "ta-IN"   "Narration"   "shubh"   "வணக்கம். இது desifaces க்கான ஒரு சிறிய தமிழ் ஒலி சோதனை."   "sarvam"   "bulbul_v3"

run_case   "en_us_global_control"   "en-US"   "Conversational"   ""   "Hello. This is a short desifaces global audio routing control test."   "elevenlabs"   "eleven_flash_v2_5"

echo
echo "===== 3. FINAL DB EVIDENCE ====="
JOB_IDS_CSV="$(paste -sd, "$OUT_DIR/job_ids.txt")"
"${PSQL[@]}" -P pager=off -c "
select
  sj.created_at,
  sj.id job_id,
  sj.status,
  sj.payload_json->>'target_locale' locale,
  sj.payload_json->>'style' style,
  sj.payload_json->>'voice' requested_voice,
  a.meta_json->>'provider_code' actual_provider,
  a.meta_json->>'model_code' actual_model,
  a.meta_json->>'voice' actual_voice,
  a.bytes,
  sj.payload_json->'pricing'->>'state' pricing_state,
  sj.payload_json->'pricing'->>'final_amount' final_amount,
  sj.payload_json->'pricing'->>'billed_units' billed_units
from public.studio_jobs sj
join lateral (
  select * from public.artifacts
  where job_id=sj.id and kind='audio'
  order by created_at desc limit 1
) a on true
where sj.id = any(string_to_array('$JOB_IDS_CSV',',')::uuid[])
order by sj.created_at;
"

echo
echo "============================================================"
echo "SARVAM_HINDI_CONVERSATIONAL_LIVE=PASS"
echo "SARVAM_TAMIL_NARRATION_LIVE=PASS"
echo "GLOBAL_EN_US_CONTROL=PASS"
echo "AUDIO_ARTIFACT_METADATA=PASS"
echo "AUDIO_PRICING_COMMIT=PASS"
echo "production=UNTOUCHED"
echo "LIVE_SARVAM_AUDIO_E2E=PASS"
echo "OUT_DIR=$OUT_DIR"
echo "============================================================"
