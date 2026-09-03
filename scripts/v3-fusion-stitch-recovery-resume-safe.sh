#!/usr/bin/env bash
set -euo pipefail

# Resume the canonical V3 Fusion benchmark after one or more stitch-only failures.
# Provider generation is frozen PASS and MUST NOT be repeated. This launcher:
#   * accepts multiple prior failed stitch attempts,
#   * deploys Director's full-status SAS refresh guard,
#   * verifies all 28 existing provider artifacts have fresh readable SAS URLs,
#   * prices one parent retry with 0 required child renders,
#   * creates exactly one new stitch-only retry attempt,
#   * proves the existing 28 provider jobs are unchanged,
#   * observes background stitch/finalization without Director /sync,
#   * commits the parent price only after a canonical scene output exists.

readonly WORKFLOW_ID="06c5d43e-7bbc-4cb4-aef3-9df36886da3b"
readonly FUSION_STAGE_ID="4038a526-308a-49ba-959a-7e40f512c3b3"
readonly EXPECTED_CHILDREN="28"
readonly EXPECTED_AMOUNT="5.60"
readonly EXPECTED_CURRENCY="USD"
readonly EXPECTED_MINUTES="4"
readonly EXPECTED_CREDITS="560"
readonly CONFIRM_PHRASE="PAY 5.60 USD TO FINALIZE EXISTING 28 FUSION VIDEOS"

DF_EMAIL="${DF_EMAIL:-user_apple_iap_test1@desifaces.ai}"
CORE_URL="${CORE_URL:-http://127.0.0.1:18000}"
DIRECTOR_URL="${DIRECTOR_URL:-http://127.0.0.1:18011}"
FUSION_URL="${FUSION_URL:-http://127.0.0.1:18002}"
FUSION_EXTENSION_URL="${FUSION_EXTENSION_URL:-http://127.0.0.1:18006}"
POSTGRES_DB="${POSTGRES_DB:-desifaces_v3}"
POSTGRES_USER="${POSTGRES_USER:-desifaces_v3_admin}"
RUN_DIR="/tmp/v3-fusion-stitch-recovery-safe-${WORKFLOW_ID}"

mkdir -p "$RUN_DIR"
rm -f "$RUN_DIR"/*.json "$RUN_DIR"/*.txt "$RUN_DIR"/*.tsv 2>/dev/null || true

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
echo " V3 FUSION STITCH-ONLY RECOVERY — RESUME SAFE"
echo " workflow: $WORKFLOW_ID"
echo " Fusion stage: $FUSION_STAGE_ID"
echo " provider rerender: FORBIDDEN"
echo " existing provider children required: $EXPECTED_CHILDREN"
echo " expected new provider jobs: 0"
echo " HITL auto-approval: DISABLED"
echo "============================================================"

[[ -f scripts/v3-compose.sh ]] || fail "run from ~/workspace/desifaces-v3"
[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || fail "wrong branch"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || fail "refusing non-V3 DB: $POSTGRES_DB"

# ---------------------------------------------------------------------------
# 1. Durable recovery preflight. Multiple prior failed stitch attempts are valid.
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
baseline_attempt_count="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
baseline_latest_attempt_id="$(psql_scalar "select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
baseline_latest_attempt_no="$(psql_scalar "select attempt_no from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
baseline_latest_attempt_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
baseline_error_code="$(psql_scalar "select coalesce(error_code,'') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
baseline_error_message="$(psql_scalar "select coalesce(error_message,'') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
parent_state="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
child_total="$(psql_scalar "$(child_jobs_sql)")"
child_succeeded="$(psql_scalar "$(child_jobs_status_sql succeeded)")"
child_failed="$(psql_scalar "$(child_jobs_status_sql failed)")"
latest_reusable_children="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c
where a.attempt_id='${baseline_latest_attempt_id}'::uuid
  and lower(coalesce(c->>'status','')) in ('succeeded','completed','complete','ready')
  and coalesce(c->>'fusion_job_id','') <> ''
  and coalesce(c->>'video_url','') <> '';")"
latest_unique_reusable_turns="$(psql_scalar "
select count(distinct c->>'dialogue_turn_id')
from public.v3_studio_stage_attempts a
cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c
where a.attempt_id='${baseline_latest_attempt_id}'::uuid
  and lower(coalesce(c->>'status','')) in ('succeeded','completed','complete','ready')
  and coalesce(c->>'fusion_job_id','') <> '';")"

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
printf 'BASELINE_FUSION_ATTEMPTS=%s\n' "$baseline_attempt_count"
printf 'LATEST_FAILED_ATTEMPT_ID=%s\n' "$baseline_latest_attempt_id"
printf 'LATEST_FAILED_ATTEMPT_NO=%s\n' "$baseline_latest_attempt_no"
printf 'LATEST_FAILED_ATTEMPT_STATE=%s\n' "$baseline_latest_attempt_state"
printf 'LATEST_ERROR_CODE=%s\n' "$baseline_error_code"
printf 'LATEST_ERROR_MESSAGE=%s\n' "$baseline_error_message"
printf 'FUSION_PARENT_PRICING_STATE=%s\n' "$parent_state"
printf 'EXISTING_FUSION_CHILD_JOBS=%s\n' "$child_total"
printf 'EXISTING_FUSION_CHILD_SUCCEEDED=%s\n' "$child_succeeded"
printf 'EXISTING_FUSION_CHILD_FAILED=%s\n' "$child_failed"
printf 'LATEST_REUSABLE_CHILDREN=%s\n' "$latest_reusable_children"
printf 'LATEST_UNIQUE_REUSABLE_TURNS=%s\n' "$latest_unique_reusable_turns"
printf 'PARENT_CONSUME_EVENTS_BEFORE=%s\n' "$parent_consume_events_before"
printf 'PARENT_CREDIT_DELTA_BEFORE=%s\n' "$parent_credit_delta_before"

[[ "$active_jobs" == "0" ]] || fail "active generation/pricing work exists"
[[ "$current_stage" == "fusion" ]] || fail "workflow is not at Fusion"
[[ "$audio_approved" == "28" ]] || fail "Audio prerequisite is not 28/28 approved"
[[ "$fusion_state" == "failed" ]] || fail "Fusion stage is not failed"
[[ "$baseline_attempt_count" -ge 1 ]] || fail "no failed Fusion attempt exists"
[[ "$baseline_latest_attempt_state" == "failed" ]] || fail "latest Fusion attempt is not failed"
[[ "$baseline_error_message" == *"fusion_scene_stitch_failed:502"* ]] || fail "latest failure is not a scene-stitch 502"
[[ "$parent_state" == "released" ]] || fail "latest failed parent pricing is not released: $parent_state"
[[ "$child_total" == "$EXPECTED_CHILDREN" ]] || fail "expected 28 existing child jobs; found $child_total"
[[ "$child_succeeded" == "$EXPECTED_CHILDREN" ]] || fail "not all existing child jobs succeeded: $child_succeeded"
[[ "$child_failed" == "0" ]] || fail "existing child failure exists: $child_failed"
[[ "$latest_reusable_children" == "$EXPECTED_CHILDREN" ]] || fail "latest attempt does not preserve all 28 completed child jobs"
[[ "$latest_unique_reusable_turns" == "$EXPECTED_CHILDREN" ]] || fail "latest retry child-turn lineage is incomplete"
[[ "$parent_consume_events_before" == "0" ]] || fail "parent was already consumed; financial recovery assumptions invalid"
python3 - "$parent_credit_delta_before" <<'PY'
from decimal import Decimal
import sys
assert Decimal(sys.argv[1]) == Decimal("0"), sys.argv[1]
PY

echo "MULTI_ATTEMPT_RECOVERY_GATE=PASS"
echo "FROZEN_PROVIDER_PERFORMANCE=PASS_28_OF_28"
echo "FROZEN_PROVIDER_RERENDER_ALLOWED=NO"

# ---------------------------------------------------------------------------
# 2. Validate and deploy only the stitch/finalization control plane.
#    Provider Fusion worker is deliberately not rebuilt/restarted.
# ---------------------------------------------------------------------------
python3 -m py_compile \
  services/svc-director/app/app/fusion_execution_preserved_url_refresh.py \
  services/svc-director/app/app/fusion_execution_runtime.py \
  services/svc-fusion-extension/app/app/services/v3_stitch_resilience.py \
  services/svc-fusion-extension/app/app/api/routes/v3_scene_stitch.py

echo "STATIC_VALIDATION=PASS"
echo "BUILDING_RECOVERY_CONTROL_PLANE=STARTED"
compose build svc-director svc-fusion-extension svc-fusion-extension-stitch-worker
echo "RECOVERY_IMAGES_BUILD=PASS"

# Validate the just-built images before recreating running services.
compose run --rm --no-deps svc-director python - <<'PY'
from app.fusion_execution_parallel_dispatch import ParallelOrphanReconciledParentPricedSceneFusionExecutionService
from app.fusion_execution_runtime import V3ResilientSceneStitchClient
from app.fusion_execution_preserved_url_refresh import _fresh_video_artifact_url

assert getattr(
    ParallelOrphanReconciledParentPricedSceneFusionExecutionService,
    "_preserved_child_url_refresh_installed",
    False,
)
client = V3ResilientSceneStitchClient(base_url="http://example.invalid")
assert client.timeout_seconds >= 900.0, client.timeout_seconds
assert _fresh_video_artifact_url({
    "primary_video_url": "https://example.invalid/stale?sig=old",
    "artifacts": [{"kind": "video", "url": "https://example.invalid/fresh?sig=new"}],
}).endswith("fresh?sig=new")
print("DIRECTOR_PRESERVED_URL_REFRESH_IMAGE_GATE=PASS")
PY

compose run --rm --no-deps svc-fusion-extension python - <<'PY'
from app.services.v3_stitch_resilience import (
    _download_attempts,
    _download_concurrency,
    _download_timeout_seconds,
)
assert _download_timeout_seconds() == 300
assert _download_attempts() == 3
assert _download_concurrency(28) == 8
print("STITCH_RESILIENCE_IMAGE_GATE=PASS")
PY

echo "RECREATING_RECOVERY_CONTROL_PLANE=STARTED"
compose --profile v3-execution up -d --no-deps --force-recreate \
  svc-director svc-fusion-extension svc-fusion-extension-stitch-worker

for _ in $(seq 1 90); do
  director_ok=0
  extension_ok=0
  curl -fsS "$DIRECTOR_URL/api/health" >/dev/null 2>&1 && director_ok=1 || true
  curl -fsS "$FUSION_EXTENSION_URL/api/health" >/dev/null 2>&1 && extension_ok=1 || true
  if [[ "$director_ok" == "1" && "$extension_ok" == "1" ]]; then
    break
  fi
  sleep 1
done
curl -fsS "$DIRECTOR_URL/api/health" >/dev/null || fail "svc-director unhealthy after recovery deployment"
curl -fsS "$FUSION_EXTENSION_URL/api/health" >/dev/null || fail "svc-fusion-extension unhealthy after recovery deployment"

stitch_worker_running="$(docker inspect -f '{{.State.Running}}' df-v3-svc-fusion-extension-stitch-worker 2>/dev/null || true)"
stitch_worker_restarts="$(docker inspect -f '{{.RestartCount}}' df-v3-svc-fusion-extension-stitch-worker 2>/dev/null || true)"
[[ "$stitch_worker_running" == "true" ]] || fail "Fusion Extension stitch/coordinator worker not running"

echo "svc-director=HEALTHY"
echo "svc-fusion-extension=HEALTHY"
echo "STITCH_COORDINATOR_WORKER_RUNNING=$stitch_worker_running"
echo "STITCH_COORDINATOR_WORKER_RESTARTS=${stitch_worker_restarts:-unknown}"

# Prove deployment did not mutate the failed benchmark.
[[ "$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")" == "failed" ]] || fail "deployment unexpectedly changed Fusion stage"
[[ "$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")" == "$baseline_attempt_count" ]] || fail "deployment unexpectedly added a Fusion attempt"
[[ "$(psql_scalar "$(child_jobs_sql)")" == "$EXPECTED_CHILDREN" ]] || fail "deployment unexpectedly changed provider child count"
echo "RECOVERY_CONTROL_PLANE_DEPLOYMENT_GATE=PASS"

# ---------------------------------------------------------------------------
# 3. Fresh authentication and direct fresh-SAS verification for all 28 artifacts.
#    This is read-only and non-billable.
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

psql_scalar "
select c->>'fusion_job_id'
from public.v3_studio_stage_attempts a
cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c
where a.attempt_id='${baseline_latest_attempt_id}'::uuid
order by coalesce((c->>'sequence_no')::int,0);" > "$RUN_DIR/provider-job-ids.txt"

python3 - "$FUSION_URL" "$DF_BEARER_TOKEN" "$RUN_DIR/provider-job-ids.txt" <<'PY'
from __future__ import annotations
import concurrent.futures
import json
import sys
from urllib.request import Request, urlopen

base, token, path = sys.argv[1:]
job_ids = [line.strip() for line in open(path, encoding="utf-8") if line.strip()]
assert len(job_ids) == 28, len(job_ids)

def fetch(job_id: str):
    req = Request(
        f"{base.rstrip('/')}/jobs/{job_id}/status",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "desifaces-v3-recovery/1.0"},
    )
    with urlopen(req, timeout=45) as resp:
        payload = json.load(resp)
    assert str(payload.get("status") or "").lower() == "succeeded", (job_id, payload.get("status"))
    artifacts = list(payload.get("artifacts") or [])
    url = ""
    for item in artifacts:
        if "video" in str(item.get("kind") or "").lower() and str(item.get("url") or "").strip():
            url = str(item["url"]).strip()
            break
    if not url:
        for item in artifacts:
            if str(item.get("url") or "").strip():
                url = str(item["url"]).strip()
                break
    assert url, f"fresh artifact URL missing for {job_id}"
    # Validate the SAS without downloading the whole MP4.
    probe = Request(url, headers={"Range": "bytes=0-0", "User-Agent": "desifaces-v3-recovery/1.0"})
    with urlopen(probe, timeout=45) as resp:
        code = int(getattr(resp, "status", 200) or 200)
        assert code in (200, 206), (job_id, code)
        _ = resp.read(1)
    return job_id, url

with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
    results = list(ex.map(fetch, job_ids))
assert len(results) == 28
print("FRESH_CANONICAL_ARTIFACT_URLS=28/28")
print("FRESH_SAS_RANGE_READ=28/28")
print("FRESH_SAS_PREPAYMENT_GATE=PASS")
PY

# ---------------------------------------------------------------------------
# 4. Non-generating retry preview: 28 preserved, 0 required new provider renders.
# ---------------------------------------------------------------------------
preview_http="$(curl -sS -o "$RUN_DIR/retry-preview.json" -w '%{http_code}' \
  -X POST \
  -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"external_provider_ok":false}' \
  "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$FUSION_STAGE_ID/pricing-preview")"
[[ "$preview_http" == "200" ]] || { cat "$RUN_DIR/retry-preview.json" >&2; fail "stitch-only retry preview HTTP $preview_http"; }

python3 - "$RUN_DIR/retry-preview.json" "$RUN_DIR/retry-dispatch.json" <<'PY'
from __future__ import annotations
import json
import sys
from decimal import Decimal

src, dst = sys.argv[1:]
p = json.load(open(src, encoding="utf-8"))
assert int(p.get("turn_count") or 0) == 28, p
assert int(p.get("preserved_child_count") or 0) == 28, p
assert int(p.get("required_child_count") or 0) == 0, p
assert int(p.get("billable_parent_quote_count") or 0) == 1, p
assert int(p.get("billable_child_quote_count") or 0) == 0, p
assert list(p.get("children") or []) == [], p.get("children")

parent = p.get("parent_quote") or {}
pricing = parent.get("pricing") or {}
quote = str(pricing.get("quote_id") or "")
fingerprint = str(pricing.get("preview_fingerprint") or "")
amount = Decimal(str(pricing.get("estimated_amount")))
currency = str(pricing.get("currency") or "")
minutes = int(parent.get("billable_minutes") or pricing.get("estimated_units") or 0)
assert quote and fingerprint
assert amount == Decimal("5.60"), amount
assert currency == "USD", currency
assert minutes == 4, minutes
assert not pricing.get("reservation_id"), pricing

json.dump(
    {
        "parent_confirmation": {
            "quote_id": quote,
            "preview_fingerprint": fingerprint,
        },
        "child_confirmations": [],
        # Required by the current Director dispatch contract. Because required_child_count
        # is zero, no external provider create call is reachable in this retry.
        "external_provider_ok": True,
        "user_confirmed": True,
    },
    open(dst, "w", encoding="utf-8"),
    indent=2,
)
print("STITCH_ONLY_RETRY_PREVIEW=PASS")
print("PRESERVED_CHILDREN=28")
print("REQUIRED_NEW_CHILDREN=0")
print("NEW_CHILD_PRICING_QUOTES=0")
print("PARENT_QUOTES=1")
print(f"RETRY_BILLABLE_MINUTES={minutes}")
print(f"RETRY_PARENT_AMOUNT={amount:.2f}")
print(f"RETRY_PARENT_CURRENCY={currency}")
PY

[[ "$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")" == "$baseline_attempt_count" ]] || fail "preview created a Fusion attempt"
[[ "$(psql_scalar "$(child_jobs_sql)")" == "$EXPECTED_CHILDREN" ]] || fail "preview created a provider child job"
parent_state_after_preview="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
[[ "$parent_state_after_preview" == "quoted" ]] || fail "retry parent preview did not persist quoted state: $parent_state_after_preview"
echo "RETRY_PREVIEW_NON_BILLABLE_GATE=PASS"

# ---------------------------------------------------------------------------
# 5. Financial confirmation for the logical parent output only.
# ---------------------------------------------------------------------------
echo
echo "============================================================"
echo " STITCH-ONLY FINALIZATION CONFIRMATION REQUIRED"
echo " Existing provider videos reused: 28"
echo " New provider renders: 0"
echo " Provider rerender charge: 0"
echo " Parent application charge on successful final scene: $EXPECTED_AMOUNT $EXPECTED_CURRENCY"
echo " Failed reservations remain released; parent commits only after scene succeeds."
echo "============================================================"

if [[ -z "${PAYMENT_CONFIRMATION:-}" ]]; then
  read -r -p "Type exactly '$CONFIRM_PHRASE' to continue: " PAYMENT_CONFIRMATION
fi
[[ "$PAYMENT_CONFIRMATION" == "$CONFIRM_PHRASE" ]] || { echo "STITCH_ONLY_PAYMENT_NOT_CONFIRMED=STOP"; exit 0; }
echo "STITCH_ONLY_PAYMENT_CONFIRMATION=ACCEPTED"

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
[[ "$baseline_child_count" == "$EXPECTED_CHILDREN" ]] || fail "provider child count changed before retry"

# One Director dispatch. The pre-dispatch wrapper first refreshes the latest failed
# attempt's 28 signed URLs from svc-fusion /status, then original dispatch reserves
# the parent and creates one new Director attempt with zero child create calls.
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
import json
import sys
p = json.load(open(sys.argv[1], encoding="utf-8"))
children = list(p.get("children") or [])
assert len(children) == 28, len(children)
assert all(bool(c.get("reused_from_prior_attempt")) for c in children), children
assert all(bool(c.get("video_url_refreshed_for_stitch")) for c in children), children
assert str(p.get("stage_state") or "") == "generating", p
pricing = p.get("parent_pricing") or {}
assert str(pricing.get("state") or "").lower() == "reserved", pricing
assert pricing.get("reservation_id"), pricing
print("STITCH_ONLY_DISPATCH=PASS")
print(f"RETRY_ATTEMPT_ID={p.get('attempt_id')}")
print(f"RETRY_ATTEMPT_COUNT={p.get('attempt_count')}")
print("RESPONSE_REUSED_CHILDREN=28")
print("RESPONSE_REFRESHED_CHILD_URLS=28")
print(f"RETRY_PARENT_RESERVATION_ID={pricing.get('reservation_id')}")
PY

expected_attempt_count=$((baseline_attempt_count + 1))
attempts_after_dispatch="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
retry_attempt_id="$(psql_scalar "select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
retry_attempt_no="$(psql_scalar "select attempt_no from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
retry_kind="$(psql_scalar "select attempt_kind from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
retry_outcome="$(psql_scalar "select coalesce(metadata_json->>'dispatch_outcome','') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
retry_preserved="$(psql_scalar "select coalesce(metadata_json->>'preserved_child_count','0') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
previous_refresh_count="$(psql_scalar "select coalesce(metadata_json #>> '{preserved_url_refresh,refreshed_count}','0') from public.v3_studio_stage_attempts where attempt_id='${baseline_latest_attempt_id}'::uuid;")"
previous_refresh_new_jobs="$(psql_scalar "select coalesce(metadata_json #>> '{preserved_url_refresh,new_provider_jobs}','-1') from public.v3_studio_stage_attempts where attempt_id='${baseline_latest_attempt_id}'::uuid;")"
retry_refreshed_children="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c
where a.attempt_id='${retry_attempt_id}'::uuid
  and coalesce((c->>'video_url_refreshed_for_stitch')::boolean,false)=true
  and c->>'video_url_refresh_source'='svc-fusion-full-status-artifact';")"
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
printf 'RETRY_ATTEMPT_ID=%s\n' "$retry_attempt_id"
printf 'RETRY_ATTEMPT_NO=%s\n' "$retry_attempt_no"
printf 'RETRY_ATTEMPT_KIND=%s\n' "$retry_kind"
printf 'RETRY_DISPATCH_OUTCOME=%s\n' "$retry_outcome"
printf 'RETRY_PRESERVED_CHILDREN=%s\n' "$retry_preserved"
printf 'PREVIOUS_ATTEMPT_REFRESHED_URLS=%s\n' "$previous_refresh_count"
printf 'PREVIOUS_ATTEMPT_REFRESH_NEW_PROVIDER_JOBS=%s\n' "$previous_refresh_new_jobs"
printf 'RETRY_ATTEMPT_REFRESHED_CHILDREN=%s\n' "$retry_refreshed_children"
printf 'FUSION_CHILD_JOBS_AFTER_RETRY_DISPATCH=%s\n' "$children_after_dispatch"

[[ "$attempts_after_dispatch" == "$expected_attempt_count" ]] || fail "retry did not create exactly one new Director attempt"
[[ "$retry_kind" == "retry" ]] || fail "new attempt is not a technical retry"
[[ "$retry_outcome" == "stitch_only_retry" ]] || fail "retry did not take stitch-only branch: $retry_outcome"
[[ "$retry_preserved" == "$EXPECTED_CHILDREN" ]] || fail "retry did not preserve all 28 children"
[[ "$previous_refresh_count" == "$EXPECTED_CHILDREN" ]] || fail "pre-dispatch full-status refresh did not refresh all 28 URLs"
[[ "$previous_refresh_new_jobs" == "0" ]] || fail "URL refresh unexpectedly created provider jobs"
[[ "$retry_refreshed_children" == "$EXPECTED_CHILDREN" ]] || fail "new attempt did not inherit 28 refreshed child URLs"
[[ "$children_after_dispatch" == "$baseline_child_count" ]] || fail "FORBIDDEN: stitch retry created new Fusion child jobs"
[[ "$child_max_created_after" == "$baseline_child_max_created" ]] || fail "FORBIDDEN: provider child creation timestamp changed"

echo "FRESH_URL_REFRESH_BEFORE_RESERVE_GATE=PASS"
echo "ZERO_NEW_PROVIDER_JOBS_GATE=PASS"
echo "EXTERNAL_PROVIDER_RERENDER=NOT_CALLED"

# ---------------------------------------------------------------------------
# 6. Observe durable background finalization only. No Director /sync call.
# ---------------------------------------------------------------------------
echo "BACKGROUND_STITCH_OBSERVER=STARTED"
terminal=0
for i in $(seq 1 180); do
  stage_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  attempt_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where attempt_id='${retry_attempt_id}'::uuid;")"
  phase="$(psql_scalar "select coalesce(metadata_json #>> '{background_coordinator,phase}','') from public.v3_studio_stage_attempts where attempt_id='${retry_attempt_id}'::uuid;")"
  stitch_ms="$(psql_scalar "select coalesce(metadata_json #>> '{background_coordinator,stitch_ms}','') from public.v3_studio_stage_attempts where attempt_id='${retry_attempt_id}'::uuid;")"
  parent_loop="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  current_children="$(psql_scalar "$(child_jobs_sql)")"
  elapsed=$((i*10))
  echo "STITCH_PROGRESS stage=$stage_state attempt=$attempt_state phase=${phase:-unknown} preserved=28 new_provider_jobs=$((current_children-baseline_child_count)) parent=$parent_loop stitch_ms=${stitch_ms:-pending} elapsed_s=$elapsed"

  [[ "$current_children" == "$baseline_child_count" ]] || fail "FORBIDDEN: provider child count increased during stitch-only recovery"

  if [[ "$stage_state" == "failed" || "$attempt_state" == "failed" ]]; then
    error_now="$(psql_scalar "select coalesce(error_message,'') from public.v3_studio_stage_attempts where attempt_id='${retry_attempt_id}'::uuid;")"
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
# 7. Final correctness, economic, and no-rerender certification.
# ---------------------------------------------------------------------------
final_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
final_attempt_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where attempt_id='${retry_attempt_id}'::uuid;")"
final_children="$(psql_scalar "$(child_jobs_sql)")"
final_succeeded_children="$(psql_scalar "$(child_jobs_status_sql succeeded)")"
final_parent_state="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
final_parent_reservation="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'reservation_id','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
final_parent_ledger="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'ledger_entry_id','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
review_pending="$(psql_scalar "select count(*) from public.v3_studio_review_items where stage_run_id='${FUSION_STAGE_ID}'::uuid and decision='pending';")"
active_outputs="$(psql_scalar "select count(*) from public.v3_studio_stage_outputs where stage_run_id='${FUSION_STAGE_ID}'::uuid and is_active=true;")"
final_media_id="$(psql_scalar "select media_id::text from public.v3_studio_stage_outputs where stage_run_id='${FUSION_STAGE_ID}'::uuid and is_active=true order by created_at desc limit 1;")"
final_stitch_ms="$(psql_scalar "select coalesce(metadata_json #>> '{background_coordinator,stitch_ms}','') from public.v3_studio_stage_attempts where attempt_id='${retry_attempt_id}'::uuid;")"
finalized_at="$(psql_scalar "select coalesce(metadata_json #>> '{background_coordinator,finalized_at}','') from public.v3_studio_stage_attempts where attempt_id='${retry_attempt_id}'::uuid;")"
final_active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
parent_consume_events_after="$(psql_scalar "
select count(*)
from public.pricing_credit_ledger_events l
join public.v3_studio_workflows w on w.owner_user_id=l.user_id
where w.workflow_id='${WORKFLOW_ID}'::uuid
  and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
parent_credit_delta_after="$(psql_scalar "
select coalesce(sum(l.credits_delta),0)
from public.pricing_credit_ledger_events l
join public.v3_studio_workflows w on w.owner_user_id=l.user_id
where w.workflow_id='${WORKFLOW_ID}'::uuid
  and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"

printf 'FINAL_FUSION_STATE=%s\n' "$final_state"
printf 'FINAL_RETRY_ATTEMPT_STATE=%s\n' "$final_attempt_state"
printf 'FINAL_PROVIDER_CHILD_JOBS=%s\n' "$final_children"
printf 'FINAL_PROVIDER_CHILD_SUCCEEDED=%s\n' "$final_succeeded_children"
printf 'FINAL_PARENT_PRICING_STATE=%s\n' "$final_parent_state"
printf 'FINAL_PARENT_RESERVATION_ID=%s\n' "$final_parent_reservation"
printf 'FINAL_PARENT_LEDGER_ENTRY_ID=%s\n' "$final_parent_ledger"
printf 'FINAL_ACTIVE_OUTPUTS=%s\n' "$active_outputs"
printf 'FINAL_PENDING_REVIEW_ITEMS=%s\n' "$review_pending"
printf 'FINAL_MEDIA_ID=%s\n' "$final_media_id"
printf 'FINAL_STITCH_MS=%s\n' "$final_stitch_ms"
printf 'FINALIZED_AT=%s\n' "$finalized_at"
printf 'PARENT_CONSUME_EVENTS_AFTER=%s\n' "$parent_consume_events_after"
printf 'PARENT_CREDIT_DELTA_AFTER=%s\n' "$parent_credit_delta_after"
printf 'ACTIVE_GENERATION_JOBS_AFTER=%s\n' "$final_active_jobs"

[[ "$final_state" == "awaiting_review" ]] || fail "Fusion did not reach awaiting_review"
[[ "$final_attempt_state" == "succeeded" ]] || fail "stitch-only retry attempt did not succeed"
[[ "$final_children" == "$EXPECTED_CHILDREN" ]] || fail "provider child count changed"
[[ "$final_succeeded_children" == "$EXPECTED_CHILDREN" ]] || fail "existing provider children no longer all succeeded"
[[ "$final_parent_state" == "committed" ]] || fail "parent price did not commit after successful scene"
[[ -n "$final_parent_reservation" ]] || fail "committed parent reservation id missing"
[[ -n "$final_parent_ledger" ]] || fail "parent ledger entry id missing"
[[ "$active_outputs" == "1" ]] || fail "expected one active canonical Fusion output"
[[ "$review_pending" == "1" ]] || fail "expected one pending Fusion HITL review"
[[ -n "$final_media_id" ]] || fail "final Fusion media id missing"
[[ -n "$final_stitch_ms" ]] || fail "stitch timing telemetry missing"
[[ -n "$finalized_at" ]] || fail "finalized_at telemetry missing"
[[ "$parent_consume_events_after" == "1" ]] || fail "expected exactly one parent consume event after successful recovery"
[[ "$final_active_jobs" == "0" ]] || fail "active generation jobs remain after recovery"
[[ "$final_children" == "$baseline_child_count" ]] || fail "FORBIDDEN: provider rerender occurred"

python3 - "$parent_credit_delta_after" <<'PY'
from decimal import Decimal
import sys
assert Decimal(sys.argv[1]) == Decimal("-560"), sys.argv[1]
PY

echo
printf '%s\n' "============================================================"
printf '%s\n' " V3 FUSION STITCH-ONLY RECOVERY = PASS"
printf '%s\n' " existing provider videos reused       = 28/28"
printf '%s\n' " new provider Fusion jobs              = 0"
printf '%s\n' " external provider rerender            = NOT CALLED"
printf '%s\n' " fresh canonical SAS URLs              = 28/28"
printf '%s\n' " scene stitch                          = PASS"
printf '%s\n' " canonical Fusion output               = 1"
printf '%s\n' " parent pricing                        = COMMITTED"
printf '%s\n' " committed parent credits              = 560"
printf '%s\n' " Fusion output awaiting HITL review    = 1"
printf '%s\n' " Fusion HITL approval                  = NOT PERFORMED"
printf '%s\n' " client-driven /sync                   = NOT USED"
printf '%s\n' " active generation jobs                = 0"
printf '%s\n' "============================================================"

echo "NEXT: review the single stitched Fusion output in HITL; do not rerun provider generation."
