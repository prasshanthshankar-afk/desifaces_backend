#!/usr/bin/env bash
set -euo pipefail

# Required
: "${TOKEN:?Set TOKEN}"
BASE="${BASE:-http://localhost:8008}"
COMPOSE_ENV="${COMPOSE_ENV:-./infra/.env}"

# Optional knobs
WAIT_SECONDS="${WAIT_SECONDS:-60}"
SLEEP_SECONDS="${SLEEP_SECONDS:-2}"
IDEMPOTENCY_KEY="${IDEMPOTENCY_KEY:-demo-001}"

RUN_DIR="/tmp/df_commerce_one_time_wait_$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "$RUN_DIR"
HDR="$RUN_DIR/hdr.txt"
OUT="$RUN_DIR/out.bin"

log() { echo "$@"; }
save_json() { printf "%s" "$1" >"$2"; }

curl_req() {
  local method="$1" url="$2" data_file="${3:-}"
  local code

  if [[ -n "${data_file}" ]]; then
    code="$(
      curl -q -sS --max-time 30 --connect-timeout 5 \
        -D "$HDR" -o "$OUT" -w "%{http_code}" \
        -X "$method" "$url" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        --data-binary "@$data_file" || true
    )"
  else
    code="$(
      curl -q -sS --max-time 15 --connect-timeout 5 \
        -D "$HDR" -o "$OUT" -w "%{http_code}" \
        -X "$method" "$url" \
        -H "Authorization: Bearer $TOKEN" || true
    )"
  fi

  echo "$code"
}

preview_body() {
  head -c 300 "$OUT" 2>/dev/null || true
}

json_get() {
  local jqexpr="$1"
  jq -r "$jqexpr" "$OUT" 2>/dev/null || true
}

log "RUN_DIR=$RUN_DIR"

# 0) health
code="$(curl_req GET "$BASE/api/health")"
if [[ "$code" != "200" ]]; then
  log "ERROR: health failed code=$code"
  log "Body preview: $(preview_body)"
  exit 1
fi
log "health=OK"

# 1) quote
REQ_QUOTE="$RUN_DIR/quote_req.json"
cat >"$REQ_QUOTE" <<'JSON'
{"mode":"platform_models","product_type":"apparel","product_ids":[],"look_set_ids":[],"outputs":{"num_images":4,"num_videos":1},"resolution":"hd","people":["solo_female"],"views":{"half_body":true,"full_body":false},"channels":["instagram"],"marketplaces":[]}
JSON

code="$(curl_req POST "$BASE/api/commerce/quote" "$REQ_QUOTE")"
cp "$OUT" "$RUN_DIR/quote_resp.json" || true
if [[ "$code" != "200" ]]; then
  log "ERROR: quote failed code=$code"
  log "Body preview: $(preview_body)"
  exit 1
fi

QID="$(json_get '.quote_id // empty')"
if [[ -z "$QID" ]]; then
  log "ERROR: quote_id missing"
  log "Body preview: $(preview_body)"
  exit 1
fi
log "quote_id=$QID"

# 2) confirm
REQ_CONFIRM="$RUN_DIR/confirm_req.json"
printf '{"quote_id":"%s","idempotency_key":"%s"}' "$QID" "$IDEMPOTENCY_KEY" >"$REQ_CONFIRM"

code="$(curl_req POST "$BASE/api/commerce/confirm" "$REQ_CONFIRM")"
cp "$OUT" "$RUN_DIR/confirm_resp.json" || true
if [[ "$code" != "200" ]]; then
  log "ERROR: confirm failed code=$code"
  log "Body preview: $(preview_body)"
  exit 1
fi

CID="$(json_get '.campaign_id // empty')"
SID="$(json_get '.studio_job_id // empty')"
if [[ -z "$SID" ]]; then
  log "ERROR: studio_job_id missing"
  log "Body preview: $(preview_body)"
  exit 1
fi
log "campaign_id=$CID"
log "studio_job_id=$SID"

# 3) start worker (silent-ish)
docker compose --env-file "$COMPOSE_ENV" up -d --build svc-commerce-worker >/dev/null 2>&1 || true

# 4) poll status
end=$(( $(date +%s) + WAIT_SECONDS ))
while true; do
  code="$(curl_req GET "$BASE/api/commerce/jobs/$SID/status")"
  cp "$OUT" "$RUN_DIR/job_status_last.json" || true

  # If the route returns 200 but json doesn't have status, still keep it minimal
  st="$(json_get '.status // empty')"
  if [[ -z "$st" ]]; then
    st="$(json_get '.detail // empty')"
  fi
  log "job_status=${st:-unknown}"

  if [[ "${st:-}" == "succeeded" || "${st:-}" == "failed" ]]; then
    log "DONE. Terminal state reached."
    break
  fi

  now_ts="$(date +%s)"
  if (( now_ts >= end )); then
    log "TIMEOUT: still not completed after ${WAIT_SECONDS}s"
    break
  fi
  sleep "$SLEEP_SECONDS"
done

# 5) If still queued, write small diagnostics to files (no big printing)
final_status="$(jq -r '.status // empty' "$RUN_DIR/job_status_last.json" 2>/dev/null || true)"
if [[ "$final_status" == "queued" || -z "$final_status" ]]; then
  docker compose --env-file "$COMPOSE_ENV" ps svc-commerce-worker >"$RUN_DIR/worker_ps.txt" 2>&1 || true
  docker compose --env-file "$COMPOSE_ENV" logs --tail=200 svc-commerce-worker >"$RUN_DIR/worker_logs_tail.txt" 2>&1 || true

  docker compose --env-file "$COMPOSE_ENV" exec -T desifaces-db psql \
    -U "${POSTGRES_USER:-desifaces_admin}" -d "${POSTGRES_DB:-desifaces}" \
    -c "select id,status,attempt_count,next_run_at,updated_at,meta_json->>'request_type' as request_type from public.studio_jobs where id='${SID}'::uuid;" \
    >"$RUN_DIR/db_job_row.txt" 2>&1 || true

  log "Diagnostics saved:"
  log "  $RUN_DIR/worker_ps.txt"
  log "  $RUN_DIR/worker_logs_tail.txt"
  log "  $RUN_DIR/db_job_row.txt"
fi

log "All outputs saved under: $RUN_DIR"