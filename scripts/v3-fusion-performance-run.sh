#!/usr/bin/env bash
set -euo pipefail

# Paid V3 Story Fusion performance certification for the canonical measured benchmark.
# Safety rules:
# - benchmark workflow/stage are pinned internally; caller exports cannot redirect it
# - re-preview must remain exactly 4 minutes / $5.60 USD
# - exactly one billable parent and zero billable children
# - explicit operator payment + external-provider consent is required
# - one Director dispatch only; no client-driven /sync loop
# - background coordinator must fan-in children, stitch, and commit parent pricing
# - final report must prove actual parallel dispatch/provider overlap and pricing invariants
# - Fusion HITL approval is NOT performed here

readonly WORKFLOW_ID="06c5d43e-7bbc-4cb4-aef3-9df36886da3b"
readonly FUSION_STAGE_ID="4038a526-308a-49ba-959a-7e40f512c3b3"
readonly EXPECTED_AMOUNT="5.60"
readonly EXPECTED_CURRENCY="USD"
readonly EXPECTED_MINUTES="4"
readonly EXPECTED_CHILDREN="28"
readonly PAYMENT_PHRASE="PAY 5.60 USD AND ALLOW EXTERNAL PROVIDER"

DF_EMAIL="${DF_EMAIL:-user_apple_iap_test1@desifaces.ai}"
CORE_URL="${CORE_URL:-http://127.0.0.1:18000}"
DIRECTOR_URL="${DIRECTOR_URL:-http://127.0.0.1:18011}"
POSTGRES_DB="${POSTGRES_DB:-desifaces_v3}"
POSTGRES_USER="${POSTGRES_USER:-desifaces_v3_admin}"
RUN_DIR="/tmp/v3-fusion-performance-${WORKFLOW_ID}"

mkdir -p "$RUN_DIR"
rm -f "$RUN_DIR"/*.json "$RUN_DIR"/*.txt 2>/dev/null || true

compose() { bash scripts/v3-compose.sh "$@"; }
psql_scalar() {
  compose exec -T desifaces-db \
    psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}
fail() { echo "ERROR: $*" >&2; exit 1; }

child_jobs_sql() {
  cat <<SQL
select count(*)
from public.studio_jobs j
where j.studio_type='fusion'
  and (
    j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{pricing,parent_job_id}'='${FUSION_STAGE_ID}'
  )
SQL
}

child_jobs_status_sql() {
  local wanted="$1"
  cat <<SQL
select count(*)
from public.studio_jobs j
where j.studio_type='fusion'
  and j.status='${wanted}'
  and (
    j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{pricing,parent_job_id}'='${FUSION_STAGE_ID}'
  )
SQL
}

echo "============================================================"
echo " V3 FUSION PAID PARALLEL PERFORMANCE RUN"
echo " workflow: $WORKFLOW_ID"
echo " Fusion stage: $FUSION_STAGE_ID"
echo " reviewed/live maximum exposure: $EXPECTED_AMOUNT $EXPECTED_CURRENCY"
echo " billable minutes: $EXPECTED_MINUTES"
echo " expected child renders: $EXPECTED_CHILDREN"
echo " client-driven Fusion sync: DISABLED"
echo " Fusion auto-approval: DISABLED"
echo "============================================================"

[[ -f scripts/v3-compose.sh ]] || fail "run from ~/workspace/desifaces-v3"
[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || fail "wrong branch"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || fail "refusing non-V3 DB: $POSTGRES_DB"

# Runtime readiness before any financial mutation.
curl -fsS "$DIRECTOR_URL/api/health" >/dev/null || fail "svc-director unhealthy"
for container in df-v3-svc-fusion-worker df-v3-svc-fusion-extension-stitch-worker; do
  running="$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)"
  restarts="$(docker inspect -f '{{.RestartCount}}' "$container" 2>/dev/null || true)"
  [[ "$running" == "true" ]] || fail "$container is not running"
  echo "${container}_RUNNING=$running"
  echo "${container}_RESTARTS=${restarts:-unknown}"
done

# Cross-workflow guard and untouched paid-execution gate.
resolved_stage="$(psql_scalar "
select stage_run_id::text
from public.v3_studio_stage_runs
where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='fusion';")"
[[ "$resolved_stage" == "$FUSION_STAGE_ID" ]] || fail "benchmark Fusion stage mismatch: $resolved_stage"

active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
current_stage="$(psql_scalar "select current_stage from public.v3_studio_workflows where workflow_id='${WORKFLOW_ID}'::uuid;")"
audio_approved="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='approved';")"
fusion_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid and workflow_id='${WORKFLOW_ID}'::uuid;")"
fusion_attempts="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
parent_state="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
parent_reservation="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'reservation_id','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
existing_children="$(psql_scalar "$(child_jobs_sql)")"

printf 'ACTIVE_GENERATION_JOBS=%s\n' "$active_jobs"
printf 'WORKFLOW_CURRENT_STAGE=%s\n' "$current_stage"
printf 'AUDIO_APPROVED=%s\n' "$audio_approved"
printf 'FUSION_STATE=%s\n' "$fusion_state"
printf 'FUSION_ATTEMPTS=%s\n' "$fusion_attempts"
printf 'FUSION_PARENT_PRICING_STATE=%s\n' "$parent_state"
printf 'FUSION_PARENT_RESERVATION=%s\n' "${parent_reservation:-NONE}"
printf 'EXISTING_FUSION_CHILD_JOBS=%s\n' "$existing_children"

[[ "$active_jobs" == "0" ]] || fail "active generation/pricing work exists"
[[ "$current_stage" == "fusion" ]] || fail "workflow is not at Fusion"
[[ "$audio_approved" == "28" ]] || fail "Audio prerequisite is not 28/28 approved"
[[ "$fusion_state" == "pending" ]] || fail "Fusion stage is not pending"
[[ "$fusion_attempts" == "0" ]] || fail "Fusion attempts already exist; do not retry blindly"
[[ "$parent_state" == "quoted" ]] || fail "Fusion parent pricing is not quoted"
[[ -z "$parent_reservation" ]] || fail "Fusion parent reservation already exists"
[[ "$existing_children" == "0" ]] || fail "Fusion child jobs already exist"
echo "PREPAID_SAFETY_GATE=PASS"

# Fresh auth only.
unset DF_BEARER_TOKEN DF_X_USER_ID || true
export DF_EMAIL CORE_URL
read -rsp "Enter test-account password: " DF_PASSWORD
echo
export DF_PASSWORD
LOGIN_EXPORTS="$(python3 scripts/df_login_exports.py)"
LOGIN_RC=$?
unset DF_PASSWORD
[[ "$LOGIN_RC" -eq 0 ]] || fail "authentication failed"
eval "$LOGIN_EXPORTS"
unset LOGIN_EXPORTS
[[ -n "${DF_BEARER_TOKEN:-}" ]] || fail "fresh bearer token missing"
export DF_BEARER_TOKEN

echo "AUTH_FRESH=PASS"

# Re-preview immediately before payment. This remains non-generating and uses
# external_provider_ok=false at the user contract boundary.
preview_http="$(curl -sS -o "$RUN_DIR/fusion-preview.json" -w '%{http_code}' \
  -X POST \
  -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"external_provider_ok":false}' \
  "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$FUSION_STAGE_ID/pricing-preview")"
[[ "$preview_http" == "200" ]] || { cat "$RUN_DIR/fusion-preview.json" >&2; fail "Fusion pricing preview HTTP $preview_http"; }

python3 - "$RUN_DIR/fusion-preview.json" "$RUN_DIR/fusion-dispatch.json" <<'PY'
from __future__ import annotations
import json, sys
from decimal import Decimal

src,dst=sys.argv[1:]
p=json.load(open(src,encoding='utf-8'))
assert int(p.get('turn_count') or 0)==28, p
assert int(p.get('billable_parent_quote_count') or 0)==1, p
assert int(p.get('billable_child_quote_count') or 0)==0, p
assert int(p.get('child_pricing_suppressed') or 0)==28, p
assert int(p.get('required_child_count') or 0)==28, p
assert int(p.get('preserved_child_count') or 0)==0, p
children=list(p.get('children') or [])
assert len(children)==28, len(children)
confirm=[]
for c in children:
    assert c.get('pricing_suppressed') is True, c
    tid=str(c.get('dialogue_turn_id') or '')
    nonce=str(c.get('request_nonce') or '')
    assert tid and nonce, c
    pricing=c.get('pricing') or {}
    assert str(pricing.get('state') or '').lower()=='suppressed', c
    assert not pricing.get('quote_id') and not pricing.get('reservation_id'), c
    confirm.append({'dialogue_turn_id':tid,'request_nonce':nonce})
parent=p.get('parent_quote') or {}
pricing=parent.get('pricing') or {}
quote=str(pricing.get('quote_id') or '')
fingerprint=str(pricing.get('preview_fingerprint') or '')
amount=Decimal(str(pricing.get('estimated_amount')))
currency=str(pricing.get('currency') or '')
minutes=int(parent.get('billable_minutes') or pricing.get('estimated_units') or 0)
assert quote and fingerprint
assert amount==Decimal('5.60'), amount
assert currency=='USD', currency
assert minutes==4, minutes
assert not pricing.get('reservation_id'), pricing
body={
    'parent_confirmation':{'quote_id':quote,'preview_fingerprint':fingerprint},
    'child_confirmations':confirm,
    'external_provider_ok':True,
    'user_confirmed':True,
}
json.dump(body,open(dst,'w',encoding='utf-8'),indent=2)
print('LIVE_FUSION_PREVIEW=PASS')
print(f'LIVE_FUSION_TOTAL_AUDIO_DURATION_SEC={parent.get("total_audio_duration_sec")}')
print(f'LIVE_FUSION_BILLABLE_MINUTES={minutes}')
print(f'LIVE_FUSION_AMOUNT={amount:.2f}')
print(f'LIVE_FUSION_CURRENCY={currency}')
print('LIVE_FUSION_PARENT_QUOTES=1')
print('LIVE_FUSION_CHILD_BILLABLE_QUOTES=0')
print('LIVE_FUSION_CHILD_PRICING_SUPPRESSED=28')
print(f'LIVE_FUSION_QUOTE_ID={quote}')
PY

# Re-prove preview itself did not start paid work.
fusion_attempts_after_preview="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
active_after_preview="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
reservation_after_preview="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'reservation_id','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
[[ "$fusion_attempts_after_preview" == "0" && "$active_after_preview" == "0" && -z "$reservation_after_preview" ]] || fail "pricing preview unexpectedly started paid work"
echo "LIVE_PREVIEW_NON_BILLABLE_GATE=PASS"

echo
echo "============================================================"
echo " PAYMENT + EXTERNAL PROVIDER CONFIRMATION REQUIRED"
echo " 1 Fusion parent charge"
echo " 4 billable minutes"
echo " Maximum reviewed/live amount: 5.60 USD"
echo " 28 internal child renders will be sent to the external Fusion provider."
echo " Child pricing remains suppressed; billing is parent-only."
echo "============================================================"

if [[ -z "${PAYMENT_CONFIRMATION:-}" ]]; then
  read -r -p "Type exactly '$PAYMENT_PHRASE' to dispatch: " PAYMENT_CONFIRMATION
fi
[[ "$PAYMENT_CONFIRMATION" == "$PAYMENT_PHRASE" ]] || { echo "PAYMENT_AND_PROVIDER_CONSENT_NOT_CONFIRMED=STOP"; exit 0; }
echo "PAYMENT_AND_PROVIDER_CONSENT=ACCEPTED"

# Final zero-attempt check immediately before the single billable dispatch.
[[ "$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")" == "0" ]] || fail "Fusion attempt appeared before dispatch"
[[ "$(psql_scalar "$(child_jobs_sql)")" == "0" ]] || fail "Fusion child job appeared before dispatch"

dispatch_started_ms="$(python3 - <<'PY'
import time
print(int(time.time()*1000))
PY
)"
dispatch_http="$(curl -sS -o "$RUN_DIR/fusion-dispatch-response.json" -w '%{http_code}' \
  -X POST \
  -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary "@$RUN_DIR/fusion-dispatch.json" \
  "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$FUSION_STAGE_ID/dispatch")"
dispatch_finished_ms="$(python3 - <<'PY'
import time
print(int(time.time()*1000))
PY
)"

dispatch_wall_ms=$((dispatch_finished_ms-dispatch_started_ms))
if [[ "$dispatch_http" != "200" ]]; then
  cat "$RUN_DIR/fusion-dispatch-response.json" >&2
  attempts_now="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  children_now="$(psql_scalar "$(child_jobs_sql)")"
  echo "FUSION_DISPATCH_HTTP=$dispatch_http"
  echo "FUSION_ATTEMPTS_NOW=$attempts_now"
  echo "FUSION_CHILD_JOBS_NOW=$children_now"
  echo "HOLD: do not retry blindly after a paid-dispatch failure."
  exit 2
fi

python3 - "$RUN_DIR/fusion-dispatch-response.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1],encoding='utf-8'))
children=list(p.get('children') or [])
assert len(children)==28, len(children)
assert str(p.get('stage_state') or '')=='generating', p
pricing=p.get('parent_pricing') or {}
state=str(pricing.get('state') or '').lower()
assert state in {'reserved','processing','generating'}, pricing
assert pricing.get('reservation_id'), pricing
print('FUSION_DISPATCH=PASS')
print(f'FUSION_ATTEMPT_ID={p.get("attempt_id")}')
print(f'FUSION_ATTEMPT_COUNT={p.get("attempt_count")}')
print(f'FUSION_DISPATCH_CHILDREN={len(children)}')
print(f'FUSION_PARENT_PRICING_AFTER_DISPATCH={state}')
print(f'FUSION_PARENT_RESERVATION_ID={pricing.get("reservation_id")}')
PY

echo "DIRECTOR_DISPATCH_WALL_MS=$dispatch_wall_ms"

attempts_now="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
state_now="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
parent_state_now="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
parent_reservation_now="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'reservation_id','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
[[ "$attempts_now" == "1" ]] || fail "expected one Fusion attempt after dispatch; found $attempts_now"
[[ "$state_now" == "generating" ]] || fail "Fusion stage did not enter generating: $state_now"
[[ -n "$parent_reservation_now" ]] || fail "parent reservation was not persisted"
echo "PAID_DISPATCH_DURABLE_GATE=PASS"
echo "FUSION_PARENT_PRICING_STATE=$parent_state_now"

# IMPORTANT: no Director /sync calls from here onward. The client only observes DB.
echo "BACKGROUND_COMPLETION_OBSERVER=STARTED"
max_loops=360
terminal_reached=0
for i in $(seq 1 "$max_loops"); do
  stage_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  child_total="$(psql_scalar "$(child_jobs_sql)")"
  child_succeeded="$(psql_scalar "$(child_jobs_status_sql succeeded)")"
  child_failed="$(psql_scalar "$(child_jobs_status_sql failed)")"
  parent_state_loop="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  bg_phase="$(psql_scalar "
select coalesce(a.metadata_json #>> '{background_coordinator,phase}','')
from public.v3_studio_stage_attempts a
where a.stage_run_id='${FUSION_STAGE_ID}'::uuid
order by a.attempt_no desc limit 1;")"
  elapsed=$((i*10))
  echo "FUSION_PROGRESS stage=$stage_state children_succeeded=$child_succeeded/$EXPECTED_CHILDREN children_failed=$child_failed child_total=$child_total parent_pricing=$parent_state_loop background_phase=${bg_phase:-unknown} elapsed_s=$elapsed"

  if [[ "$child_failed" != "0" || "$stage_state" == "failed" ]]; then
    echo "FUSION_BACKGROUND_RESULT=FAILED"
    STAGE_ID="$FUSION_STAGE_ID" bash scripts/v3-fusion-performance-report.sh | tee "$RUN_DIR/fusion-performance-report.txt" || true
    echo "HOLD: inspect failed children / released parent before any retry."
    exit 3
  fi

  if [[ "$stage_state" == "awaiting_review" || "$stage_state" == "approved" ]]; then
    terminal_reached=1
    break
  fi
  sleep 10
done
[[ "$terminal_reached" == "1" ]] || fail "background coordinator did not reach awaiting_review/approved within bounded observation"

echo "BACKGROUND_COORDINATOR_COMPLETION=PASS"

# Final durable correctness/economics/performance gates.
final_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
final_attempts="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
final_attempt_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
child_total="$(psql_scalar "$(child_jobs_sql)")"
child_succeeded="$(psql_scalar "$(child_jobs_status_sql succeeded)")"
child_failed="$(psql_scalar "$(child_jobs_status_sql failed)")"
suppressed_children="$(psql_scalar "
select count(*) from public.studio_jobs j
where j.studio_type='fusion'
  and (
    j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{pricing,parent_job_id}'='${FUSION_STAGE_ID}'
  )
  and (
    j.payload_json #>> '{pricing,state}'='suppressed'
    or j.payload_json #>> '{provider_options,pricing,state}'='suppressed'
    or j.payload_json #>> '{tags,pricing,state}'='suppressed'
  );")"
active_final="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
final_parent_state="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
final_parent_reservation="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'reservation_id','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
final_parent_ledger="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'ledger_entry_id','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
review_pending="$(psql_scalar "select count(*) from public.v3_studio_review_items where stage_run_id='${FUSION_STAGE_ID}'::uuid and decision='pending';")"
active_outputs="$(psql_scalar "select count(*) from public.v3_studio_stage_outputs where stage_run_id='${FUSION_STAGE_ID}'::uuid and is_active=true;")"

execution_mode="$(psql_scalar "select coalesce(metadata_json #>> '{dispatch_performance,execution_mode}','') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
requested_children="$(psql_scalar "select coalesce(metadata_json #>> '{dispatch_performance,requested_children}','0') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
accepted_children="$(psql_scalar "select coalesce(metadata_json #>> '{dispatch_performance,accepted_children}','0') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
max_dispatch_overlap="$(psql_scalar "select coalesce(metadata_json #>> '{dispatch_performance,max_parallel_dispatch_observed}','0') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
dispatch_spread_ms="$(psql_scalar "select coalesce(metadata_json #>> '{dispatch_performance,dispatch_spread_ms}','') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
stitch_ms="$(psql_scalar "select coalesce(metadata_json #>> '{background_coordinator,stitch_ms}','') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
finalized_at="$(psql_scalar "select coalesce(metadata_json #>> '{background_coordinator,finalized_at}','') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"

provider_timing_counts="$(psql_scalar "
with jobs as (
  select j.id from public.studio_jobs j
  where j.studio_type='fusion' and (
    j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{pricing,parent_job_id}'='${FUSION_STAGE_ID}'
  )
), runs as (
  select j.id,
    nullif(j.meta_json->>'worker_claimed_at','')::timestamptz worker_claimed_at,
    nullif(r.meta_json->>'provider_submitted_at','')::timestamptz submitted_at,
    nullif(r.meta_json->>'provider_first_processing_at','')::timestamptz processing_at,
    nullif(r.meta_json->>'provider_terminal_at','')::timestamptz terminal_at
  from public.studio_jobs j join jobs x on x.id=j.id
  left join lateral (select * from public.provider_runs p where p.job_id=j.id order by p.created_at desc limit 1) r on true
)
select count(*) || '|' ||
       count(*) filter(where worker_claimed_at is not null) || '|' ||
       count(*) filter(where submitted_at is not null) || '|' ||
       count(*) filter(where terminal_at is not null)
from runs;")"
IFS='|' read -r timing_total timing_claim timing_submit timing_terminal <<<"$provider_timing_counts"

max_provider_overlap="$(psql_scalar "
with jobs as (
  select j.id from public.studio_jobs j
  where j.studio_type='fusion' and (
    j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{pricing,parent_job_id}'='${FUSION_STAGE_ID}'
  )
), runs as (
  select j.id,
    coalesce(nullif(r.meta_json->>'provider_first_processing_at','')::timestamptz,
             nullif(r.meta_json->>'provider_submitted_at','')::timestamptz) as start_at,
    nullif(r.meta_json->>'provider_terminal_at','')::timestamptz as terminal_at
  from public.studio_jobs j join jobs x on x.id=j.id
  join lateral (select * from public.provider_runs p where p.job_id=j.id order by p.created_at desc limit 1) r on true
), overlap as (
  select r1.id,
    (select count(*) from runs r2
      where r2.start_at is not null and r2.terminal_at is not null
        and r2.start_at <= r1.start_at and r2.terminal_at > r1.start_at) n
  from runs r1 where r1.start_at is not null and r1.terminal_at is not null
)
select coalesce(max(n),0) from overlap;")"

parent_consume_events="$(psql_scalar "
select count(*)
from public.pricing_credit_ledger_events l
join public.v3_studio_workflows w on w.owner_user_id=l.user_id
where w.workflow_id='${WORKFLOW_ID}'::uuid
  and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
parent_credit_delta="$(psql_scalar "
select coalesce(sum(l.credits_delta),0)
from public.pricing_credit_ledger_events l
join public.v3_studio_workflows w on w.owner_user_id=l.user_id
where w.workflow_id='${WORKFLOW_ID}'::uuid
  and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"

printf 'FINAL_FUSION_STATE=%s\n' "$final_state"
printf 'FINAL_FUSION_ATTEMPTS=%s\n' "$final_attempts"
printf 'FINAL_FUSION_ATTEMPT_STATE=%s\n' "$final_attempt_state"
printf 'FUSION_CHILD_JOBS=%s\n' "$child_total"
printf 'FUSION_CHILD_SUCCEEDED=%s\n' "$child_succeeded"
printf 'FUSION_CHILD_FAILED=%s\n' "$child_failed"
printf 'FUSION_CHILD_PRICING_SUPPRESSED=%s\n' "$suppressed_children"
printf 'FUSION_PARENT_PRICING_STATE=%s\n' "$final_parent_state"
printf 'FUSION_PARENT_RESERVATION_ID=%s\n' "$final_parent_reservation"
printf 'FUSION_PARENT_LEDGER_ENTRY_ID=%s\n' "$final_parent_ledger"
printf 'FUSION_PENDING_REVIEW_ITEMS=%s\n' "$review_pending"
printf 'FUSION_ACTIVE_OUTPUTS=%s\n' "$active_outputs"
printf 'ACTIVE_GENERATION_JOBS=%s\n' "$active_final"
printf 'DISPATCH_EXECUTION_MODE=%s\n' "$execution_mode"
printf 'DISPATCH_REQUESTED_CHILDREN=%s\n' "$requested_children"
printf 'DISPATCH_ACCEPTED_CHILDREN=%s\n' "$accepted_children"
printf 'MAX_PARALLEL_DISPATCH_OBSERVED=%s\n' "$max_dispatch_overlap"
printf 'DISPATCH_SPREAD_MS=%s\n' "$dispatch_spread_ms"
printf 'STITCH_MS=%s\n' "$stitch_ms"
printf 'BACKGROUND_FINALIZED_AT=%s\n' "$finalized_at"
printf 'PROVIDER_TIMING_TOTAL=%s\n' "$timing_total"
printf 'PROVIDER_TIMING_WORKER_CLAIM=%s\n' "$timing_claim"
printf 'PROVIDER_TIMING_SUBMITTED=%s\n' "$timing_submit"
printf 'PROVIDER_TIMING_TERMINAL=%s\n' "$timing_terminal"
printf 'MAX_PROVIDER_OVERLAP=%s\n' "$max_provider_overlap"
printf 'PARENT_CONSUME_EVENTS=%s\n' "$parent_consume_events"
printf 'PARENT_CREDIT_DELTA=%s\n' "$parent_credit_delta"

[[ "$final_state" == "awaiting_review" || "$final_state" == "approved" ]] || fail "Fusion did not reach reviewable terminal state"
[[ "$final_attempts" == "1" && "$final_attempt_state" == "succeeded" ]] || fail "Fusion attempt is not exactly one succeeded attempt"
[[ "$child_total" == "28" && "$child_succeeded" == "28" && "$child_failed" == "0" ]] || fail "Fusion child cohort is not 28/28 succeeded"
[[ "$suppressed_children" == "28" ]] || fail "not all Fusion children are pricing suppressed"
[[ "$final_parent_state" == "committed" ]] || fail "Fusion parent pricing is not committed"
[[ -n "$final_parent_reservation" && -n "$final_parent_ledger" ]] || fail "Fusion parent reservation/ledger lineage missing"
[[ "$review_pending" == "1" && "$active_outputs" == "1" ]] || fail "Fusion canonical output/review gate failed"
[[ "$active_final" == "0" ]] || fail "active jobs remain after Fusion completion"
[[ "$execution_mode" == "parallel" ]] || fail "dispatch execution mode is not parallel"
[[ "$requested_children" == "28" && "$accepted_children" == "28" ]] || fail "dispatch did not request/accept all 28 children"
python3 - "$max_dispatch_overlap" "$max_provider_overlap" <<'PY'
import sys
md=float(sys.argv[1] or 0)
mp=float(sys.argv[2] or 0)
assert md>1, f'max_parallel_dispatch_observed={md}'
assert mp>1, f'max_provider_overlap={mp}'
PY
[[ "$timing_total" == "28" && "$timing_claim" == "28" && "$timing_submit" == "28" && "$timing_terminal" == "28" ]] || fail "Fusion lifecycle timing coverage is not 28/28"
[[ -n "$stitch_ms" && -n "$finalized_at" ]] || fail "background stitch/finalization telemetry missing"
[[ "$parent_consume_events" == "1" ]] || fail "expected exactly one Fusion parent consume event"
[[ "$parent_credit_delta" == "-560" ]] || fail "expected exactly -560 Fusion parent credits; got $parent_credit_delta"
echo "FUSION_RUNTIME_ECONOMIC_GATE=PASS"
echo "ACTUAL_PARALLEL_FUSION_EXECUTION=PASS"
echo "BACKGROUND_WITHOUT_CLIENT_SYNC=PASS"

# Canonical detailed performance evidence.
STAGE_ID="$FUSION_STAGE_ID" bash scripts/v3-fusion-performance-report.sh | tee "$RUN_DIR/fusion-performance-report.txt"

echo
echo "============================================================"
echo " V3 FUSION PAID PERFORMANCE RUN = PASS"
echo " Fusion parent charge                  = 1"
echo " Fusion child billable charges         = 0"
echo " Fusion child jobs                     = 28/28 succeeded"
echo " parent committed credits              = 560"
echo " actual parallel dispatch              = PASS"
echo " actual provider overlap               = PASS"
echo " background coordinator completion     = PASS"
echo " scene stitch                          = PASS"
echo " client-driven /sync                   = NOT USED"
echo " active generation jobs                = 0"
echo " Fusion output awaiting HITL review    = 1"
echo " Fusion HITL approval                  = NOT PERFORMED"
echo "============================================================"
echo "PERFORMANCE_REPORT=$RUN_DIR/fusion-performance-report.txt"
