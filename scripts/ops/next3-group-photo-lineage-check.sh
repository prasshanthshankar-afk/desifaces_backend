#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || {
  echo "FAIL: DEV host required"
  exit 1
}

WF="53e00bba-c4e9-437a-9d05-7ab5aa975d6d"
STAGE="0d554eda-f589-4778-9200-c7ecc1b333d3"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/tmp/next3-group-photo-lineage-${STAMP}.txt"

{
  echo "============================================================"
  echo " NEXT3 — GROUP PHOTO MEDIA LINEAGE"
  echo "============================================================"
  echo "workflow_id=$WF"
  echo "stage_run_id=$STAGE"

  echo
  echo "===== WORKFLOW / STAGE OWNERSHIP ====="
  docker exec -i desifaces-db sh -lc '
    psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -F "|" -At
  ' <<SQL
select
  'workflow',
  workflow_id::text,
  coalesce(account_id::text,'NULL'),
  coalesce(project_id::text,'NULL'),
  coalesce(owner_user_id::text,'NULL')
from public.v3_studio_workflows
where workflow_id='$WF'::uuid;

select
  'stage',
  stage_run_id::text,
  workflow_id::text,
  coalesce(scene_id::text,'NULL'),
  coalesce(state::text,'NULL')
from public.v3_studio_stage_runs
where stage_run_id='$STAGE'::uuid;
SQL

  echo
  echo "===== RECENT VALIDATED GROUP-PHOTO MEDIA ====="
  docker exec -i desifaces-db sh -lc '
    psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -F "|" -At
  ' <<'SQL'
select
  id::text,
  coalesce(user_id::text,'NULL'),
  coalesce(account_id::text,'NULL'),
  coalesce(project_id::text,'NULL'),
  coalesce(kind,'NULL'),
  coalesce(lifecycle_state,'NULL'),
  coalesce(width::text,'NULL'),
  coalesce(height::text,'NULL'),
  coalesce(meta_json->'shared_scene_validation'->>'status','NULL'),
  coalesce(meta_json->'shared_scene_validation'->>'allow','NULL'),
  coalesce(meta_json->'shared_scene_validation'->>'expected_speakers','NULL'),
  to_char(updated_at,'YYYY-MM-DD"T"HH24:MI:SSOF')
from public.media_assets
where meta_json ? 'shared_scene_validation'
order by updated_at desc
limit 10;
SQL

  echo
  echo "===== RECENT FACE OUTPUT MEDIA ====="
  docker exec -i desifaces-db sh -lc '
    psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -F "|" -At
  ' <<'SQL'
select
  fjo.job_id::text,
  coalesce(fjo.output_asset_id::text,'NULL'),
  coalesce(ma.user_id::text,'NULL'),
  coalesce(ma.account_id::text,'NULL'),
  coalesce(ma.project_id::text,'NULL'),
  coalesce(ma.kind,'NULL'),
  coalesce(ma.lifecycle_state,'NULL'),
  coalesce(ma.width::text,'NULL'),
  coalesce(ma.height::text,'NULL'),
  to_char(fjo.created_at,'YYYY-MM-DD"T"HH24:MI:SSOF')
from public.face_job_outputs fjo
left join public.media_assets ma on ma.id=fjo.output_asset_id
order by fjo.created_at desc
limit 10;
SQL

  echo
  echo "===== DIRECTOR OWNERSHIP PREDICATE ====="
  cat <<'TXT'
Required simultaneously:
  media.id = selected shared_scene_media_id
  media.user_id = workflow owner user
  media.account_id = workflow account
  media.project_id IS NULL OR media.project_id = workflow project
  media.kind IN (image, source_image, face_image, face_source_image)
  media.lifecycle_state = active
TXT

  echo
  echo "============================================================"
  echo " NEXT3_GROUP_PHOTO_LINEAGE_CAPTURE=PASS"
  echo "============================================================"
} > "$OUT" 2>&1

echo "TRACE_FILE=$OUT"
echo "----- RESULT -----"
tail -n 120 "$OUT"
