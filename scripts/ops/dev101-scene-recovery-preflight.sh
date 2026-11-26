#!/usr/bin/env bash
# Strict read-only incident preflight. NEVER reserves, commits, releases or updates.
set -euo pipefail
STAGE_ID="${STAGE_ID:-0a3e18b8-8ee8-4d85-8d52-47372ecda50b}"
ORIGINAL_RESERVATION_ID="${ORIGINAL_RESERVATION_ID:-77122b8a-e65c-4267-b4ca-ee2ac5238f66}"
MEDIA_ID="${MEDIA_ID:-5a9658b4-8ad2-4332-ab38-7b5a71324fb7}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"

for id in "$STAGE_ID" "$ORIGINAL_RESERVATION_ID" "$MEDIA_ID"; do
  [[ "$id" =~ ^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$ ]] || {
    echo "BLOCK: invalid UUID input" >&2; exit 2;
  }
done
command -v docker >/dev/null || { echo 'BLOCK: docker missing' >&2; exit 2; }
query() {
  docker exec -i "$DB_CONTAINER" sh -c \
    'psql -X -v ON_ERROR_STOP=1 -qAt -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    -v stage_id="$STAGE_ID" -v reservation_id="$ORIGINAL_RESERVATION_ID" -v media_id="$MEDIA_ID" <<'SQL'
BEGIN READ ONLY;
WITH incident AS (
  SELECT s.stage_run_id,s.workflow_id,s.state AS stage_state,
         s.metadata_json #>> '{fusion_parent_pricing,state}' AS pricing_state,
         s.metadata_json #>> '{fusion_parent_pricing,reservation_id}' AS stage_reservation_id,
         w.owner_user_id,w.account_id,w.project_id
  FROM public.v3_studio_stage_runs s
  JOIN public.v3_studio_workflows w ON w.workflow_id=s.workflow_id
  WHERE s.stage_run_id=:'stage_id'::uuid AND s.stage_type='fusion'
),
attempt AS (
  SELECT a.stage_run_id,a.state AS attempt_state,
         a.metadata_json->>'stitched_media_id' AS stitched_media_id,
         jsonb_array_length(
           CASE WHEN jsonb_typeof(a.metadata_json->'children')='array'
             THEN a.metadata_json->'children' ELSE '[]'::jsonb END
         ) AS child_count,
         (SELECT count(*) FROM jsonb_array_elements(
           CASE WHEN jsonb_typeof(a.metadata_json->'children')='array'
             THEN a.metadata_json->'children' ELSE '[]'::jsonb END
         ) c WHERE c->>'status'='succeeded' AND nullif(c->>'video_url','') IS NOT NULL)
         AS successful_children
  FROM public.v3_studio_stage_attempts a
  WHERE a.stage_run_id=:'stage_id'::uuid
  ORDER BY a.attempt_no DESC LIMIT 1
),
reservation AS (
  SELECT r.id,r.user_id,r.job_ref,r.status,r.reserved_credits,
         r.funding_summary_json->>'released_reason' AS reason
  FROM public.pricing_credit_reservations r
  WHERE r.id=:'reservation_id'::uuid
),
debits AS (
  SELECT count(*) AS debit_count FROM public.pricing_credit_ledger_events e
  JOIN reservation r ON r.user_id=e.user_id
  WHERE e.credits_delta<0 AND e.idempotency_key LIKE
   ('consume:svc-fusion-extension:v3-scene:' || :'stage_id' || ':commit:%')
)
SELECT
  CASE
    WHEN i.stage_run_id IS NULL THEN 'BLOCK:stage_missing'
    WHEN i.stage_state <> 'generating' THEN 'BLOCK:stage_state'
    WHEN i.pricing_state <> 'commit_pending' THEN 'BLOCK:pricing_state'
    WHEN i.stage_reservation_id <> :'reservation_id' THEN 'BLOCK:original_reservation_pointer'
    WHEN r.id IS NULL OR r.user_id<>i.owner_user_id OR r.job_ref<>:'stage_id' THEN 'BLOCK:reservation_lineage'
    WHEN r.status<>'released' OR r.reason<>'expired' OR r.reserved_credits<>918 THEN 'BLOCK:original_reservation_state'
    WHEN a.stage_run_id IS NULL OR a.stitched_media_id<>:'media_id' OR a.child_count<>7 OR a.successful_children<>7 THEN 'BLOCK:child_or_media_lineage'
    WHEN m.id IS NULL OR m.lifecycle_state<>'active' OR m.account_id<>i.account_id OR m.project_id<>i.project_id OR m.user_id<>i.owner_user_id THEN 'BLOCK:media_ownership'
    WHEN d.debit_count<>0 THEN 'BLOCK:prior_scene_debit'
    WHEN acc.user_id IS NULL OR acc.balance_credits-acc.reserved_credits<918 THEN 'BLOCK:insufficient_credit'
    ELSE 'READ_ONLY_PREFLIGHT_PASS'
  END AS preflight_result,
  coalesce(i.workflow_id::text,'') AS workflow_id,
  coalesce(r.status,'') AS reservation_status,
  coalesce(a.successful_children,0) AS completed_clips,
  coalesce(d.debit_count,0) AS prior_debits,
  coalesce(acc.balance_credits-acc.reserved_credits,0) AS available_credits
FROM (SELECT 1 AS singleton) root
LEFT JOIN incident i ON true
LEFT JOIN attempt a ON a.stage_run_id=i.stage_run_id
LEFT JOIN reservation r ON true
LEFT JOIN public.media_assets m ON m.id=:'media_id'::uuid
LEFT JOIN public.pricing_credit_accounts acc ON acc.user_id=i.owner_user_id
CROSS JOIN debits d;
COMMIT;
SQL
}
echo "===== DEV101 EXPIRED SCENE RECOVERY — READ ONLY ====="
result="$(query)"
echo "$result"
echo "$result" | grep -q '^READ_ONLY_PREFLIGHT_PASS|' || {
  echo "BLOCK: incident preflight did not pass. No mutation attempted." >&2; exit 1;
}
echo 'Preflight only: do NOT enable recovery until full integration certification.'
