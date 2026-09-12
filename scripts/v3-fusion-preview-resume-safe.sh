#!/usr/bin/env bash
set -euo pipefail

# Safety wrapper for the canonical V3 measured-performance benchmark.
# Caller shell WORKFLOW_ID/FUSION_STAGE_ID values are intentionally ignored so
# stale exports from another Story can never redirect this benchmark.

readonly BENCHMARK_WORKFLOW_ID="06c5d43e-7bbc-4cb4-aef3-9df36886da3b"
readonly EXPECTED_FUSION_STAGE_ID="4038a526-308a-49ba-959a-7e40f512c3b3"
POSTGRES_DB="${POSTGRES_DB:-desifaces_v3}"
POSTGRES_USER="${POSTGRES_USER:-desifaces_v3_admin}"

compose() { bash scripts/v3-compose.sh "$@"; }
psql_scalar() {
  compose exec -T desifaces-db \
    psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}
fail() { echo "ERROR: $*" >&2; exit 1; }

[[ -f scripts/v3-compose.sh ]] || fail "run from ~/workspace/desifaces-v3"
[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || fail "wrong branch"

fusion_stage_count="$(psql_scalar "
select count(*)
from public.v3_studio_stage_runs
where workflow_id='${BENCHMARK_WORKFLOW_ID}'::uuid
  and stage_type='fusion';")"
[[ "$fusion_stage_count" == "1" ]] || fail "benchmark workflow must have exactly one Fusion stage; found $fusion_stage_count"

resolved_fusion_stage_id="$(psql_scalar "
select stage_run_id::text
from public.v3_studio_stage_runs
where workflow_id='${BENCHMARK_WORKFLOW_ID}'::uuid
  and stage_type='fusion'
order by created_at, stage_run_id
limit 1;")"

[[ -n "$resolved_fusion_stage_id" ]] || fail "benchmark Fusion stage could not be resolved"
[[ "$resolved_fusion_stage_id" == "$EXPECTED_FUSION_STAGE_ID" ]] || \
  fail "benchmark Fusion stage mismatch: resolved=$resolved_fusion_stage_id expected=$EXPECTED_FUSION_STAGE_ID"

fusion_state="$(psql_scalar "
select state
from public.v3_studio_stage_runs
where workflow_id='${BENCHMARK_WORKFLOW_ID}'::uuid
  and stage_run_id='${resolved_fusion_stage_id}'::uuid
  and stage_type='fusion';")"

fusion_attempts="$(psql_scalar "
select count(*)
from public.v3_studio_stage_attempts a
join public.v3_studio_stage_runs s on s.stage_run_id=a.stage_run_id
where s.workflow_id='${BENCHMARK_WORKFLOW_ID}'::uuid
  and s.stage_type='fusion'
  and s.stage_run_id='${resolved_fusion_stage_id}'::uuid;")"

printf 'BENCHMARK_WORKFLOW_ID=%s\n' "$BENCHMARK_WORKFLOW_ID"
printf 'RESOLVED_FUSION_STAGE_ID=%s\n' "$resolved_fusion_stage_id"
printf 'FUSION_STATE=%s\n' "$fusion_state"
printf 'FUSION_ATTEMPTS=%s\n' "$fusion_attempts"

[[ "$fusion_state" == "pending" ]] || fail "benchmark Fusion stage is not pending"
[[ "$fusion_attempts" == "0" ]] || fail "benchmark Fusion attempts already exist"
echo "CROSS_WORKFLOW_STAGE_GUARD=PASS"

# Explicitly overwrite any stale caller exports for the implementation script.
WORKFLOW_ID="$BENCHMARK_WORKFLOW_ID" \
FUSION_STAGE_ID="$resolved_fusion_stage_id" \
bash scripts/v3-fusion-preview-resume-after-audio-hitl.sh
