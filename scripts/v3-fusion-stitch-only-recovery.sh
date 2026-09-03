#!/usr/bin/env bash
set -euo pipefail

# Recover the canonical V3 Fusion benchmark after a post-render scene-stitch failure.
# This script is intentionally pinned to one known benchmark stage and will refuse
# to run unless all 28 provider child renders already succeeded and are reusable.
#
# Financial/provider invariants:
# - attempt 1 failed only after 28/28 child success
# - prior parent reservation was released and consumed 0 credits
# - retry preview must preserve all 28 children and require 0 new children
# - retry dispatch may reserve a fresh parent quote, but MUST create 0 new Fusion jobs
# - no VEED rerender is permitted
# - parent pricing commits only after a canonical stitched scene is created
# - Fusion HITL approval is deliberately not performed here

readonly WORKFLOW_ID="06c5d43e-7bbc-4cb4-aef3-9df36886da3b"
readonly FUSION_STAGE_ID="4038a526-308a-49ba-959a-7e40f512c3b3"
readonly FAILED_ATTEMPT_ID="6b62905f-b555-43f2-9f4e-da61f1439cb2"
readonly EXPECTED_CHILDREN="28"
readonly EXPECTED_AMOUNT="5.60"
readonly EXPECTED_CURRENCY="USD"
readonly EXPECTED_MINUTES="4"
readonly EXPECTED_CREDITS="560"
readonly CONFIRM_PHRASE="PAY 5.60 USD TO FINALIZE EXISTING 28 FUSION VIDEOS"

DF_EMAIL="${DF_EMAIL:-user_apple_iap_test1@desifaces.ai}"
CORE_URL="${CORE_URL:-http://127.0.0.1:18000}"
DIRECTOR_URL="${DIRECTOR_URL:-http://127.0.0.1:18011}"
FUSION_EXTENSION_URL="${FUSION_EXTENSION_URL:-http://127.0.0.1:18006}"
POSTGRES_DB="${POSTGRES_DB:-desifaces_v3}"
POSTGRES_USER="${POSTGRES_USER:-desifaces_v3_admin}"
RUN_DIR="/tmp/v3-fusion-stitch-recovery-${WORKFLOW_ID}"

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
echo " V3 FUSION STITCH-ONLY RECOVERY"
echo " workflow: $WORKFLOW_ID"
echo " Fusion stage: $FUSION_STAGE_ID"
echo " failed attempt: $FAILED_ATTEMPT_ID"
echo " provider rerender: FORBIDDEN"
echo " expected preserved children: $EXPECTED_CHILDREN"
echo " expected new provider jobs: 0"
echo " HITL auto-approval: DISABLED"
echo "============================================================"

[[ -f scripts/v3-compose.sh ]] || fail "run from ~/workspace/desifaces-v3"
[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || fail "wrong branch"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || fail "refusing non-V3 DB: $POSTGRES_DB"

# ---------------------------------------------------------------------------
# 1. Durable failed-state preflight. Nothing below mutates pricing or provider jobs.
# ---------------------------------------------------------------------------
resolved_stage="$(psql_scalar "
select stage_run_id::text
from public.v3_studio_stage_runs
where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='fusion';")"
[[ "$resolved_stage" == "$FUSION_STAGE_ID" ]] || fail "benchmark Fusion stage mismatch: $resolved_stage"

active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
current_stage="$(psql_scalar "select current_stage from public.v3_studio_workflows where workflow_id='${WORKFLOW_ID}'::uuid;")"
audio_approved="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='approved';")"
fusion_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
attempt_count="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
latest_attempt_id="$(psql_scalar "select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
latest_attempt_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
latest_error_code="$(psql_scalar "select coalesce(error_code,'') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
latest_error_message="$(psql_scalar "select coalesce(error_message,'') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
parent_state="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
child_total="$(psql_scalar "$(child_jobs_sql)")"
child_succeeded="$(psql_scalar "$(child_jobs_status_sql succeeded)")"
child_failed="$(psql_scalar "$(child_jobs_status_sql failed)")"
reusable_children="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c
where a.attempt_id='${FAILED_ATTEMPT_ID}'::uuid
  and lower(coalesce(c->>'status','')) in ('succeeded','completed','complete','ready')
  and coalesce(c->>'video_url','') <> '';")"
unique_reusable_turns="$(psql_scalar "
select count(distinct c->>'dialogue_turn_id')
from public.v3_studio_stage_attempts a
cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c
where a.attempt_id='${FAILED_ATTEMPT_ID}'::uuid
  and lower(coalesce(c->>'status','')) in ('succeeded','completed','complete','ready')
  and coalesce(c->>'video_url','') <> '';")"

parent_consume_events_before="$(psql_scalar "
select count(*)
from public.pricing_credit_ledger_events l
join public.v3_studio_workflows w on w.owner_user_id=l.user_id
where w.workflow_id='${WORKFLOW_ID}'::uuid
  and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
parent_credit_delta_before="$(psql_scalar "
select coalesce(sum(l.credits_delta),0)
from public.pricing_credit_ledger_events l
join public.v3_studio_workflows w on w.owner_user_id=l.user_id
where w.workflow_id='${WORKFLOW_ID}'::uuid
  and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"

printf 'ACTIVE_GENERATION_JOBS=%s\n' "$active_jobs"
printf 'WORKFLOW_CURRENT_STAGE=%s\n' "$current_stage"
printf 'AUDIO_APPROVED=%s\n' "$audio_approved"
printf 'FUSION_STATE=%s\n' "$fusion_state"
printf 'FUSION_ATTEMPTS=%s\n' "$attempt_count"
printf 'FAILED_ATTEMPT_ID=%s\n' "$latest_attempt_id"
printf 'FAILED_ATTEMPT_STATE=%s\n' "$latest_attempt_state"
printf 'FAILED_ERROR_CODE=%s\n' "$latest_error_code"
printf 'FAILED_ERROR_MESSAGE=%s\n' "$latest_error_message"
printf 'FUSION_PARENT_PRICING_STATE=%s\n' "$parent_state"
printf 'EXISTING_FUSION_CHILD_JOBS=%s\n' "$child_total"
printf 'EXISTING_FUSION_CHILD_SUCCEEDED=%s\n' "$child_succeeded"
printf 'EXISTING_FUSION_CHILD_FAILED=%s\n' "$child_failed"
printf 'REUSABLE_CHILDREN=%s\n' "$reusable_children"
printf 'UNIQUE_REUSABLE_TURNS=%s\n' "$unique_reusable_turns"
printf 'PARENT_CONSUME_EVENTS_BEFORE=%s\n' "$parent_consume_events_before"
printf 'PARENT_CREDIT_DELTA_BEFORE=%s\n' "$parent_credit_delta_before"

[[ "$active_jobs" == "0" ]] || fail "active generation/pricing work exists"
[[ "$current_stage" == "fusion" ]] || fail "workflow is not at Fusion"
[[ "$audio_approved" == "28" ]] || fail "Audio prerequisite is not 28/28 approved"
[[ "$fusion_state" == "failed" ]] || fail "Fusion stage is not failed"
[[ "$attempt_count" == "1" ]] || fail "expected exactly one failed Fusion attempt before recovery; found $attempt_count"
[[ "$latest_attempt_id" == "$FAILED_ATTEMPT_ID" ]] || fail "unexpected latest attempt: $latest_attempt_id"
[[ "$latest_attempt_state" == "failed" ]] || fail "attempt 1 is not failed"
[[ "$latest_error_message" == *"fusion_scene_stitch_failed:502"* ]] || fail "failure was not the certified scene-stitch 502"
[[ "$latest_error_message" == *"The read operation timed out"* ]] || fail "failure was not the certified segment read timeout"
[[ "$parent_state" == "released" ]] || fail "failed parent pricing was not released: $parent_state"
[[ "$child_total" == "$EXPECTED_CHILDREN" ]] || fail "expected 28 existing child jobs; found $child_total"
[[ "$child_succeeded" == "$EXPECTED_CHILDREN" ]] || fail "not all existing child jobs succeeded: $child_succeeded"
[[ "$child_failed" == "0" ]] || fail "existing child failure exists: $child_failed"
[[ "$reusable_children" == "$EXPECTED_CHILDREN" ]] || fail "attempt metadata does not preserve all 28 video URLs"
[[ "$unique_reusable_turns" == "$EXPECTED_CHILDREN" ]] || fail "reusable child turn lineage is incomplete"
[[ "$parent_consume_events_before" == "0" ]] || fail "parent was already consumed; recovery financial assumptions invalid"
python3 - "$parent_credit_delta_before" <<'PY'
from decimal import Decimal
import sys
assert Decimal(sys.argv[1]) == Decimal("0"), sys.argv[1]
PY

echo "FAILED_RUN_RECOVERY_GATE=PASS"
echo "FROZEN_PROVIDER_PERFORMANCE=PASS_28_OF_28"
echo "FROZEN_PROVIDER_RERENDER_ALLOWED=NO"

# ---------------------------------------------------------------------------
# 2. Static/runtime fix validation, then rebuild only the Fusion Extension API
#    and its V3 coordinator worker. No Face/Audio/Fusion provider worker restart.
# ---------------------------------------------------------------------------
python3 -m py_compile \
  services/svc-fusion-extension/app/app/services/v3_stitch_resilience.py \
  services/svc-fusion-extension/app/app/api/routes/v3_scene_stitch.py \
  services/svc-director/app/app/fusion_execution_runtime.py

echo "STATIC_VALIDATION=PASS"

echo "BUILDING_STITCH_FIX=STARTED"
compose build svc-fusion-extension svc-fusion-extension-stitch-worker

echo "STITCH_FIX_IMAGE_BUILD=PASS"

# Run the transient-read regression directly inside the just-built service image.
# This test uses no network/provider and proves a timed-out segment is retried while
# a successful sibling segment is downloaded only once.
compose run --rm --no-deps \
  -e V3_SCENE_STITCH_DOWNLOAD_ATTEMPTS=3 \
  -e V3_SCENE_STITCH_DOWNLOAD_CONCURRENCY=1 \
  -e V3_SCENE_STITCH_DOWNLOAD_RETRY_BACKOFF_SECONDS=0 \
  -e V3_SCENE_STITCH_DOWNLOAD_TIMEOUT_SECONDS=300 \
  svc-fusion-extension python - <<'PY'
from pathlib import Path
import tempfile
from app.services import v3_stitch_resilience as target

calls = {}
def fake_download(url, output_path, *, timeout_seconds=120):
    calls[url] = calls.get(url, 0) + 1
    assert timeout_seconds == 300
    if url.endswith('first.mp4') and calls[url] == 1:
        raise TimeoutError('The read operation timed out')
    Path(output_path).write_bytes(url.encode())
    return output_path

def fake_stitch(inputs, out_mp4):
    assert len(inputs) == 2
    assert all(Path(p).stat().st_size > 0 for p in inputs)
    Path(out_mp4).write_bytes(b'stitched')

target.download_to_local = fake_download
target.stitch_videos = fake_stitch
with tempfile.TemporaryDirectory() as td:
    out = Path(td) / 'scene.mp4'
    target.resilient_stitch_video_urls([
        'https://provider.example/first.mp4',
        'https://provider.example/second.mp4',
    ], str(out))
    assert out.read_bytes() == b'stitched'
assert calls['https://provider.example/first.mp4'] == 2, calls
assert calls['https://provider.example/second.mp4'] == 1, calls
print('TRANSIENT_SEGMENT_RETRY_TEST=PASS')
PY

echo "RECREATING_STITCH_RUNTIME=STARTED"
compose --profile v3-execution up -d --no-deps --force-recreate \
  svc-fusion-extension svc-fusion-extension-stitch-worker

# API readiness and coordinator-process readiness.
for _ in $(seq 1 60); do
  if curl -fsS "$FUSION_EXTENSION_URL/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "$FUSION_EXTENSION_URL/api/health" >/dev/null || fail "svc-fusion-extension unhealthy after stitch fix"

stitch_worker_running="$(docker inspect -f '{{.State.Running}}' df-v3-svc-fusion-extension-stitch-worker 2>/dev/null || true)"
stitch_worker_restarts="$(docker inspect -f '{{.RestartCount}}' df-v3-svc-fusion-extension-stitch-worker 2>/dev/null || true)"
[[ "$stitch_worker_running" == "true" ]] || fail "V3 Fusion Extension stitch/coordinator worker not running"
printf 'svc-fusion-extension=HEALTHY\n'
printf 'STITCH_COORDINATOR_WORKER_RUNNING=%s\n' "$stitch_worker_running"
printf 'STITCH_COORDINATOR_WORKER_RESTARTS=%s\n' "${stitch_worker_restarts:-unknown}"
echo "STITCH_RUNTIME_DEPLOYMENT_GATE=PASS"

# Verify runtime image actually contains the resilient implementation.
compose exec -T svc-fusion-extension python - <<'PY'
from app.services.v3_stitch_resilience import (
    _download_attempts,
    _download_concurrency,
    _download_timeout_seconds,
)
assert _download_timeout_seconds() == 300
assert _download_attempts() == 3
assert _download_concurrency(28) == 8
print('STITCH_RUNTIME_CONFIG=timeout_300_attempts_3_concurrency_8')
PY

# Reconfirm deployment itself did not mutate the failed benchmark.
[[ "$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")" == "failed" ]] || fail "deployment unexpectedly changed Fusion stage"
[[ "$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")" == "1" ]] || fail "deployment unexpectedly added Fusion attempt"
[[ "$(psql_scalar "$(child_jobs_sql)")" == "$EXPECTED_CHILDREN" ]] || fail "deployment unexpectedly changed child job count"
echo "POST_DEPLOY_NO_MUTATION_GATE=PASS"

# ---------------------------------------------------------------------------
# 3. Fresh authentication and non-generating retry preview.
# ---------------------------------------------------------------------------
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

preview_http="$(curl -sS -o "$RUN_DIR/retry-preview.json" -w '%{http_code}' \
  -X POST \
  -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"external_provider_ok":false}' \
  "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$FUSION_STAGE_ID/pricing-preview")"
[[ "$preview_http" == "200" ]] || { cat "$RUN_DIR/retry-preview.json" >&2; fail "stitch-only retry preview HTTP $preview_http"; }

python3 - "$RUN_DIR/retry-preview.json" "$RUN_DIR/retry-dispatch.json" <<'PY'
from __future__ import annotations
import json, sys
from decimal import Decimal

src,dst=sys.argv[1:]
p=json.load(open(src,encoding='utf-8'))
assert int(p.get('turn_count') or 0)==28, p
assert int(p.get('preserved_child_count') or 0)==28, p
assert int(p.get('required_child_count') or 0)==0, p
assert int(p.get('billable_parent_quote_count') or 0)==1, p
assert int(p.get('billable_child_quote_count') or 0)==0, p
children=list(p.get('children') or [])
assert len(children)==0, children

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
    'parent_confirmation':{
        'quote_id':quote,
        'preview_fingerprint':fingerprint,
    },
    'child_confirmations':[],
    # Director's existing dispatch contract requires this flag even when the
    # recovery has zero required provider children. The zero-new-job gates below
    # prove this retry does not submit anything to the external provider.
    'external_provider_ok':True,
    'user_confirmed':True,
}
json.dump(body,open(dst,'w',encoding='utf-8'),indent=2)
print('STITCH_ONLY_RETRY_PREVIEW=PASS')
print('PRESERVED_CHILDREN=28')
print('REQUIRED_NEW_CHILDREN=0')
print('NEW_CHILD_PRICING_QUOTES=0')
print('PARENT_QUOTES=1')
print(f'RETRY_BILLABLE_MINUTES={minutes}')
print(f'RETRY_PARENT_AMOUNT={amount:.2f}')
print(f'RETRY_PARENT_CURRENCY={currency}')
print(f'RETRY_PARENT_QUOTE_ID={quote}')
PY

# Preview must not create a second attempt, reservation, or provider child.
[[ "$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")" == "1" ]] || fail "preview created a Fusion attempt"
[[ "$(psql_scalar "$(child_jobs_sql)")" == "$EXPECTED_CHILDREN" ]] || fail "preview created a provider child job"
parent_state_after_preview="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
[[ "$parent_state_after_preview" == "quoted" ]] || fail "retry parent preview did not persist quoted state: $parent_state_after_preview"
echo "RETRY_PREVIEW_NON_BILLABLE_GATE=PASS"

# ---------------------------------------------------------------------------
# 4. Financial confirmation. This does NOT authorize another provider render.
#    It authorizes the fresh parent reservation that will commit only if the
#    existing 28 videos are successfully assembled into the scene output.
# ---------------------------------------------------------------------------
echo
echo "============================================================"
echo " STITCH-ONLY FINALIZATION CONFIRMATION REQUIRED"
echo " Existing provider videos reused: 28"
echo " New provider renders: 0"
echo " Fresh parent reservation: $EXPECTED_AMOUNT $EXPECTED_CURRENCY"
echo " Parent charge commits only if final scene succeeds."
echo "============================================================"

if [[ -z "${PAYMENT_CONFIRMATION:-}" ]]; then
  read -r -p "Type exactly '$CONFIRM_PHRASE' to continue: " PAYMENT_CONFIRMATION
fi
[[ "$PAYMENT_CONFIRMATION" == "$CONFIRM_PHRASE" ]] || { echo "STITCH_ONLY_PAYMENT_NOT_CONFIRMED=STOP"; exit 0; }
echo "STITCH_ONLY_PAYMENT_CONFIRMATION=ACCEPTED"

# Absolute no-rerender barrier immediately before reserve/dispatch.
baseline_child_count="$(psql_scalar "$(child_jobs_sql)")"
baseline_child_max_created="$(psql_scalar "
select coalesce(max(created_at)::text,'')
from public.studio_jobs j
where j.studio_type='fusion'
  and (
    j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{pricing,parent_job_id}'='${FUSION_STAGE_ID}'
  );")"
[[ "$baseline_child_count" == "$EXPECTED_CHILDREN" ]] || fail "child count changed before retry"

# One Director dispatch creates attempt 2 + fresh parent reservation. With 28
# preserved children and 0 required turns it MUST take the stitch_only_retry branch.
dispatch_http="$(curl -sS -o "$RUN_DIR/retry-dispatch-response.json" -w '%{http_code}' \
  -X POST \
  -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary "@$RUN_DIR/retry-dispatch.json" \
  "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$FUSION_STAGE_ID/dispatch")"
if [[ "$dispatch_http" != "200" ]]; then
  cat "$RUN_DIR/retry-dispatch-response.json" >&2
  echo "HOLD: stitch-only retry dispatch failed; do not dispatch again blindly."
  exit 2
fi

python3 - "$RUN_DIR/retry-dispatch-response.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1],encoding='utf-8'))
children=list(p.get('children') or [])
assert len(children)==28, len(children)
assert all(bool(c.get('reused_from_prior_attempt')) for c in children), children
assert str(p.get('stage_state') or '')=='generating', p
pricing=p.get('parent_pricing') or {}
assert str(pricing.get('state') or '').lower()=='reserved', pricing
assert pricing.get('reservation_id'), pricing
print('STITCH_ONLY_DISPATCH=PASS')
print(f'RETRY_ATTEMPT_ID={p.get("attempt_id")}')
print(f'RETRY_ATTEMPT_COUNT={p.get("attempt_count")}')
print('RESPONSE_REUSED_CHILDREN=28')
print(f'RETRY_PARENT_RESERVATION_ID={pricing.get("reservation_id")}')
PY

attempts_after_dispatch="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
retry_kind="$(psql_scalar "select attempt_kind from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
retry_outcome="$(psql_scalar "select coalesce(metadata_json->>'dispatch_outcome','') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
retry_preserved="$(psql_scalar "select coalesce(metadata_json->>'preserved_child_count','0') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
children_after_dispatch="$(psql_scalar "$(child_jobs_sql)")"
child_max_created_after="$(psql_scalar "
select coalesce(max(created_at)::text,'')
from public.studio_jobs j
where j.studio_type='fusion'
  and (
    j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{pricing,parent_job_id}'='${FUSION_STAGE_ID}'
  );")"

printf 'FUSION_ATTEMPTS_AFTER_RETRY=%s\n' "$attempts_after_dispatch"
printf 'RETRY_ATTEMPT_KIND=%s\n' "$retry_kind"
printf 'RETRY_DISPATCH_OUTCOME=%s\n' "$retry_outcome"
printf 'RETRY_PRESERVED_CHILDREN=%s\n' "$retry_preserved"
printf 'FUSION_CHILD_JOBS_AFTER_RETRY_DISPATCH=%s\n' "$children_after_dispatch"

[[ "$attempts_after_dispatch" == "2" ]] || fail "stitch retry did not create exactly attempt 2"
[[ "$retry_kind" == "retry" ]] || fail "attempt 2 is not a technical retry"
[[ "$retry_outcome" == "stitch_only_retry" ]] || fail "retry did not take stitch-only branch: $retry_outcome"
[[ "$retry_preserved" == "$EXPECTED_CHILDREN" ]] || fail "retry did not preserve all 28 children"
[[ "$children_after_dispatch" == "$baseline_child_count" ]] || fail "FORBIDDEN: stitch retry created new Fusion child jobs"
[[ "$child_max_created_after" == "$baseline_child_max_created" ]] || fail "FORBIDDEN: child job creation timestamp changed"
echo "ZERO_NEW_PROVIDER_JOBS_GATE=PASS"
echo "EXTERNAL_PROVIDER_RERENDER=NOT_CALLED"

# ---------------------------------------------------------------------------
# 5. Observe durable background finalization only. No Director /sync call.
# ---------------------------------------------------------------------------
echo "BACKGROUND_STITCH_OBSERVER=STARTED"
terminal=0
for i in $(seq 1 180); do
  stage_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  attempt_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
  phase="$(psql_scalar "select coalesce(metadata_json #>> '{background_coordinator,phase}','') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
  stitch_ms="$(psql_scalar "select coalesce(metadata_json #>> '{background_coordinator,stitch_ms}','') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
  parent_loop="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  current_children="$(psql_scalar "$(child_jobs_sql)")"
  elapsed=$((i*10))
  echo "STITCH_PROGRESS stage=$stage_state attempt=$attempt_state phase=${phase:-unknown} preserved=28 new_provider_jobs=$((current_children-baseline_child_count)) parent=$parent_loop stitch_ms=${stitch_ms:-pending} elapsed_s=$elapsed"

  if [[ "$current_children" != "$baseline_child_count" ]]; then
    fail "FORBIDDEN: provider child count increased during stitch-only recovery"
  fi
  if [[ "$stage_state" == "failed" || "$attempt_state" == "failed" ]]; then
    error_now="$(psql_scalar "select coalesce(error_message,'') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
    echo "STITCH_ONLY_RECOVERY_RESULT=FAILED"
    echo "RECOVERY_ERROR=$error_now"
    echo "HOLD: existing 28 children remain the recovery source; do not rerender."
    exit 3
  fi
  if [[ "$stage_state" == "awaiting_review" && "$attempt_state" == "succeeded" ]]; then
    terminal=1
    break
  fi
  sleep 10
done
[[ "$terminal" == "1" ]] || fail "stitch-only recovery did not reach awaiting_review within bounded observation"

# ---------------------------------------------------------------------------
# 6. Final correctness/economic/no-rerender certification.
# ---------------------------------------------------------------------------
final_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
final_attempt_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
final_children="$(psql_scalar "$(child_jobs_sql)")"
final_succeeded_children="$(psql_scalar "$(child_jobs_status_sql succeeded)")"
final_parent_state="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
final_parent_reservation="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'reservation_id','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
final_parent_ledger="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'ledger_entry_id','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
review_pending="$(psql_scalar "select count(*) from public.v3_studio_review_items where stage_run_id='${FUSION_STAGE_ID}'::uuid and decision='pending';")"
active_outputs="$(psql_scalar "select count(*) from public.v3_studio_stage_outputs where stage_run_id='${FUSION_STAGE_ID}'::uuid and is_active=true;")"
final_media_id="$(psql_scalar "select media_id::text from public.v3_studio_stage_outputs where stage_run_id='${FUSION_STAGE_ID}'::uuid and is_active=true order by created_at desc limit 1;")"
final_stitch_ms="$(psql_scalar "select coalesce(metadata_json #>> '{background_coordinator,stitch_ms}','') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
finalized_at="$(psql_scalar "select coalesce(metadata_json #>> '{background_coordinator,finalized_at}','') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
active_final="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"

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
printf 'FINAL_RETRY_ATTEMPT_STATE=%s\n' "$final_attempt_state"
printf 'FINAL_FUSION_CHILD_JOBS=%s\n' "$final_children"
printf 'FINAL_FUSION_CHILD_SUCCEEDED=%s\n' "$final_succeeded_children"
printf 'NEW_PROVIDER_JOBS_CREATED=0\n'
printf 'FINAL_PARENT_PRICING_STATE=%s\n' "$final_parent_state"
printf 'FINAL_PARENT_RESERVATION_ID=%s\n' "$final_parent_reservation"
printf 'FINAL_PARENT_LEDGER_ENTRY_ID=%s\n' "$final_parent_ledger"
printf 'PARENT_CONSUME_EVENTS=%s\n' "$parent_consume_events"
printf 'PARENT_CREDIT_DELTA=%s\n' "$parent_credit_delta"
printf 'FINAL_STITCH_MS=%s\n' "$final_stitch_ms"
printf 'FINALIZED_AT=%s\n' "$finalized_at"
printf 'FINAL_MEDIA_ID=%s\n' "$final_media_id"
printf 'PENDING_FUSION_REVIEW_ITEMS=%s\n' "$review_pending"
printf 'ACTIVE_FUSION_OUTPUTS=%s\n' "$active_outputs"
printf 'ACTIVE_GENERATION_JOBS=%s\n' "$active_final"

[[ "$final_state" == "awaiting_review" ]] || fail "Fusion did not reach awaiting_review"
[[ "$final_attempt_state" == "succeeded" ]] || fail "retry attempt did not succeed"
[[ "$final_children" == "$EXPECTED_CHILDREN" ]] || fail "provider child count changed"
[[ "$final_succeeded_children" == "$EXPECTED_CHILDREN" ]] || fail "existing provider child success changed"
[[ "$final_parent_state" == "committed" ]] || fail "parent pricing did not commit"
[[ -n "$final_parent_reservation" ]] || fail "parent reservation missing from committed pricing"
[[ -n "$final_parent_ledger" ]] || fail "parent ledger id missing"
[[ "$parent_consume_events" == "1" ]] || fail "expected exactly one parent consume event; found $parent_consume_events"
python3 - "$parent_credit_delta" <<'PY'
from decimal import Decimal
import sys
assert Decimal(sys.argv[1]) == Decimal('-560'), sys.argv[1]
PY
[[ -n "$final_stitch_ms" ]] || fail "stitch timing telemetry missing"
[[ -n "$finalized_at" ]] || fail "finalization telemetry missing"
[[ -n "$final_media_id" ]] || fail "canonical final media missing"
[[ "$review_pending" == "1" ]] || fail "expected one pending Fusion HITL review"
[[ "$active_outputs" == "1" ]] || fail "expected one active Fusion output"
[[ "$active_final" == "0" ]] || fail "active generation jobs remain"

# Save a fresh signed read URL for operator review without dumping it into shell logs.
read_http="$(curl -sS -o "$RUN_DIR/final-read-url.json" -w '%{http_code}' \
  -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  "$FUSION_EXTENSION_URL/api/longform/v3/assets/$final_media_id/read-url")"
[[ "$read_http" == "200" ]] || fail "final scene read-url HTTP $read_http"
python3 - "$RUN_DIR/final-read-url.json" "$RUN_DIR/final-video-url.txt" <<'PY'
import json,sys
p=json.load(open(sys.argv[1],encoding='utf-8'))
url=str(p.get('read_url') or '')
assert url
open(sys.argv[2],'w',encoding='utf-8').write(url+'\n')
PY

echo
echo "============================================================"
echo " V3 FUSION STITCH-ONLY RECOVERY = PASS"
echo " existing provider children reused      = 28/28"
echo " new provider renders                   = 0"
echo " provider performance rerun             = NOT PERFORMED"
echo " retry attempt                           = succeeded"
echo " canonical stitched output              = created"
echo " parent pricing                          = committed / 560 credits"
echo " Fusion output awaiting HITL review      = 1"
echo " Fusion HITL approval                    = NOT PERFORMED"
echo " client-driven /sync                     = NOT USED"
echo " final media id                          = $final_media_id"
echo " review URL file                         = $RUN_DIR/final-video-url.txt"
echo "============================================================"
