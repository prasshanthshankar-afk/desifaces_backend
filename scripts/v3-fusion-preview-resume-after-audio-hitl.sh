#!/usr/bin/env bash
set -euo pipefail

# Resume the V3 benchmark after Audio HITL has already completed.
# Deploys the non-generating child-pricing consent-boundary fix, obtains the
# single Fusion parent quote, and stops before reservation/dispatch/provider use.

WORKFLOW_ID="${WORKFLOW_ID:-06c5d43e-7bbc-4cb4-aef3-9df36886da3b}"
FUSION_STAGE_ID="${FUSION_STAGE_ID:-4038a526-308a-49ba-959a-7e40f512c3b3}"
DF_EMAIL="${DF_EMAIL:-user_apple_iap_test1@desifaces.ai}"
CORE_URL="${CORE_URL:-http://127.0.0.1:18000}"
DIRECTOR_URL="${DIRECTOR_URL:-http://127.0.0.1:18011}"
POSTGRES_DB="${POSTGRES_DB:-desifaces_v3}"
POSTGRES_USER="${POSTGRES_USER:-desifaces_v3_admin}"
RUN_DIR="/tmp/v3-fusion-preview-resume-${WORKFLOW_ID}"

mkdir -p "$RUN_DIR"
rm -f "$RUN_DIR"/*.json 2>/dev/null || true

compose() { bash scripts/v3-compose.sh "$@"; }
psql_scalar() {
  compose exec -T desifaces-db \
    psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}
fail() { echo "ERROR: $*" >&2; exit 1; }

wait_health() {
  local url="$1" name="$2"
  for _ in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "$name=HEALTHY"
      return 0
    fi
    sleep 1
  done
  fail "$name did not become healthy"
}

echo "============================================================"
echo " V3 FUSION PREVIEW RESUME AFTER AUDIO HITL"
echo " workflow: $WORKFLOW_ID"
echo " Fusion stage: $FUSION_STAGE_ID"
echo " parent reserve: DISABLED"
echo " Fusion dispatch: DISABLED"
echo " external provider generation: DISABLED"
echo "============================================================"

[[ -f scripts/v3-compose.sh ]] || fail "run from ~/workspace/desifaces-v3"
[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || fail "wrong branch"

# Current-state safety gate: Audio is already approved, workflow is at Fusion,
# and the failed preview must not have created execution/billing work.
active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
current_stage="$(psql_scalar "select current_stage from public.v3_studio_workflows where workflow_id='${WORKFLOW_ID}'::uuid;")"
audio_approved="$(psql_scalar "select count(*) from public.v3_studio_stage_runs where workflow_id='${WORKFLOW_ID}'::uuid and stage_type='audio' and state='approved';")"
audio_review_approved="$(psql_scalar "
select count(*) from public.v3_studio_review_items r
join public.v3_studio_stage_runs s on s.stage_run_id=r.stage_run_id
where s.workflow_id='${WORKFLOW_ID}'::uuid and s.stage_type='audio' and r.decision='approved';")"
fusion_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid and workflow_id='${WORKFLOW_ID}'::uuid;")"
fusion_attempts="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"

printf 'ACTIVE_GENERATION_JOBS=%s\n' "$active_jobs"
printf 'WORKFLOW_CURRENT_STAGE=%s\n' "$current_stage"
printf 'AUDIO_APPROVED=%s\n' "$audio_approved"
printf 'AUDIO_REVIEW_APPROVED=%s\n' "$audio_review_approved"
printf 'FUSION_STATE=%s\n' "$fusion_state"
printf 'FUSION_ATTEMPTS=%s\n' "$fusion_attempts"

[[ "$active_jobs" == "0" ]] || fail "active work exists"
[[ "$current_stage" == "fusion" ]] || fail "workflow is not at Fusion"
[[ "$audio_approved" == "28" && "$audio_review_approved" == "28" ]] || fail "Audio HITL is not 28/28 approved"
[[ "$fusion_state" == "pending" ]] || fail "Fusion stage is not pending"
[[ "$fusion_attempts" == "0" ]] || fail "Fusion attempts already exist"
echo "RESUME_SAFETY_GATE=PASS"

# Static validation before deployment.
python3 -m py_compile \
  services/svc-director/app/app/fusion_execution_runtime.py \
  services/svc-director/tests/test_fusion_internal_child_preview_consent.py

echo "STATIC_VALIDATION=PASS"

# Only Director changed. Do not restart Fusion/Audio workers or pricing services.
echo
echo "===== BUILD + RECREATE DIRECTOR ONLY ====="
compose build svc-director
compose up -d --no-deps --force-recreate svc-director
wait_health "$DIRECTOR_URL/api/health" "svc-director"

# Re-prove that the restart itself created no work.
active_after_restart="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
fusion_attempts_after_restart="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
[[ "$active_after_restart" == "0" && "$fusion_attempts_after_restart" == "0" ]] || fail "restart changed benchmark execution state"
echo "POST_DEPLOY_EXECUTION_GATE=PASS"

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

# Non-billable/non-generating Fusion preview. external_provider_ok remains FALSE at
# the Director/user contract. Runtime uses an ephemeral true value only while
# asking svc-fusion to verify the internal child pricing-suppression contract.
preview_http="$(curl -sS -o "$RUN_DIR/fusion-preview.json" -w '%{http_code}' \
  -X POST \
  -H "Authorization: Bearer $DF_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"external_provider_ok":false}' \
  "$DIRECTOR_URL/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$FUSION_STAGE_ID/pricing-preview")"

if [[ "$preview_http" != "200" ]]; then
  cat "$RUN_DIR/fusion-preview.json" >&2
  fail "Fusion pricing preview HTTP $preview_http"
fi

python3 - "$RUN_DIR/fusion-preview.json" <<'PY'
from __future__ import annotations
import json
import sys

p=json.load(open(sys.argv[1],encoding='utf-8'))

assert int(p.get('turn_count') or 0) == 28, p
assert int(p.get('billable_parent_quote_count') or 0) == 1, p
assert int(p.get('billable_child_quote_count') or 0) == 0, p
assert int(p.get('child_pricing_suppressed') or 0) == 28, p
assert int(p.get('required_child_count') or 0) == 28, p
assert int(p.get('preserved_child_count') or 0) == 0, p

children=list(p.get('children') or [])
assert len(children) == 28, len(children)
for child in children:
    assert child.get('pricing_suppressed') is True, child
    pricing=child.get('pricing') or {}
    assert str(pricing.get('state') or '').lower() == 'suppressed', child
    assert not pricing.get('quote_id'), child
    assert not pricing.get('reservation_id'), child
    assert str(pricing.get('amount') or '0') in {'0','0.0','0.00'}, child

parent=p.get('parent_quote') or {}
pricing=parent.get('pricing') or {}
quote=str(pricing.get('quote_id') or '')
fingerprint=str(pricing.get('preview_fingerprint') or '')
assert quote and fingerprint, parent
assert str(pricing.get('unit_type') or '').lower() == 'minute', pricing
assert not pricing.get('reservation_id'), pricing

minutes=int(parent.get('billable_minutes') or pricing.get('estimated_units') or 0)
assert minutes > 0, parent
assert int(pricing.get('estimated_units') or 0) == minutes, parent

print('FUSION_PREVIEW=PASS')
print('FUSION_TURNS=28')
print('FUSION_PARENT_QUOTE_COUNT=1')
print('FUSION_CHILD_BILLABLE_QUOTES=0')
print('FUSION_CHILD_PRICING_SUPPRESSED=28')
print(f'FUSION_TOTAL_AUDIO_DURATION_SEC={parent.get("total_audio_duration_sec")}')
print(f'FUSION_BILLABLE_MINUTES={minutes}')
print(f'FUSION_PARENT_QUOTE_ID={quote}')
print(f'FUSION_PARENT_PREVIEW_FINGERPRINT={fingerprint}')
print(f'FUSION_PARENT_ESTIMATED_AMOUNT={pricing.get("estimated_amount")}')
print(f'FUSION_PARENT_CURRENCY={pricing.get("currency")}')
print(f'FUSION_PARENT_BILLING_MODE={pricing.get("billing_mode")}')
print(f'FUSION_PARENT_SETTLEMENT_MODE={pricing.get("settlement_mode")}')
print(f'FUSION_PARENT_SKU={pricing.get("sku_code")}')
print(f'FUSION_PARENT_LEAF_SKU={pricing.get("leaf_sku_code")}')
print(f'FUSION_PARENT_BEFORE_CREDITS={pricing.get("before_credits")}')
print(f'FUSION_PARENT_AFTER_ESTIMATED_CREDITS={pricing.get("after_estimated_credits")}')
PY

# Hard proof: preview created no Fusion execution and no active owner jobs.
fusion_attempts_final="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
fusion_state_final="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
active_final="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
parent_pricing_state="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
parent_reservation="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'reservation_id','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"

[[ "$fusion_attempts_final" == "0" ]] || fail "Fusion preview created an attempt"
[[ "$fusion_state_final" == "pending" ]] || fail "Fusion stage state changed during preview: $fusion_state_final"
[[ "$active_final" == "0" ]] || fail "Fusion preview created active jobs"
[[ "$parent_pricing_state" == "quoted" ]] || fail "parent pricing was not persisted as quoted: $parent_pricing_state"
[[ -z "$parent_reservation" ]] || fail "parent reservation unexpectedly exists: $parent_reservation"

echo "FUSION_EXECUTION_NOT_STARTED=PASS"
echo "FUSION_PARENT_PRICING_STATE=quoted"
echo "FUSION_PARENT_RESERVATION=NONE"

echo
echo "============================================================"
echo " V3 FUSION PARENT PREVIEW RESUME = PASS"
echo " Audio HITL approved                  = 28/28"
echo " workflow current stage               = fusion"
echo " Fusion parent quote                  = 1"
echo " Fusion billable child quotes         = 0"
echo " Fusion child pricing suppressed      = 28/28"
echo " Fusion attempts                      = 0"
echo " active generation jobs               = 0"
echo " Fusion reservation                   = NOT CALLED"
echo " Fusion dispatch                      = NOT CALLED"
echo " external provider generation consent = NOT GRANTED"
echo "============================================================"
echo "STOP: use the exact parent exposure above for the paid Fusion confirmation gate."
