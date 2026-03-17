#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# DesiFaces svc-fusion E2E Test (Pricing-aware: postpaid + prepaid)
#
# File:
#   services/svc-fusion/app/app/scripts/e2e/df_e2e_fusion_with_pricing.sh
#
# Validates:
#   1) login via svc-core
#   2) create fusion job using face/audio artifact IDs
#   3) poll to succeeded
#   4) pricing block on studio_jobs payload/meta
#   5) pricing reservation snapshot in pricing_credit_reservations
#   6) postpaid semantics for user2
#   7) prepaid semantics for user1
#
# Default account mapping:
#   - user2@desifaces.ai => postpaid
#   - user1@desifaces.ai => prepaid
#
# Notes:
#   - Artifact resolution uses latest shared face/audio artifacts from DB.
#   - If you already know artifact IDs, set FACE_ARTIFACT_ID / AUDIO_ARTIFACT_ID.
#   - If your install requires a known talking_photo_id, set HEYGEN_TALKING_PHOTO_ID.
# ==============================================================================

CORE_BASE="${CORE_BASE:-http://localhost:8000}"
FUSION_BASE="${FUSION_BASE:-http://localhost:8002}"

USER1_EMAIL="${USER1_EMAIL:-user1@desifaces.ai}"
USER2_EMAIL="${USER2_EMAIL:-user2@desifaces.ai}"

USER1_PASSWORD="${USER1_PASSWORD:-${DF_PASSWORD:-}}"
USER2_PASSWORD="${USER2_PASSWORD:-${DF_PASSWORD:-}}"

: "${USER1_PASSWORD:?USER1_PASSWORD or DF_PASSWORD is required}"
: "${USER2_PASSWORD:?USER2_PASSWORD or DF_PASSWORD is required}"

VOICE_MODE="${VOICE_MODE:-audio}"                 # audio | tts
EXTERNAL_PROVIDER_OK="${EXTERNAL_PROVIDER_OK:-true}"
ASPECT_RATIO="${ASPECT_RATIO:-9:16}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
POLL_SECONDS="${POLL_SECONDS:-3}"

# Optional overrides
FACE_ARTIFACT_ID="${FACE_ARTIFACT_ID:-}"
AUDIO_ARTIFACT_ID="${AUDIO_ARTIFACT_ID:-}"
HEYGEN_TALKING_PHOTO_ID="${HEYGEN_TALKING_PHOTO_ID:-}"

# Optional TTS mode fields
TTS_VOICE_ID="${TTS_VOICE_ID:-}"
TTS_SCRIPT="${TTS_SCRIPT:-}"

OUT_DIR="${OUT_DIR:-/tmp/df_fusion_pricing_e2e_$(date +%s)}"
mkdir -p "$OUT_DIR"

bool_norm() {
  local v="${1:-}"
  v="$(echo "$v" | tr '[:upper:]' '[:lower:]' | xargs)"
  if [[ "$v" == "1" || "$v" == "true" || "$v" == "yes" || "$v" == "y" ]]; then
    echo "true"
  else
    echo "false"
  fi
}

now_epoch() { date +%s; }

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

EXTERNAL_PROVIDER_OK="$(bool_norm "$EXTERNAL_PROVIDER_OK")"

decode_jwt_sub() {
  python3 - "$1" <<'PY'
import base64, json, sys
token = sys.argv[1]
try:
    parts = token.split(".")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    obj = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    print(obj.get("sub") or "")
except Exception:
    print("")
PY
}

login_user() {
  local email="$1"
  local password="$2"
  local label="$3"

  local body
  body="$(jq -nc --arg email "$email" --arg password "$password" '{email:$email,password:$password}')"

  local resp
  resp="$(curl -sS -X POST "${CORE_BASE}/api/auth/login" -H "Content-Type: application/json" -d "$body")"
  echo "$resp" > "${OUT_DIR}/${label}_auth.json"

  local token
  token="$(echo "$resp" | jq -r '.access_token // .token // empty')"
  [[ -n "$token" ]] || die "login failed for ${email}; no access_token returned"

  local user_id
  user_id="$(echo "$resp" | jq -r '.user.id // .user_id // .id // empty')"
  if [[ -z "$user_id" ]]; then
    user_id="$(decode_jwt_sub "$token")"
  fi
  [[ -n "$user_id" ]] || die "login succeeded but user_id could not be resolved for ${email}"

  jq -nc --arg token "$token" --arg user_id "$user_id" '{token:$token,user_id:$user_id}'
}

resolve_latest_artifacts() {
  if [[ -n "$FACE_ARTIFACT_ID" && -n "$AUDIO_ARTIFACT_ID" ]]; then
    jq -nc \
      --arg face_artifact_id "$FACE_ARTIFACT_ID" \
      --arg audio_artifact_id "$AUDIO_ARTIFACT_ID" \
      '{face_artifact_id:$face_artifact_id,audio_artifact_id:$audio_artifact_id}'
    return
  fi

  docker exec -i df-svc-fusion python - <<'PY'
import asyncio, asyncpg, json
from app.config import settings

async def main():
    pool = await asyncpg.create_pool(settings.DATABASE_URL)
    try:
        async with pool.acquire() as conn:
            face = await conn.fetchrow("""
                SELECT id::text AS id, kind, url
                FROM public.artifacts
                WHERE kind IN ('face','image','face_image')
                ORDER BY created_at DESC
                LIMIT 1
            """)
            audio = await conn.fetchrow("""
                SELECT id::text AS id, kind, url
                FROM public.artifacts
                WHERE kind = 'audio'
                ORDER BY created_at DESC
                LIMIT 1
            """)
        out = {
            "face_artifact_id": face["id"] if face else None,
            "audio_artifact_id": audio["id"] if audio else None,
        }
        print(json.dumps(out))
    finally:
        await pool.close()

asyncio.run(main())
PY
}

build_payload() {
  local face_artifact_id="$1"
  local audio_artifact_id="$2"

  python3 - "$face_artifact_id" "$audio_artifact_id" "$VOICE_MODE" "$EXTERNAL_PROVIDER_OK" "$ASPECT_RATIO" "$HEYGEN_TALKING_PHOTO_ID" "$TTS_VOICE_ID" "$TTS_SCRIPT" <<'PY'
import json, sys

face_artifact_id = sys.argv[1]
audio_artifact_id = sys.argv[2]
voice_mode = sys.argv[3]
external_provider_ok = sys.argv[4].lower() == "true"
aspect_ratio = sys.argv[5]
heygen_talking_photo_id = sys.argv[6].strip()
tts_voice_id = sys.argv[7].strip()
tts_script = sys.argv[8].strip()

payload = {
    "face_artifact_id": face_artifact_id,
    "voice_mode": voice_mode,
    "consent": {"external_provider_ok": external_provider_ok},
    "video": {"aspect_ratio": aspect_ratio},
}

if heygen_talking_photo_id:
    payload["heygen_talking_photo_id"] = heygen_talking_photo_id

if voice_mode == "audio":
    payload["voice_audio"] = {
        "type": "audio",
        "audio_artifact_id": audio_artifact_id,
    }
elif voice_mode == "tts":
    if not tts_voice_id or not tts_script:
        raise SystemExit("TTS mode requires TTS_VOICE_ID and TTS_SCRIPT")
    payload["voice_tts"] = {
        "voice_id": tts_voice_id,
        "script": tts_script,
    }
else:
    raise SystemExit(f"Unsupported VOICE_MODE: {voice_mode}")

print(json.dumps(payload, ensure_ascii=False))
PY
}

query_pricing_snapshot() {
  local job_id="$1"

  docker exec -i df-svc-fusion python - "$job_id" <<'PY'
import asyncio, asyncpg, json, sys
from app.config import settings

job_id = sys.argv[1]

def as_dict(v):
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            vv = json.loads(v)
            return vv if isinstance(vv, dict) else {}
        except Exception:
            return {}
    try:
        if hasattr(v, "keys"):
            return {k: v[k] for k in v.keys()}
    except Exception:
        pass
    try:
        vv = dict(v)
        return vv if isinstance(vv, dict) else {}
    except Exception:
        return {}

def row_to_dict(row):
    if not row:
        return None
    return {k: row[k] for k in row.keys()}

def pick_expr(cols, names, alias, cast=None, default_sql=None):
    for name in names:
        if name in cols:
            expr = name
            if cast:
                expr = f"{name}::{cast}"
            return f"{expr} AS {alias}"
    return default_sql or f"NULL AS {alias}"

async def main():
    pool = await asyncpg.create_pool(settings.DATABASE_URL)
    try:
        async with pool.acquire() as conn:
            job = await conn.fetchrow("""
                SELECT
                  id::text AS id,
                  user_id::text AS user_id,
                  studio_type,
                  status,
                  error_code,
                  error_message,
                  payload_json,
                  meta_json
                FROM public.studio_jobs
                WHERE id = $1::uuid
                  AND studio_type = 'fusion'
                LIMIT 1
            """, job_id)

            jobd = row_to_dict(job) or {}
            payload = as_dict(jobd.get("payload_json"))
            meta = as_dict(jobd.get("meta_json"))
            pricing = as_dict(payload.get("pricing")) or as_dict(meta.get("pricing"))

            reservation_id = str(pricing.get("reservation_id") or "").strip()

            # ------------------------------------------------------------------
            # Reservation lookup: prefer pricing.reservation_id, fallback to job_ref
            # ------------------------------------------------------------------
            reservation = None

            res_cols_rows = await conn.fetch("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'pricing_credit_reservations'
            """)
            res_cols = {r["column_name"] for r in res_cols_rows}

            reservation_select = """
                SELECT
                  id::text AS reservation_id,
                  user_id::text AS reservation_user_id,
                  status,
                  billing_account_id::text AS billing_account_id,
                  settlement_mode,
                  reserved_credits,
                  estimated_money,
                  currency,
                  tier_code,
                  service_name,
                  service_action,
                  sku_code,
                  job_ref,
                  expires_at,
                  finalized_at,
                  created_at,
                  updated_at,
                  quote_json
                FROM public.pricing_credit_reservations
            """

            if reservation_id:
                try:
                    reservation = await conn.fetchrow(
                        reservation_select + """
                        WHERE id = $1::uuid
                        LIMIT 1
                        """,
                        reservation_id,
                    )
                except Exception:
                    reservation = None

            if reservation is None and "job_ref" in res_cols:
                try:
                    reservation = await conn.fetchrow(
                        reservation_select + """
                        WHERE job_ref = $1::text
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        job_id,
                    )
                except Exception:
                    reservation = None

            reservationd = row_to_dict(reservation) or {}

            # ------------------------------------------------------------------
            # Optional ledger lookup by reservation_id
            # ------------------------------------------------------------------
            ledger = None
            ledger_table_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'pricing_credit_ledger_events'
                )
            """)

            if ledger_table_exists and reservationd.get("reservation_id"):
                led_cols_rows = await conn.fetch("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'pricing_credit_ledger_events'
                """)
                led_cols = {r["column_name"] for r in led_cols_rows}

                order_col = "created_at" if "created_at" in led_cols else "id"

                ledger_select_parts = [
                    pick_expr(led_cols, ["id"], "ledger_event_id", cast="text", default_sql="NULL::text AS ledger_event_id"),
                    pick_expr(led_cols, ["reservation_id"], "reservation_id", cast="text", default_sql="NULL::text AS reservation_id"),
                    pick_expr(led_cols, ["event_type", "entry_type", "kind"], "event_type", default_sql="NULL::text AS event_type"),
                    pick_expr(led_cols, ["credits_delta"], "credits_delta", default_sql="NULL::numeric AS credits_delta"),
                    pick_expr(led_cols, ["money_amount", "amount"], "money_amount", default_sql="NULL::numeric AS money_amount"),
                    pick_expr(led_cols, ["currency"], "ledger_currency", default_sql="NULL::text AS ledger_currency"),
                    pick_expr(led_cols, ["billing_account_id"], "ledger_billing_account_id", cast="text", default_sql="NULL::text AS ledger_billing_account_id"),
                    pick_expr(led_cols, ["created_at"], "ledger_created_at", default_sql="NULL::timestamptz AS ledger_created_at"),
                    pick_expr(led_cols, ["meta_json"], "meta_json", default_sql="'{}'::jsonb AS meta_json"),
                ]

                if "reservation_id" in led_cols:
                    try:
                        ledger = await conn.fetchrow(f"""
                            SELECT
                              {", ".join(ledger_select_parts)}
                            FROM public.pricing_credit_ledger_events
                            WHERE reservation_id = $1::uuid
                            ORDER BY {order_col} DESC
                            LIMIT 1
                        """, reservationd["reservation_id"])
                    except Exception:
                        ledger = None

        out = {
            "job": {
                "id": jobd.get("id"),
                "user_id": jobd.get("user_id"),
                "studio_type": jobd.get("studio_type"),
                "status": jobd.get("status"),
                "error_code": jobd.get("error_code"),
                "error_message": jobd.get("error_message"),
            },
            "pricing": pricing,
            "reservation": reservationd or None,
            "ledger": row_to_dict(ledger),
        }
        print(json.dumps(out, default=str))
    finally:
        await pool.close()

asyncio.run(main())
PY
}

validate_snapshot() {
  local label="$1"
  local expected_settlement="$2"
  local snapshot_path="$3"

  python3 - "$label" "$expected_settlement" "$snapshot_path" <<'PY'
import json, sys

label = sys.argv[1]
expected = sys.argv[2]
path = sys.argv[3]

with open(path, "r", encoding="utf-8") as f:
    obj = json.load(f)

job = obj.get("job") or {}
pricing = obj.get("pricing") or {}
reservation = obj.get("reservation") or {}

errors = []

job_status = str(job.get("status") or "")
studio_type = str(job.get("studio_type") or "")
pricing_state = str(pricing.get("state") or "")
reservation_status = str(reservation.get("status") or "")
settlement_mode = str(reservation.get("settlement_mode") or pricing.get("settlement_mode") or "")
billing_account_id = str(reservation.get("billing_account_id") or pricing.get("billing_account_id") or "")

if studio_type != "fusion":
    errors.append(f"job.studio_type expected 'fusion', got {studio_type!r}")

if job_status != "succeeded":
    errors.append(f"job.status expected succeeded, got {job_status!r}")

if pricing_state != "committed":
    errors.append(f"pricing.state expected committed, got {pricing_state!r}")

if reservation and reservation_status != "committed":
    errors.append(f"reservation.status expected committed, got {reservation_status!r}")

if settlement_mode != expected:
    errors.append(f"settlement_mode expected {expected!r}, got {settlement_mode!r}")

if not billing_account_id:
    errors.append("billing_account_id missing")

def as_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default

def as_bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y")

charged_credits = as_float(reservation.get("charged_credits"), 0.0)
hold_applied = as_bool(reservation.get("hold_applied"))

if expected == "postpaid":
    if charged_credits != 0.0:
        errors.append(f"postpaid expected charged_credits=0, got {charged_credits}")
    if hold_applied:
        errors.append("postpaid expected hold_applied=false")
elif expected == "prepaid":
    if charged_credits <= 0.0:
        errors.append(f"prepaid expected charged_credits>0, got {charged_credits}")
    if not hold_applied:
        errors.append("prepaid expected hold_applied=true")

if errors:
    print(f"VALIDATION FAILED [{label}]")
    for e in errors:
        print(f" - {e}")
    sys.exit(1)

print(f"Pricing validation OK [{label}]")
print(f"  job.studio_type    = {studio_type}")
print(f"  job.status         = {job_status}")
print(f"  pricing.state      = {pricing_state}")
print(f"  reservation.status = {reservation_status or '(none)'}")
print(f"  billing_account_id = {billing_account_id}")
print(f"  settlement_mode    = {settlement_mode}")
print(f"  charged_credits    = {reservation.get('charged_credits')}")
print(f"  hold_applied       = {reservation.get('hold_applied')}")
print(f"  ledger_entry_id    = {reservation.get('ledger_entry_id') or pricing.get('ledger_entry_id')}")
PY
}

download_video_if_present() {
  local label="$1"
  local status_json="$2"
  local job_id="$3"

  local video_url
  video_url="$(jq -r '.artifacts[]? | select(.kind=="video") | .url' "$status_json" | head -n 1 || true)"
  if [[ -z "$video_url" ]]; then
    log "WARN: no video artifact found for ${label} job=${job_id}"
    return 0
  fi

  local out="${OUT_DIR}/${label}_${job_id}.mp4"
  log "Downloading video for ${label}: ${out}"
  curl -sS -L -o "$out" "$video_url"

  if [[ ! -s "$out" ]]; then
    die "downloaded video is empty: ${out}"
  fi

  file "$out" || true
}

run_case() {
  local label="$1"
  local email="$2"
  local password="$3"
  local expected_settlement="$4"
  local face_artifact_id="$5"
  local audio_artifact_id="$6"

  log "------------------------------------------------------------"
  log "Running Fusion E2E for ${label} (${email}) expected=${expected_settlement}"
  log "------------------------------------------------------------"

  local auth_json token user_id
  auth_json="$(login_user "$email" "$password" "$label")"
  token="$(echo "$auth_json" | jq -r '.token')"
  user_id="$(echo "$auth_json" | jq -r '.user_id')"

  log "Logged in ${label}: user_id=${user_id}"

  local payload
  payload="$(build_payload "$face_artifact_id" "$audio_artifact_id")"
  echo "$payload" | jq > "${OUT_DIR}/${label}_payload.json"

  local create_resp create_path job_id
  create_path="${OUT_DIR}/${label}_create.json"
  create_resp="$(curl -sS -X POST "${FUSION_BASE}/jobs" \
    -H "Authorization: Bearer ${token}" \
    -H "X-User-Id: ${user_id}" \
    -H "Content-Type: application/json" \
    -d "$payload")"

  echo "$create_resp" | jq > "$create_path"
  job_id="$(echo "$create_resp" | jq -r '.job_id // empty')"
  [[ -n "$job_id" ]] || die "job_id missing in create response for ${label}"

  log "Created job ${job_id} for ${label}"

  local start status last_resp status_path
  start="$(now_epoch)"
  status=""
  last_resp=""
  status_path="${OUT_DIR}/${label}_status.json"

  while true; do
    last_resp="$(curl -sS "${FUSION_BASE}/jobs/${job_id}" \
      -H "Authorization: Bearer ${token}" \
      -H "X-User-Id: ${user_id}")"

    echo "$last_resp" | jq > "$status_path"
    status="$(echo "$last_resp" | jq -r '.status // empty')"

    if [[ "$status" == "succeeded" ]]; then
      log "Fusion job succeeded for ${label}: ${job_id}"
      break
    fi

    if [[ "$status" == "failed" ]]; then
      log "Fusion job failed for ${label}: ${job_id}"
      cat "$status_path"
      local failed_snapshot
      failed_snapshot="$(query_pricing_snapshot "$job_id")"
      echo "$failed_snapshot" | jq > "${OUT_DIR}/${label}_pricing_snapshot_failed.json"
      die "fusion job failed for ${label}"
    fi

    local now elapsed
    now="$(now_epoch)"
    elapsed=$((now - start))
    if (( elapsed >= TIMEOUT_SECONDS )); then
      local timeout_snapshot
      timeout_snapshot="$(query_pricing_snapshot "$job_id")"
      echo "$timeout_snapshot" | jq > "${OUT_DIR}/${label}_pricing_snapshot_timeout.json"
      die "timeout waiting for fusion job=${job_id} label=${label} status=${status:-unknown}"
    fi

    log "Polling ${label}: job=${job_id} status=${status:-unknown} elapsed=${elapsed}s"
    sleep "$POLL_SECONDS"
  done

  local snapshot snapshot_path
  snapshot="$(query_pricing_snapshot "$job_id")"
  snapshot_path="${OUT_DIR}/${label}_pricing_snapshot.json"
  echo "$snapshot" | jq > "$snapshot_path"

  validate_snapshot "$label" "$expected_settlement" "$snapshot_path"

  log "Fusion pricing snapshot [${label}]"
  jq '.pricing' "$snapshot_path"

  log "Reservation snapshot [${label}]"
  jq '.reservation' "$snapshot_path"

  download_video_if_present "$label" "$status_path" "$job_id"

  log "DONE ${label} job=${job_id}"
}

main() {
  log "OUT_DIR=${OUT_DIR}"
  log "CORE_BASE=${CORE_BASE}"
  log "FUSION_BASE=${FUSION_BASE}"
  log "VOICE_MODE=${VOICE_MODE}"
  log "ASPECT_RATIO=${ASPECT_RATIO}"
  log "EXTERNAL_PROVIDER_OK=${EXTERNAL_PROVIDER_OK}"

  if [[ "$VOICE_MODE" == "tts" ]]; then
    [[ -n "$TTS_VOICE_ID" ]] || die "TTS_VOICE_ID is required when VOICE_MODE=tts"
    [[ -n "$TTS_SCRIPT" ]] || die "TTS_SCRIPT is required when VOICE_MODE=tts"
  fi

  if [[ -z "$HEYGEN_TALKING_PHOTO_ID" ]]; then
    log "WARN: HEYGEN_TALKING_PHOTO_ID is not set."
    log "WARN: If your current Fusion runtime still requires explicit talking_photo_id, the run may fail after a long poll."
  fi

  local resolved face_artifact_id audio_artifact_id
  resolved="$(resolve_latest_artifacts)"
  echo "$resolved" | jq > "${OUT_DIR}/resolved_artifacts.json"

  face_artifact_id="$(echo "$resolved" | jq -r '.face_artifact_id // empty')"
  audio_artifact_id="$(echo "$resolved" | jq -r '.audio_artifact_id // empty')"

  [[ -n "$face_artifact_id" ]] || die "could not resolve face_artifact_id"
  if [[ "$VOICE_MODE" == "audio" ]]; then
    [[ -n "$audio_artifact_id" ]] || die "could not resolve audio_artifact_id"
  fi

  log "Resolved artifacts:"
  log "  face_artifact_id  = ${face_artifact_id}"
  if [[ "$VOICE_MODE" == "audio" ]]; then
    log "  audio_artifact_id = ${audio_artifact_id}"
  fi
  if [[ -n "$HEYGEN_TALKING_PHOTO_ID" ]]; then
    log "  heygen_talking_photo_id = ${HEYGEN_TALKING_PHOTO_ID}"
  fi

  # user2 => postpaid
  run_case "postpaid_user2" "$USER2_EMAIL" "$USER2_PASSWORD" "postpaid" "$face_artifact_id" "$audio_artifact_id"

  # user1 => prepaid
  run_case "prepaid_user1" "$USER1_EMAIL" "$USER1_PASSWORD" "prepaid" "$face_artifact_id" "$audio_artifact_id"

  log "✅ DONE. Fusion outputs, create/status payloads, auth, and pricing snapshots saved in: ${OUT_DIR}"
}

main "$@"