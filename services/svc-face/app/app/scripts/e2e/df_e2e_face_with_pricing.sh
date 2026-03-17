#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# DesiFaces svc-face E2E Test (T2I + optional I2I + Pricing + Balance Semantics)
#
# File:
#   services/svc-face/app/app/scripts/e2e/df_e2e_face_with_pricing.sh
#
# Validates:
#   1) svc-pricing health
#   2) login
#   3) T2I create -> reserved -> succeeded -> committed
#   4) optional I2I create -> reserved -> succeeded -> committed
#   5) before/after credit balance
#   6) reservation row in pricing_credit_reservations
#   7) billed units / amount / ledger entry / billing mode semantics
#
# Important:
#   - prepaid/wallet: credit delta is authoritative
#   - postpaid/invoice: money charge is authoritative
#   - this script reports:
#       * actual_units
#       * billed_units
#       * charged_units
#       * charged_credits
#       * charged_money
#       * billing_mode
#       * settlement_mode
#       * billing_account_id
# ==============================================================================

command -v curl   >/dev/null || { echo "Missing: curl"; exit 1; }
command -v jq     >/dev/null || { echo "Missing: jq"; exit 1; }
command -v docker >/dev/null || { echo "Missing: docker"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../../../.." && pwd)"

CORE_BASE="${CORE_BASE:-http://localhost:8000}"
FACE_BASE="${FACE_BASE:-http://localhost:8003}"
PRICING_BASE="${PRICING_BASE:-http://localhost:8009}"

DF_EMAIL="${DF_EMAIL:-user1@desifaces.ai}"
DF_PASSWORD="${DF_PASSWORD:-password1}"

IMG_LOCAL="${IMG_LOCAL:-${REPO_ROOT}/tmp/download.png}"
RUN_I2I="${RUN_I2I:-auto}"   # auto | 0 | 1

NUM_VARIANTS="${NUM_VARIANTS:-2}"
POLL_SECS="${POLL_SECS:-2}"
TIMEOUT_SECS="${TIMEOUT_SECS:-240}"

T2I_PROMPT="${T2I_PROMPT:-confident Manipur traditional, vibrant colors, intricate patterns, traditional jewelry, thick jasmine flower mala, ornate hair accessories, natural lighting, sharp focus, professional resolution, professional photography}"
I2I_PROMPT="${I2I_PROMPT:-Same person, same identity. Change outfit to a professional Tamilnadu-inspired maroon saree with subtle bright gold border, thick jasmine flower mala. Change background to a traditional Tamilnadu wedding setting. Keep face, age, hair, skin tone unchanged.}"

PRESERVATION_STRENGTH="${PRESERVATION_STRENGTH:-0.85}"

OUT_DIR="${OUT_DIR:-/tmp/df_face_pricing_e2e_$(date +%s)}"
mkdir -p "$OUT_DIR"

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" >&2; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ------------------------------------------------------------------------------
# Generic helpers
# ------------------------------------------------------------------------------
check_http_200() {
  local url="$1"
  local label="$2"
  local out="${OUT_DIR}/$(echo "$label" | tr ' /:' '___').txt"

  curl -sS -o "$out" -w "\nHTTP=%{http_code}\n" "$url" \
    | tail -n1 | grep -q "HTTP=200" || {
      log "$label failed at $url"
      cat "$out" >&2 || true
      die "$label failed"
    }
}

login() {
  local login_json="$OUT_DIR/login.json"

  log "Logging in via CORE_BASE=${CORE_BASE} as ${DF_EMAIL} ..."

  curl -sS -o "$login_json" -w "\nHTTP=%{http_code}\n" \
    -X POST "$CORE_BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$DF_EMAIL\",\"password\":\"$DF_PASSWORD\"}" \
    | tail -n1 | grep -q "HTTP=200" || {
      log "Login response:"
      cat "$login_json" | jq >&2 || cat "$login_json" >&2
      die "Login failed"
    }

  DF_TOKEN="$(jq -r '.access_token // .token // empty' "$login_json")"
  [ -n "${DF_TOKEN:-}" ] || {
    cat "$login_json" | jq >&2 || true
    die "Could not extract access_token"
  }

  AUTH=(-H "Authorization: Bearer $DF_TOKEN")
  log "Login OK."
}

check_pricing_health() {
  log "Checking svc-pricing health at $PRICING_BASE ..."
  check_http_200 "${PRICING_BASE}/api/health" "pricing_health"
  log "svc-pricing health OK."
}

# ------------------------------------------------------------------------------
# Credits / balance helpers
# ------------------------------------------------------------------------------
save_balance_snapshot() {
  local label="$1"
  local out_json="$OUT_DIR/balance_${label}.json"

  curl -sS -o "$out_json" -w "\nHTTP=%{http_code}\n" \
    -X GET "$PRICING_BASE/api/credits/balance" \
    "${AUTH[@]}" \
    | tail -n1 | grep -q "HTTP=200" || {
      log "Balance response [$label]:"
      cat "$out_json" | jq >&2 || cat "$out_json" >&2
      die "Balance fetch failed for label=$label"
    }

  echo "$out_json"
}

lower_ascii() {
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'
}

log_balance_snapshot() {
  local label="$1"
  local snap_json
  snap_json="$(save_balance_snapshot "$label")"

  local balance reserved available
  balance="$(jq -r '.balance_credits // 0' "$snap_json")"
  reserved="$(jq -r '.reserved_credits // 0' "$snap_json")"
  available="$(jq -r '.available_credits // 0' "$snap_json")"

  log "Credit balance [$label]"
  log "  balance_credits  = $balance"
  log "  reserved_credits = $reserved"
  log "  available_credits= $available"

  echo "$snap_json"
}

summarize_balance_delta() {
  local before_json="$1"
  local after_json="$2"
  local label="$3"

  local before_balance before_reserved before_available
  local after_balance after_reserved after_available

  before_balance="$(jq -r '.balance_credits // 0' "$before_json")"
  before_reserved="$(jq -r '.reserved_credits // 0' "$before_json")"
  before_available="$(jq -r '.available_credits // 0' "$before_json")"

  after_balance="$(jq -r '.balance_credits // 0' "$after_json")"
  after_reserved="$(jq -r '.reserved_credits // 0' "$after_json")"
  after_available="$(jq -r '.available_credits // 0' "$after_json")"

  log "Credit delta [$label]"
  log "  balance_credits  : ${before_balance} -> ${after_balance} (delta=$((after_balance - before_balance)))"
  log "  reserved_credits : ${before_reserved} -> ${after_reserved} (delta=$((after_reserved - before_reserved)))"
  log "  available_credits: ${before_available} -> ${after_available} (delta=$((after_available - before_available)))"
}

# ------------------------------------------------------------------------------
# Face helpers
# ------------------------------------------------------------------------------
upload_image() {
  local file_path="$1"
  [ -f "$file_path" ] || die "Missing image file: $file_path"

  local out_json="$OUT_DIR/upload_$(basename "$file_path").json"
  log "Uploading image: $file_path"

  curl -sS -o "$out_json" -w "\nHTTP=%{http_code}\n" \
    -X POST "$FACE_BASE/api/face/assets/upload" \
    "${AUTH[@]}" \
    -F "file=@${file_path}" \
    | tail -n1 | grep -q "HTTP=200" || {
      log "Upload response:"
      cat "$out_json" | jq >&2 || cat "$out_json" >&2
      die "Upload failed"
    }

  local url
  url="$(jq -r '.image_url // .source_image_url // .url // .data.url // empty' "$out_json")"
  [ -n "$url" ] || {
    cat "$out_json" | jq >&2 || true
    die "Upload succeeded but no image_url returned"
  }

  echo "$url"
}

create_job() {
  local payload_json="$1"
  local label="$2"
  local out_json="$OUT_DIR/create_job_${label}.json"

  curl -sS -o "$out_json" -w "\nHTTP=%{http_code}\n" \
    -X POST "$FACE_BASE/api/face/creator/generate" \
    "${AUTH[@]}" \
    -H "Content-Type: application/json" \
    -d "$payload_json" \
    | tail -n1 | grep -q "HTTP=200" || {
      log "Create job response ($label):"
      cat "$out_json" | jq >&2 || cat "$out_json" >&2
      die "Create job failed for $label"
    }

  local job_id
  job_id="$(jq -r '.job_id // empty' "$out_json")"
  [ -n "$job_id" ] || {
    cat "$out_json" | jq >&2 || true
    die "No job_id in create response for $label"
  }

  echo "$job_id"
}

poll_job() {
  local job_id="$1"
  local start_ts now_ts elapsed status_json st
  start_ts="$(date +%s)"

  while true; do
    now_ts="$(date +%s)"
    elapsed="$((now_ts - start_ts))"
    if [ "$elapsed" -gt "$TIMEOUT_SECS" ]; then
      die "Timeout waiting for job $job_id after ${TIMEOUT_SECS}s"
    fi

    status_json="$OUT_DIR/status_${job_id}.json"

    curl -sS -o "$status_json" -w "HTTP=%{http_code}\n" \
      -X GET "$FACE_BASE/api/face/creator/jobs/${job_id}/status" \
      "${AUTH[@]}" \
      | tail -n1 | grep -q "HTTP=200" || {
        log "Status poll failed; response:"
        cat "$status_json" | jq >&2 || cat "$status_json" >&2
        die "Status poll failed for $job_id"
      }

    st="$(jq -r '.status // empty' "$status_json")"
    log "Job $job_id status=$st (elapsed ${elapsed}s)"

    if [ "$st" = "succeeded" ]; then
      echo "$status_json"
      return 0
    fi

    if [ "$st" = "failed" ] || [ "$st" = "cancelled" ]; then
      log "Final status payload:"
      cat "$status_json" | jq >&2 || cat "$status_json" >&2
      die "Job $job_id ended with status=$st"
    fi

    sleep "$POLL_SECS"
  done
}

download_variants() {
  local status_json="$1"
  local prefix="$2"

  local n
  n="$(jq -r '.variants | length' "$status_json")"
  [ "${n:-0}" -gt 0 ] || die "No variants in status response for prefix=$prefix"

  log "Downloading $n variants for $prefix ..."
  for i in $(seq 0 $((n - 1))); do
    local url out
    url="$(jq -r ".variants[$i].image_url // empty" "$status_json")"
    [ -n "$url" ] || {
      log "Missing image_url for variant index $i"
      continue
    }
    out="$OUT_DIR/${prefix}_variant_$((i + 1)).jpg"
    curl -sL "$url" -o "$out"
    log "Saved: $out ($(ls -lh "$out" | awk '{print $5}'))"
  done
}

# ------------------------------------------------------------------------------
# DB helpers: studio_jobs pricing block
# ------------------------------------------------------------------------------
db_pricing_row_json() {
  local job_id="$1"

  docker exec -e JOB_ID="$job_id" -i "$DB_CONTAINER" sh -lc '
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "
SELECT json_build_object(
  '\''id'\'', id::text,
  '\''status'\'', status,
  '\''payload_pricing'\'', payload_json->'\''pricing'\'',
  '\''meta_pricing'\'', meta_json->'\''pricing'\'',
  '\''error_code'\'', error_code,
  '\''error_message'\'', error_message
)
FROM public.studio_jobs
WHERE id = '\''${JOB_ID}'\''::uuid
LIMIT 1;
"'
}

save_db_snapshot() {
  local job_id="$1"
  local label="$2"
  local out_json="$OUT_DIR/db_${label}_${job_id}.json"

  db_pricing_row_json "$job_id" > "$out_json"
  [ -s "$out_json" ] || die "DB snapshot empty for job=$job_id label=$label"

  echo "$out_json"
}

assert_pricing_state() {
  local job_id="$1"
  local expected_state="$2"
  local label="$3"

  local snap_json
  snap_json="$(save_db_snapshot "$job_id" "$label")"

  log "DB pricing snapshot [$label] for job=$job_id:"
  cat "$snap_json" | jq >&2 || cat "$snap_json" >&2

  local payload_state meta_state status
  payload_state="$(jq -r '.payload_pricing.state // empty' "$snap_json")"
  meta_state="$(jq -r '.meta_pricing.state // empty' "$snap_json")"
  status="$(jq -r '.status // empty' "$snap_json")"

  [ "$payload_state" = "$expected_state" ] || die "Expected payload_pricing.state=$expected_state for job=$job_id, got=$payload_state"
  [ "$meta_state" = "$expected_state" ] || die "Expected meta_pricing.state=$expected_state for job=$job_id, got=$meta_state"

  log "Pricing state OK for job=$job_id label=$label status=$status state=$expected_state"
}

assert_committed_fields() {
  local job_id="$1"
  local label="$2"

  local snap_json
  snap_json="$(save_db_snapshot "$job_id" "$label")"

  local state actual_units ledger_entry_id commit_status
  state="$(jq -r '.payload_pricing.state // empty' "$snap_json")"
  actual_units="$(jq -r '.payload_pricing.actual_units // empty' "$snap_json")"
  ledger_entry_id="$(jq -r '.payload_pricing.ledger_entry_id // empty' "$snap_json")"
  commit_status="$(jq -r '.payload_pricing.commit_status // empty' "$snap_json")"

  [ "$state" = "committed" ] || die "Expected committed state for job=$job_id, got=$state"
  [ -n "$actual_units" ] || die "Missing actual_units for committed job=$job_id"
  [ -n "$ledger_entry_id" ] || die "Missing ledger_entry_id for committed job=$job_id"
  [ -n "$commit_status" ] || die "Missing commit_status for committed job=$job_id"

  log "Commit fields OK for job=$job_id actual_units=$actual_units ledger_entry_id=$ledger_entry_id commit_status=$commit_status"
}

# ------------------------------------------------------------------------------
# DB helpers: pricing reservation row
# ------------------------------------------------------------------------------
extract_reservation_id_from_job() {
  local job_id="$1"
  local snap_json
  snap_json="$(save_db_snapshot "$job_id" "reservation_lookup")"

  jq -r '.payload_pricing.reservation_id // empty' "$snap_json"
}

db_reservation_row_json() {
  local reservation_id="$1"

  docker exec -e RESERVATION_ID="$reservation_id" -i "$DB_CONTAINER" sh -lc '
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "
SELECT json_build_object(
  '\''id'\'', id::text,
  '\''status'\'', status,
  '\''billing_account_id'\'', billing_account_id::text,
  '\''settlement_mode'\'', settlement_mode,
  '\''reserved_credits'\'', reserved_credits,
  '\''expires_at'\'', coalesce(expires_at::text, '\'''\'' ),
  '\''currency'\'', coalesce(currency, '\''USD'\''),
  '\''estimated_money'\'', coalesce(estimated_money::text, '\''0'\''),
  '\''channel'\'', coalesce(channel, '\'''\'' ),
  '\''country_code'\'', coalesce(country_code, '\'''\'' ),
  '\''quote_json'\'', quote_json
)
FROM public.pricing_credit_reservations
WHERE id = '\''${RESERVATION_ID}'\''::uuid
LIMIT 1;
"'
}

save_reservation_snapshot() {
  local reservation_id="$1"
  local label="$2"
  local out_json="$OUT_DIR/reservation_${label}_${reservation_id}.json"

  db_reservation_row_json "$reservation_id" > "$out_json"
  [ -s "$out_json" ] || die "Reservation snapshot empty for reservation_id=$reservation_id label=$label"

  echo "$out_json"
}

reservation_summary() {
  local job_id="$1"
  local label="$2"

  local reservation_id
  reservation_id="$(extract_reservation_id_from_job "$job_id")"
  [ -n "$reservation_id" ] || die "No reservation_id found in studio_jobs pricing block for job=$job_id"

  local snap_json
  snap_json="$(save_reservation_snapshot "$reservation_id" "$label")"

  local res_status reserved_credits currency estimated_money
  local billing_mode settlement_mode billing_account_id hold_applied
  local variant_code category charged_money charged_credits charged_units ledger_entry_id

  res_status="$(jq -r '.status // empty' "$snap_json")"
  reserved_credits="$(jq -r '.reserved_credits // 0' "$snap_json")"
  currency="$(jq -r '.currency // "USD"' "$snap_json")"
  estimated_money="$(jq -r '.estimated_money // "0"' "$snap_json")"
  settlement_mode="$(jq -r '.settlement_mode // .quote_json.settlement_mode // empty' "$snap_json")"
  billing_account_id="$(jq -r '.billing_account_id // .quote_json.billing_account_id // empty' "$snap_json")"

  billing_mode="$(jq -r '
    .quote_json.billing_mode_snapshot
    // .quote_json.billing_mode
    // .quote_json.gate_billing_mode
    // .quote_json.economics_final.billing_mode
    // .quote_json.economics.billing_mode
    // empty
  ' "$snap_json")"

  hold_applied="$(jq -r '
    .quote_json.hold_applied
    // .quote_json.finalize.hold_applied
    // false
  ' "$snap_json")"

  variant_code="$(jq -r '.quote_json.variant_code // .quote_json.sku_code // empty' "$snap_json")"
  category="$(jq -r '.quote_json.category // .quote_json.service_name // empty' "$snap_json")"

  charged_money="$(jq -r '
    .quote_json.final_charged_money
    // .quote_json.charged_money
    // .quote_json.amount
    // .quote_json.economics_final.charged_money
    // .quote_json.economics_final.amount
    // .quote_json.economics.charged_money
    // .quote_json.economics.amount
    // empty
  ' "$snap_json")"

  charged_credits="$(jq -r '
    .quote_json.final_charged_credits
    // .quote_json.finalize.final_charged_credits
    // .quote_json.economics_final.charged_credits
    // .quote_json.economics.charged_credits
    // "0"
  ' "$snap_json")"

  charged_units="$(jq -r '
    .quote_json.billed_units
    // .quote_json.actual_units
    // .quote_json.finalize.actuals.actual_units
    // "0"
  ' "$snap_json")"

  ledger_entry_id="$(jq -r '
    .quote_json.ledger_entry_id
    // .quote_json.economics_final.ledger_entry_id
    // .quote_json.economics.ledger_entry_id
    // empty
  ' "$snap_json")"

  log "Reservation summary [$label] for job=$job_id"
  log "  reservation_id  = $reservation_id"
  log "  status          = ${res_status:-<empty>}"
  log "  billing_account_id = ${billing_account_id:-<empty>}"
  log "  settlement_mode = ${settlement_mode:-<empty>}"
  log "  reserved_credits= ${reserved_credits:-<empty>}"
  log "  billing_mode    = ${billing_mode:-<empty>}"
  log "  hold_applied    = ${hold_applied:-<empty>}"
  log "  variant_code    = ${variant_code:-<empty>}"
  log "  category        = ${category:-<empty>}"
  log "  estimated_money = ${estimated_money:-<empty>} ${currency}"
  log "  charged_money   = ${charged_money:-<empty>} ${currency}"
  log "  charged_units   = ${charged_units:-<empty>}"
  log "  charged_credits = ${charged_credits:-<empty>}"
  log "  ledger_entry_id = ${ledger_entry_id:-<empty>}"

  echo "$snap_json"
}

job_consumption_summary() {
  local job_id="$1"
  local label="$2"

  local snap_json
  snap_json="$(save_db_snapshot "$job_id" "$label")"

  local state reservation_id actual_units billed_units amount currency ledger_entry_id
  state="$(jq -r '.payload_pricing.state // empty' "$snap_json")"
  reservation_id="$(jq -r '.payload_pricing.reservation_id // empty' "$snap_json")"
  actual_units="$(jq -r '.payload_pricing.actual_units // empty' "$snap_json")"
  billed_units="$(jq -r '.payload_pricing.billed_units // empty' "$snap_json")"
  amount="$(jq -r '.payload_pricing.amount // empty' "$snap_json")"
  currency="$(jq -r '.payload_pricing.currency // "USD"' "$snap_json")"
  ledger_entry_id="$(jq -r '.payload_pricing.ledger_entry_id // empty' "$snap_json")"

  log "Consumption summary [$label] for job=$job_id"
  log "  state          = ${state:-<empty>}"
  log "  reservation_id = ${reservation_id:-<empty>}"
  log "  actual_units   = ${actual_units:-<empty>}"
  log "  billed_units   = ${billed_units:-<empty>}"
  log "  amount         = ${amount:-<empty>} ${currency}"
  log "  ledger_entry_id= ${ledger_entry_id:-<empty>}"
}

validate_balance_semantics() {
  local reservation_json="$1"
  local before_json="$2"
  local after_json="$3"
  local label="$4"

  local billing_mode settlement_mode hold_applied charged_money charged_credits
  local before_balance after_balance before_available after_available

  billing_mode="$(jq -r '
    .quote_json.billing_mode_snapshot
    // .quote_json.billing_mode
    // .quote_json.gate_billing_mode
    // .quote_json.economics_final.billing_mode
    // .quote_json.economics.billing_mode
    // empty
  ' "$reservation_json")"

  settlement_mode="$(jq -r '
    .settlement_mode
    // .quote_json.settlement_mode
    // .quote_json.finalize.settlement_mode_effective
    // empty
  ' "$reservation_json")"

  hold_applied="$(jq -r '
    .quote_json.hold_applied
    // .quote_json.finalize.hold_applied
    // false
  ' "$reservation_json")"

  charged_money="$(jq -r '
    .quote_json.final_charged_money
    // .quote_json.charged_money
    // .quote_json.amount
    // .quote_json.economics_final.charged_money
    // .quote_json.economics.charged_money
    // "0"
  ' "$reservation_json")"

  charged_credits="$(jq -r '
    .quote_json.final_charged_credits
    // .quote_json.finalize.final_charged_credits
    // .quote_json.economics_final.charged_credits
    // .quote_json.economics.charged_credits
    // "0"
  ' "$reservation_json")"

  before_balance="$(jq -r '.balance_credits // 0' "$before_json")"
  after_balance="$(jq -r '.balance_credits // 0' "$after_json")"
  before_available="$(jq -r '.available_credits // 0' "$before_json")"
  after_available="$(jq -r '.available_credits // 0' "$after_json")"

  local delta_balance delta_available
  delta_balance=$((after_balance - before_balance))
  delta_available=$((after_available - before_available))

  case "$(lower_ascii "${billing_mode:-}")" in
    bill|postpaid|invoice|money)
      case "$(lower_ascii "${settlement_mode:-}")" in
        postpaid|invoice)
          log "Validation [$label]: billing_mode=${billing_mode}, settlement_mode=${settlement_mode}; zero credit delta is expected. Money charge is authoritative."
          ;;
        prepaid|wallet|credit|credits|payg)
          if [ "$delta_balance" -eq 0 ] && [ "$delta_available" -eq 0 ]; then
            log "WARNING [$label]: billing_mode=${billing_mode}, settlement_mode=${settlement_mode}; expected wallet delta but none observed."
            log "WARNING [$label]: charged_credits=${charged_credits}, charged_money=${charged_money}, hold_applied=${hold_applied}"
          else
            log "Validation [$label]: billing_mode=${billing_mode}, settlement_mode=${settlement_mode}; wallet delta observed."
          fi
          ;;
        *)
          if [ "$(lower_ascii "$hold_applied")" = "true" ]; then
            if [ "$delta_balance" -eq 0 ] && [ "$delta_available" -eq 0 ]; then
              log "WARNING [$label]: hold_applied=true but wallet delta is unchanged."
            else
              log "Validation [$label]: hold_applied=true; wallet delta observed."
            fi
          else
            log "Validation [$label]: billing_mode=${billing_mode}; zero credit delta is acceptable when postpaid-like."
          fi
          ;;
      esac
      ;;
    credit|credits|wallet|prepaid)
      if [ "$delta_balance" -eq 0 ] && [ "$delta_available" -eq 0 ]; then
        log "WARNING [$label]: billing_mode=${billing_mode} but balance delta is unchanged."
        log "WARNING [$label]: charged_credits=${charged_credits}, charged_money=${charged_money}, hold_applied=${hold_applied}"
      else
        log "Validation [$label]: billing_mode=${billing_mode}; wallet delta observed."
      fi
      ;;
    free)
      log "Validation [$label]: billing_mode=free; zero delta is expected."
      ;;
    *)
      log "Validation [$label]: billing_mode unresolved; using per-job committed amount/units as source of truth."
      ;;
  esac
}

# ------------------------------------------------------------------------------
# Test flows
# ------------------------------------------------------------------------------
run_t2i() {
  log "=== T2I E2E + Pricing ==="

  local balance_before_json
  balance_before_json="$(log_balance_snapshot "before_t2i")"

  local payload
  payload="$(jq -cn \
    --arg language "en" \
    --arg user_prompt "$T2I_PROMPT" \
    --argjson num_variants "$NUM_VARIANTS" \
    '{
      language: $language,
      user_prompt: $user_prompt,
      num_variants: $num_variants,
      mode: "text-to-image",
      age_range_code: "established_professional",
      skin_tone_code: "medium_brown",
      region_code: "kerala",
      gender: "female",
      image_format_code: "instagram_portrait",
      use_case_code: "brand_ambassador",
      style_code: "professional",
      context_code: "studio_headshot"
    }'
  )"

  local job_id
  job_id="$(create_job "$payload" "t2i")"
  log "T2I job_id=$job_id"

  assert_pricing_state "$job_id" "reserved" "after_create_t2i"

  local status_json
  status_json="$(poll_job "$job_id")"

  assert_pricing_state "$job_id" "committed" "after_success_t2i"
  assert_committed_fields "$job_id" "after_success_t2i"
  job_consumption_summary "$job_id" "after_success_t2i"

  local reservation_json
  reservation_json="$(reservation_summary "$job_id" "after_success_t2i")"

  local balance_after_json
  balance_after_json="$(log_balance_snapshot "after_t2i")"
  summarize_balance_delta "$balance_before_json" "$balance_after_json" "t2i"
  validate_balance_semantics "$reservation_json" "$balance_before_json" "$balance_after_json" "t2i"

  download_variants "$status_json" "t2i_${job_id}"
}

run_i2i() {
  log "=== I2I E2E + Pricing ==="

  local balance_before_json
  balance_before_json="$(log_balance_snapshot "before_i2i")"

  local src_url
  src_url="$(upload_image "$IMG_LOCAL")"
  log "SRC_URL=$src_url"

  local payload
  payload="$(jq -cn \
    --arg language "en" \
    --arg user_prompt "$I2I_PROMPT" \
    --argjson num_variants "$NUM_VARIANTS" \
    --arg source_image_url "$src_url" \
    --argjson preservation_strength "$PRESERVATION_STRENGTH" \
    '{
      language: $language,
      user_prompt: $user_prompt,
      num_variants: $num_variants,
      mode: "image-to-image",
      source_image_url: $source_image_url,
      preservation_strength: $preservation_strength,
      region_code: "kerala",
      gender: "female",
      use_case_code: "brand_ambassador",
      style_code: "professional",
      context_code: "modern_office"
    }'
  )"

  local job_id
  job_id="$(create_job "$payload" "i2i")"
  log "I2I job_id=$job_id"

  assert_pricing_state "$job_id" "reserved" "after_create_i2i"

  local status_json
  status_json="$(poll_job "$job_id")"

  assert_pricing_state "$job_id" "committed" "after_success_i2i"
  assert_committed_fields "$job_id" "after_success_i2i"
  job_consumption_summary "$job_id" "after_success_i2i"

  local reservation_json
  reservation_json="$(reservation_summary "$job_id" "after_success_i2i")"

  local balance_after_json
  balance_after_json="$(log_balance_snapshot "after_i2i")"
  summarize_balance_delta "$balance_before_json" "$balance_after_json" "i2i"
  validate_balance_semantics "$reservation_json" "$balance_before_json" "$balance_after_json" "i2i"

  download_variants "$status_json" "i2i_${job_id}"
}

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
log "OUT_DIR=$OUT_DIR"
check_pricing_health
login

INITIAL_BALANCE_JSON="$(log_balance_snapshot "before_all")"

run_t2i

case "$RUN_I2I" in
  1|true|TRUE|yes|YES)
    [ -f "$IMG_LOCAL" ] || die "RUN_I2I is enabled but IMG_LOCAL is missing: $IMG_LOCAL"
    run_i2i
    ;;
  0|false|FALSE|no|NO)
    log "RUN_I2I disabled; skipping I2I."
    ;;
  auto|AUTO|"")
    if [ -f "$IMG_LOCAL" ]; then
      run_i2i
    else
      log "IMG_LOCAL not found at $IMG_LOCAL ; skipping I2I. Set IMG_LOCAL=/absolute/path/to/image to enable."
    fi
    ;;
  *)
    die "Invalid RUN_I2I value: $RUN_I2I (expected auto|0|1)"
    ;;
esac

FINAL_BALANCE_JSON="$(log_balance_snapshot "after_all")"
summarize_balance_delta "$INITIAL_BALANCE_JSON" "$FINAL_BALANCE_JSON" "overall"

log "✅ DONE. Outputs, DB pricing snapshots, reservation snapshots, and balance snapshots saved in: $OUT_DIR"