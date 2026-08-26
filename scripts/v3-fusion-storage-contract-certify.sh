#!/usr/bin/env bash
set -euo pipefail

readonly WORKFLOW_ID="06c5d43e-7bbc-4cb4-aef3-9df36886da3b"
readonly FUSION_STAGE_ID="4038a526-308a-49ba-959a-7e40f512c3b3"
readonly EXPECTED_CHILDREN="28"
POSTGRES_DB="${POSTGRES_DB:-desifaces_v3}"
POSTGRES_USER="${POSTGRES_USER:-desifaces_v3_admin}"

compose() { bash scripts/v3-compose.sh "$@"; }
psql_scalar() {
  compose exec -T desifaces-db psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
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

[[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || fail "wrong branch"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || fail "refusing non-V3 DB"

stage_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
attempt_count="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
latest_attempt_no="$(psql_scalar "select attempt_no from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
latest_attempt_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
children="$(psql_scalar "$(child_jobs_sql)")"
succeeded_children="$(psql_scalar "$(child_jobs_sql) and j.status='succeeded'")"
active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
consume_events="$(psql_scalar "select count(*) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
credit_delta="$(psql_scalar "select coalesce(sum(l.credits_delta),0) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"

artifact_container_count="$(psql_scalar "
select count(distinct split_part(split_part(c->>'video_url', '.blob.core.windows.net/', 2), '/', 1))
from public.v3_studio_stage_attempts a
cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c
where a.stage_run_id='${FUSION_STAGE_ID}'::uuid
  and coalesce(c->>'video_url','') like 'https://%.blob.core.windows.net/%';")"
artifact_container="$(psql_scalar "
select min(split_part(split_part(c->>'video_url', '.blob.core.windows.net/', 2), '/', 1))
from public.v3_studio_stage_attempts a
cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c
where a.stage_run_id='${FUSION_STAGE_ID}'::uuid
  and coalesce(c->>'video_url','') like 'https://%.blob.core.windows.net/%';")"

[[ "$stage_state" == "failed" ]] || fail "Fusion stage must remain failed during storage certification: $stage_state"
[[ "$latest_attempt_state" == "failed" ]] || fail "latest attempt must remain failed: $latest_attempt_state"
[[ "$children" == "$EXPECTED_CHILDREN" ]] || fail "provider child count changed: $children"
[[ "$succeeded_children" == "$EXPECTED_CHILDREN" ]] || fail "provider child success count changed: $succeeded_children"
[[ "$active_jobs" == "0" ]] || fail "active generation/pricing jobs exist: $active_jobs"
[[ "$consume_events" == "0" ]] || fail "parent consume event already exists: $consume_events"
python3 - "$credit_delta" <<'PY'
from decimal import Decimal
import sys
assert Decimal(sys.argv[1]) == Decimal('0'), sys.argv[1]
PY
[[ "$artifact_container_count" == "1" ]] || fail "expected one preserved-artifact Azure container; found $artifact_container_count"
[[ "$artifact_container" == "video-output" ]] || fail "canonical preserved-artifact container is not video-output: $artifact_container"

export DF_V3_FUSION_OUTPUT_CONTAINER="$artifact_container"

echo "STORAGE_CERT_PRECONDITION_GATE=PASS"
echo "PRESERVED_ARTIFACT_CONTAINER=$artifact_container"
echo "PROVIDER_CHILDREN_FROZEN=$children/$succeeded_children"
echo "BASELINE_ATTEMPT_COUNT=$attempt_count"
echo "BASELINE_LATEST_ATTEMPT_NO=$latest_attempt_no"
echo "PARENT_CONSUME_EVENTS=$consume_events"
echo "PARENT_CREDIT_DELTA=$credit_delta"

# Prove the resolved Compose contract before changing running containers.
compose --profile v3-execution config > /tmp/v3-fusion-storage-contract.compose.yml
python3 - /tmp/v3-fusion-storage-contract.compose.yml "$artifact_container" <<'PY'
import sys, yaml
path, expected = sys.argv[1:]
config = yaml.safe_load(open(path, encoding='utf-8'))
for service in (
    'svc-fusion',
    'svc-fusion-worker',
    'svc-fusion-extension',
    'svc-fusion-extension-worker',
    'svc-fusion-extension-stitch-worker',
):
    env = (config['services'][service].get('environment') or {})
    actual = env.get('AZURE_VIDEO_OUTPUT_CONTAINER')
    final = env.get('AZURE_FINAL_VIDEO_CONTAINER')
    assert actual == expected, (service, 'AZURE_VIDEO_OUTPUT_CONTAINER', actual, expected)
    assert final == expected, (service, 'AZURE_FINAL_VIDEO_CONTAINER', final, expected)
    print(f"COMPOSE_STORAGE_CONTRACT_{service}=PASS:{expected}")
PY

echo "COMPOSE_STORAGE_CONTRACT_GATE=PASS"

# Recreate only non-provider-render control plane needed for storage/stitch.
# The provider Fusion worker is deliberately NOT restarted.
compose --profile v3-execution up -d --no-deps --force-recreate \
  svc-fusion svc-fusion-extension svc-fusion-extension-stitch-worker

for name in df-v3-svc-fusion df-v3-svc-fusion-extension df-v3-svc-fusion-extension-stitch-worker; do
  running="$(docker inspect -f '{{.State.Running}}' "$name")"
  [[ "$running" == "true" ]] || fail "$name is not running"
  video="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$name" | sed -n 's/^AZURE_VIDEO_OUTPUT_CONTAINER=//p' | tail -1)"
  final="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$name" | sed -n 's/^AZURE_FINAL_VIDEO_CONTAINER=//p' | tail -1)"
  [[ "$video" == "$artifact_container" ]] || fail "$name video container mismatch: $video"
  [[ "$final" == "$artifact_container" ]] || fail "$name final container mismatch: $final"
  echo "LIVE_STORAGE_CONTRACT_${name}=PASS:$video"
done

echo "LIVE_STORAGE_CONTRACT_GATE=PASS"

# Prove the exact running stitch worker can resolve, write to, and delete from the
# canonical Azure container. This is a tiny non-media probe and has no pricing path.
docker exec -i \
  -e DF_EXPECTED_CONTAINER="$artifact_container" \
  df-v3-svc-fusion-extension-stitch-worker \
  python - <<'PY'
import os, uuid
from azure.storage.blob import BlobServiceClient
from app.config import settings

expected = os.environ['DF_EXPECTED_CONTAINER']
video = str(settings.AZURE_VIDEO_OUTPUT_CONTAINER or '').strip()
final = str(settings.AZURE_FINAL_VIDEO_CONTAINER or '').strip()
assert video == expected, (video, expected)
assert final == expected, (final, expected)
service = BlobServiceClient.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)
container = service.get_container_client(expected)
props = container.get_container_properties()
name = f"_health/v3-fusion-storage-contract-{uuid.uuid4().hex}.txt"
blob = container.get_blob_client(name)
blob.upload_blob(b"desifaces-v3-storage-contract\n", overwrite=True)
assert blob.exists()
blob.delete_blob()
print(f"AZURE_STORAGE_ACCOUNT={service.account_name}")
print(f"AZURE_CONTAINER={expected}")
print(f"AZURE_CONTAINER_ETAG={props.etag}")
print("AZURE_CONTAINER_RESOLVE=PASS")
print("AZURE_CONTAINER_WRITE_DELETE=PASS")
PY

# Final invariant check: certification itself must not create an attempt, provider
# job, output, review item, or pricing consume event.
final_attempt_count="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
final_latest_attempt_no="$(psql_scalar "select attempt_no from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
final_children="$(psql_scalar "$(child_jobs_sql)")"
final_consume_events="$(psql_scalar "select count(*) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
final_credit_delta="$(psql_scalar "select coalesce(sum(l.credits_delta),0) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"

[[ "$final_attempt_count" == "$attempt_count" ]] || fail "storage certification created a Fusion attempt"
[[ "$final_latest_attempt_no" == "$latest_attempt_no" ]] || fail "latest attempt changed during storage certification"
[[ "$final_children" == "$children" ]] || fail "provider child count changed during storage certification"
[[ "$final_consume_events" == "$consume_events" ]] || fail "pricing consume events changed during storage certification"
[[ "$final_credit_delta" == "$credit_delta" ]] || fail "credit delta changed during storage certification"

echo "NO_FUSION_RETRY_CREATED=PASS"
echo "NO_PROVIDER_RERENDER=PASS"
echo "NO_PRICING_CONSUME=PASS"
echo "CONTAINER_NOT_FOUND_ROOT_CAUSE_RESOLVED=PASS"
