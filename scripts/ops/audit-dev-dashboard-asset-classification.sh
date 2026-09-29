#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: run on desifaces-dev"; exit 2; }

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"

for c in "$DB_CONTAINER" "$PRICING_CONTAINER"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: missing container $c"; exit 2; }
done

DATABASE_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DATABASE_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DATABASE_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo "============================================================"
echo " desifaces DEV — DASHBOARD ASSET CLASSIFICATION AUDIT"
echo " READ ONLY"
echo "============================================================"
echo "production=UNTOUCHED"
echo "db_mutation=NONE"

echo
echo "===== RECENT LIBRARY ROWS ====="
"${PSQL[@]}" -P pager=off -c "
select
  created_at,
  studio,
  asset_type,
  title,
  library_id,
  source_job_id,
  media_asset_id,
  artifact_id,
  reuse_payload_json,
  metadata_json
from public.v_dashboard_asset_library
order by created_at desc nulls last
limit 40;
"

echo
echo "===== FACE ROWS WITH MULTI-PERSON / GROUP SIGNALS ====="
"${PSQL[@]}" -P pager=off -c "
select
  created_at,
  library_id,
  source_job_id,
  media_asset_id,
  title,
  reuse_payload_json,
  metadata_json #> '{job_payload}' as job_payload,
  metadata_json #> '{job_meta}' as job_meta
from public.v_dashboard_asset_library
where lower(coalesce(studio,''))='face'
  and (
    coalesce(metadata_json::text,'') ~* 'multi.person|shared.scene|group.photo|two_people|conversation participant|FACE_MULTI_PERSON'
    or coalesce(reuse_payload_json::text,'') ~* 'multi.person|shared.scene|group.photo|conversation'
  )
order by created_at desc nulls last
limit 30;
"

echo
echo "===== AUDIO ROWS WITH DIRECTOR / STORY SIGNALS ====="
"${PSQL[@]}" -P pager=off -c "
select
  created_at,
  library_id,
  source_job_id,
  title,
  reuse_payload_json,
  metadata_json #> '{payload}' as payload,
  metadata_json #> '{meta}' as meta
from public.v_dashboard_asset_library
where lower(coalesce(studio,''))='audio'
  and (
    coalesce(metadata_json::text,'') ~* 'workflow_id|stage_run_id|dialogue_turn_id|participant_id|shared_scene|ordered_speaker_shots|story'
    or coalesce(reuse_payload_json::text,'') ~* 'workflow_id|dialogue|participant|story'
  )
order by created_at desc nulls last
limit 40;
"

echo
echo "===== VIDEO ROWS WITH CONVERSATION SIGNALS ====="
"${PSQL[@]}" -P pager=off -c "
select
  created_at,
  library_id,
  source_job_id,
  media_asset_id,
  title,
  reuse_payload_json,
  metadata_json
from public.v_dashboard_asset_library
where lower(coalesce(studio,''))='video'
  and (
    coalesce(metadata_json::text,'') ~* 'shared_scene|ordered_speaker_shots|conversation_mode|workflow_id|stage_run_id|story_id|parent_story'
    or coalesce(reuse_payload_json::text,'') ~* 'shared_scene|ordered_speaker_shots|conversation_mode|workflow_id|story_id'
  )
order by created_at desc nulls last
limit 40;
"

echo
echo "===== CANONICAL MULTI-PERSON WORKFLOWS ====="
"${PSQL[@]}" -P pager=off -c "
select
  w.created_at,
  w.updated_at,
  w.workflow_id,
  w.story_id,
  w.state,
  w.current_stage,
  w.final_media_id,
  w.metadata_json->>'shared_scene_source_mode' as shared_scene_source_mode,
  w.metadata_json->>'shared_scene_people_approved' as people_approved,
  jsonb_array_length(
    case
      when jsonb_typeof(w.metadata_json->'shared_scene_people_snapshot')='array'
      then w.metadata_json->'shared_scene_people_snapshot'
      else '[]'::jsonb
    end
  ) as speaker_count
from public.v3_studio_workflows w
where
  coalesce(w.metadata_json::text,'') ~* 'shared_scene|shared_scene_people'
  or exists (
    select 1
    from public.v3_studio_stage_runs s
    where s.workflow_id=w.workflow_id
      and coalesce(s.metadata_json::text,'') ~* 'shared_scene|conversation_mode'
  )
order by w.updated_at desc
limit 20;
"

echo
echo "============================================================"
echo "DASHBOARD_ASSET_CLASSIFICATION_AUDIT=PASS"
echo "db_mutation=NONE"
echo "production=UNTOUCHED"
echo "============================================================"
