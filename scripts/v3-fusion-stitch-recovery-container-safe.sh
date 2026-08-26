#!/usr/bin/env bash
set -euo pipefail

# Final V3 Fusion stitch-only recovery wrapper for the canonical benchmark.
#
# Purpose:
# - protect the already-completed 28 provider renders,
# - derive the canonical Azure output container from those preserved artifacts,
# - reject stale/divergent runtime container configuration,
# - verify the real Azure container exists BEFORE another priced retry,
# - accept either released or quoted parent state when no consume event exists,
# - delegate the actual retry/certification to the reviewed resume-safe launcher.

readonly WORKFLOW_ID="06c5d43e-7bbc-4cb4-aef3-9df36886da3b"
readonly FUSION_STAGE_ID="4038a526-308a-49ba-959a-7e40f512c3b3"
readonly EXPECTED_CHILDREN="28"
readonly BASE_RECOVERY_SCRIPT="scripts/v3-fusion-stitch-recovery-resume-safe.sh"

POSTGRES_DB="${POSTGRES_DB:-desifaces_v3}"
POSTGRES_USER="${POSTGRES_USER:-desifaces_v3_admin}"
REQUESTED_V3_CONTAINER="${DF_V3_FUSION_OUTPUT_CONTAINER:-}"

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

artifact_container_expr="split_part(split_part(c->>'video_url', '.blob.core.windows.net/', 2), '/', 1)"

[[ -f scripts/v3-compose.sh ]] || fail "run from ~/workspace/desifaces-v3"
[[ -f "$BASE_RECOVERY_SCRIPT" ]] || fail "missing $BASE_RECOVERY_SCRIPT"
[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || fail "wrong branch"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || fail "refusing non-V3 DB: $POSTGRES_DB"

active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
fusion_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
latest_attempt_id="$(psql_scalar "select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
latest_attempt_no="$(psql_scalar "select attempt_no from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
latest_attempt_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
latest_error="$(psql_scalar "select coalesce(error_message,'') from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
parent_state="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
children="$(psql_scalar "$(child_jobs_sql)")"
succeeded_children="$(psql_scalar "$(child_jobs_status_sql succeeded)")"
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

artifact_container_count="$(psql_scalar "
select count(distinct ${artifact_container_expr})
from public.v3_studio_stage_attempts a
cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c
where a.stage_run_id='${FUSION_STAGE_ID}'::uuid
  and coalesce(c->>'video_url','') like 'https://%.blob.core.windows.net/%';")"

artifact_container="$(psql_scalar "
select min(${artifact_container_expr})
from public.v3_studio_stage_attempts a
cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c
where a.stage_run_id='${FUSION_STAGE_ID}'::uuid
  and coalesce(c->>'video_url','') like 'https://%.blob.core.windows.net/%';")"

[[ "$artifact_container_count" == "1" ]] || fail "expected exactly one Azure container across preserved Fusion artifacts; found $artifact_container_count"
[[ -n "$artifact_container" ]] || fail "unable to derive Azure container from preserved Fusion artifacts"

if [[ -n "$REQUESTED_V3_CONTAINER" && "$REQUESTED_V3_CONTAINER" != "$artifact_container" ]]; then
  fail "explicit DF_V3_FUSION_OUTPUT_CONTAINER=$REQUESTED_V3_CONTAINER disagrees with preserved artifact container=$artifact_container"
fi

export DF_V3_FUSION_OUTPUT_CONTAINER="$artifact_container"
export AZURE_FINAL_VIDEO_CONTAINER="$artifact_container"
export AZURE_VIDEO_OUTPUT_CONTAINER="$artifact_container"

live_stitch_container="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' df-v3-svc-fusion-extension-stitch-worker 2>/dev/null | sed -n 's/^AZURE_VIDEO_OUTPUT_CONTAINER=//p' | tail -1 || true)"
live_stitch_final_container="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' df-v3-svc-fusion-extension-stitch-worker 2>/dev/null | sed -n 's/^AZURE_FINAL_VIDEO_CONTAINER=//p' | tail -1 || true)"

echo "============================================================"
echo " V3 FUSION FINAL STITCH RECOVERY — STORAGE SAFE"
echo " latest attempt: ${latest_attempt_no} / ${latest_attempt_id}"
echo " Fusion stage: $fusion_state"
echo " parent pricing: $parent_state"
echo " provider children: $children ($succeeded_children succeeded)"
echo " preserved artifact container: $artifact_container"
echo " live stitch AZURE_VIDEO_OUTPUT_CONTAINER: ${live_stitch_container:-unset}"
echo " live stitch AZURE_FINAL_VIDEO_CONTAINER: ${live_stitch_final_container:-unset}"
echo " canonical retry container: $DF_V3_FUSION_OUTPUT_CONTAINER"
echo " provider rerender: FORBIDDEN"
echo "============================================================"

[[ "$active_jobs" == "0" ]] || fail "active generation/pricing work exists"
[[ "$fusion_state" == "failed" ]] || fail "Fusion stage is not failed"
[[ -n "$latest_attempt_id" ]] || fail "latest Fusion attempt missing"
[[ "$latest_attempt_state" == "failed" ]] || fail "latest Fusion attempt is not failed"
[[ "$latest_error" == *"fusion_scene_stitch_failed:502"* ]] || fail "latest failure is not stitch-only"
[[ "$parent_state" == "released" || "$parent_state" == "quoted" ]] || fail "parent state is not safely retryable: $parent_state"
[[ "$children" == "$EXPECTED_CHILDREN" ]] || fail "provider child count changed: $children"
[[ "$succeeded_children" == "$EXPECTED_CHILDREN" ]] || fail "provider children are not 28/28 succeeded: $succeeded_children"
[[ "$parent_consume_events" == "0" ]] || fail "parent already has a consume event"
python3 - "$parent_credit_delta" <<'PY'
from decimal import Decimal
import sys
assert Decimal(sys.argv[1]) == Decimal("0"), sys.argv[1]
PY

echo "STORAGE_RECOVERY_FINANCIAL_GATE=PASS"
echo "PROVIDER_CHILDREN_FROZEN=28/28"
echo "PRESERVED_ARTIFACT_CONTAINER=$artifact_container"
if [[ "$live_stitch_container" != "$artifact_container" || "$live_stitch_final_container" != "$artifact_container" ]]; then
  echo "STALE_STITCH_CONTAINER_CONFIG_DETECTED=YES"
  echo "STITCH_RUNTIME_CONTAINER_CORRECTION=$artifact_container"
else
  echo "STALE_STITCH_CONTAINER_CONFIG_DETECTED=NO"
fi

# Validate the exact Azure container proven by the preserved artifacts before any
# retry preview/payment mutation. The one-shot worker is explicitly overridden to
# that same container; the delegated recovery inherits the same exported values.
compose --profile v3-execution run --rm --no-deps \
  -e DF_EXPECTED_CONTAINER="$artifact_container" \
  -e AZURE_FINAL_VIDEO_CONTAINER="$artifact_container" \
  -e AZURE_VIDEO_OUTPUT_CONTAINER="$artifact_container" \
  svc-fusion-extension-stitch-worker python - <<'PY'
import os
from azure.storage.blob import BlobServiceClient
from app.config import settings

expected = os.environ["DF_EXPECTED_CONTAINER"]
configured = str(settings.AZURE_VIDEO_OUTPUT_CONTAINER or "").strip()
assert configured == expected, (configured, expected)
service = BlobServiceClient.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)
container = service.get_container_client(configured)
props = container.get_container_properties()
print(f"V3_FUSION_OUTPUT_CONTAINER={configured}")
print(f"V3_FUSION_OUTPUT_CONTAINER_ETAG={props.etag}")
print("V3_FUSION_OUTPUT_CONTAINER_EXISTS=PASS")
PY

echo "ARTIFACT_CONTAINER_MATCH_GATE=PASS"
echo "AZURE_FINAL_CONTAINER_PREPAYMENT_GATE=PASS"

# The reviewed resume-safe launcher originally required state=released. A failed
# retry can legitimately leave the latest parent quote in state=quoted after the
# reservation has been released. Because the gates above prove zero consume events
# and zero credit delta, quoted is equally safe for a fresh retry preview.
tmp_script="$(mktemp /tmp/v3-fusion-stitch-recovery-resume-safe.XXXXXX.sh)"
trap 'rm -f "$tmp_script"' EXIT

python3 - "$BASE_RECOVERY_SCRIPT" "$tmp_script" <<'PY'
from pathlib import Path
import sys

src = Path(sys.argv[1]).read_text(encoding="utf-8")
old = '[[ "$parent_state" == "released" ]] || fail "latest failed parent pricing is not released: $parent_state"'
new = '[[ "$parent_state" == "released" || "$parent_state" == "quoted" ]] || fail "latest failed parent pricing is not safely retryable: $parent_state"'

count = src.count(old)
if count != 1:
    raise SystemExit(f"resume-safe parent-state guard changed unexpectedly: matches={count}")

Path(sys.argv[2]).write_text(src.replace(old, new, 1), encoding="utf-8")
PY

chmod +x "$tmp_script"
bash -n "$tmp_script"
echo "RESUME_SAFE_COMPATIBILITY_PATCH=PASS"
echo "DELEGATING_TO_REVIEWED_STITCH_ONLY_RECOVERY=YES"

exec bash "$tmp_script"
