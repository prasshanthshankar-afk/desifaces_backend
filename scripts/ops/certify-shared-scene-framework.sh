#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || {
  echo "FAIL: DEV host required"
  exit 1
}

STORY_ID="${STORY_ID:-88cedbfe-9959-587e-8948-d0853e9bc19c}"
EXPECTED_WEB_SHA="${EXPECTED_WEB_SHA:-7ef1a0b7765fed8ee3c72161d88ee8534aac1ac1}"

echo "============================================================"
echo " desifaces DEV — SHARED-SCENE FRAMEWORK CERTIFICATION"
echo "============================================================"
echo "story_id=$STORY_ID"
echo "production=UNTOUCHED"

echo
echo "===== 1. RUNTIME PROVENANCE ====="
WEB_IMAGE="$(docker inspect df-web-dev --format '{{.Image}}')"
WEB_REV="$(docker image inspect "$WEB_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true)"
DIRECTOR_IMAGE="$(docker inspect df-svc-director --format '{{.Image}}')"

echo "web_image=$WEB_IMAGE"
echo "web_revision=${WEB_REV:-UNKNOWN}"
echo "director_image=$DIRECTOR_IMAGE"

[[ "$WEB_REV" == "$EXPECTED_WEB_SHA" ]] || {
  echo "FAIL: web revision mismatch expected=$EXPECTED_WEB_SHA actual=${WEB_REV:-UNKNOWN}"
  exit 1
}
echo "WEB_CANONICAL_STATE_CLIENT=PASS"

docker exec -i df-svc-director python - <<'PY'
from app.main import app
paths={getattr(r,"path","") for r in app.routes}
required={
 "/api/director/studio-workflows/{workflow_id}/shared-scene-state",
 "/api/director/studio-workflows/{workflow_id}/shared-scene-source",
 "/api/director/studio-workflows/{workflow_id}/shared-scene-group-photo-spec",
 "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/shared-scene-draft",
}
missing=sorted(required-paths)
assert not missing, missing
print("DIRECTOR_CANONICAL_STATE_ROUTES=PASS")
PY

echo
echo "===== 2. DB + BACKEND CANONICAL STATE ====="
docker exec -i -e STORY_ID="$STORY_ID" df-svc-director python - <<'PY'
from __future__ import annotations

import asyncio
import json
import os

from app.db import open_business_pool, close_pools
from app.shared_scene_state_routes import load_shared_scene_state


def clean(v):
    return str(v or "").strip()


def norm_profile(item):
    return {
        "participant_id": clean(item.get("participant_id")),
        "display_name": clean(item.get("display_name")),
        "gender_presentation": clean(item.get("gender_presentation")).lower() or None,
        "age_presentation": clean(item.get("age_presentation")) or None,
        "country_code": clean(item.get("country_code")).upper() or None,
        "region_code": clean(item.get("region_code")) or None,
    }


async def main():
    story_id = os.environ["STORY_ID"]
    pool = await open_business_pool()
    try:
        async with pool.acquire() as conn:
            workflow = await conn.fetchrow(
                """
                select workflow_id,account_id,metadata_json,state,current_stage
                from public.v3_studio_workflows
                where story_id=$1::uuid
                  and state::text <> 'canceled'
                  and metadata_json->>'workflow_kind'='shared_scene_conversation_story'
                order by updated_at desc,created_at desc
                limit 1
                """,
                story_id,
            )
            assert workflow, f"shared-scene workflow not found for story {story_id}"

            workflow_id = workflow["workflow_id"]
            account_id = workflow["account_id"]
            state = await load_shared_scene_state(
                conn,
                workflow_id=workflow_id,
                account_id=account_id,
            )

            rows = await conn.fetch(
                """
                select distinct on (p.participant_id)
                       p.participant_id,p.display_name,p.metadata_json,p.persona_json
                from public.v3_studio_stage_runs s
                join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
                join public.v3_participants p on p.participant_id=dt.speaker_participant_id
                where s.workflow_id=$1
                  and s.stage_type='audio'
                  and s.scope_type='dialogue_turn'
                  and dt.speaker_participant_id is not null
                order by p.participant_id,s.created_at
                """,
                workflow_id,
            )

            live = []
            for row in rows:
                metadata = dict(row["metadata_json"] or {})
                persona = dict(row["persona_json"] or {})
                explicit = dict(metadata.get("explicit_face_constraints") or {})
                live.append(norm_profile({
                    "participant_id": row["participant_id"],
                    "display_name": row["display_name"],
                    "gender_presentation": (
                        explicit.get("gender")
                        or explicit.get("gender_presentation")
                        or persona.get("gender_presentation")
                        or persona.get("gender")
                    ),
                    "age_presentation": (
                        explicit.get("age")
                        or persona.get("age_presentation")
                        or persona.get("age")
                    ),
                    "country_code": (
                        explicit.get("country_code")
                        or persona.get("country_code")
                    ),
                    "region_code": (
                        explicit.get("region_code")
                        or persona.get("region_code")
                    ),
                }))

            projected = [norm_profile(x) for x in state["people"]["speakers"]]
            live_sorted = sorted(live, key=lambda x: x["participant_id"])
            projected_sorted = sorted(projected, key=lambda x: x["participant_id"])
            assert live_sorted == projected_sorted, {
                "live_db_profiles": live_sorted,
                "canonical_state_profiles": projected_sorted,
            }
            print("DB_TO_CANONICAL_SPEAKERS=PASS")

            snapshot = state["people"].get("approved_snapshot")
            if state["people"]["approved"]:
                if not snapshot:
                    print("APPROVED_SPEAKER_SNAPSHOT=LEGACY_MISSING")
                else:
                    snapshot_sorted = sorted(
                        (norm_profile(x) for x in snapshot),
                        key=lambda x: x["participant_id"],
                    )
                    assert snapshot_sorted == projected_sorted, {
                        "approved_snapshot": snapshot_sorted,
                        "canonical_state_profiles": projected_sorted,
                    }
                    print("APPROVED_SPEAKER_SNAPSHOT=PASS")
            else:
                print("APPROVED_SPEAKER_SNAPSHOT=NOT_YET_APPLICABLE")

            generation_input = state["group_photo"].get("generation_input")
            if generation_input:
                subjects = list(generation_input.get("subjects") or [])
                approved = snapshot or state["people"]["speakers"]
                expected_genders = [
                    clean(x.get("gender_presentation")).lower()
                    for x in approved
                    if clean(x.get("gender_presentation"))
                ]
                actual_genders = [
                    clean(x.get("gender")).lower()
                    for x in subjects
                    if clean(x.get("gender"))
                ]
                assert actual_genders == expected_genders, {
                    "expected_genders": expected_genders,
                    "generation_input_genders": actual_genders,
                }
                prompt = clean(generation_input.get("prompt"))
                assert "CURRENT APPROVED SPEAKER PROFILES" in prompt
                assert all(
                    clean(x.get("display_name")) in prompt
                    for x in approved
                    if clean(x.get("display_name"))
                )
                print("CANONICAL_FACE_GENERATION_INPUT=PASS")
            else:
                print("CANONICAL_FACE_GENERATION_INPUT=NOT_YET_APPLICABLE")

            print("workflow_id=" + str(workflow_id))
            print("state_version=" + str(state.get("state_version")))
            print("phase=" + clean(state.get("phase")))
            print("next_action=" + clean(state.get("next_action")))
            print("allowed_actions=" + ",".join(state.get("allowed_actions") or []))
            print("source_mode=" + clean(state["group_photo"].get("source_mode") or "<not-selected>"))
            print("draft_media_id=" + clean(state["group_photo"].get("draft_media_id") or "<none>"))
            print("approved_media_id=" + clean(state["group_photo"].get("approved_media_id") or "<none>"))
            print("mapped=" + str(state["group_photo"].get("mapped_count")) + "/" + str(state["group_photo"].get("required_mapped_count")))

            print("CANONICAL_STATE_JSON_BEGIN")
            print(json.dumps(state, indent=2, default=str))
            print("CANONICAL_STATE_JSON_END")
    finally:
        await close_pools()


asyncio.run(main())
PY

echo
echo "===== 3. RUNTIME NAMING ====="
BAD_C="$(docker ps -a --format '{{.Names}}' | grep -Ei 'v3|next3' || true)"
BAD_N="$(docker network ls --format '{{.Name}}' | grep -Ei 'v3|next3' || true)"
[[ -z "$BAD_C" ]] || { echo "$BAD_C"; echo "FAIL: versioned container name detected"; exit 1; }
[[ -z "$BAD_N" ]] || { echo "$BAD_N"; echo "FAIL: versioned network name detected"; exit 1; }
echo "VERSION_NEUTRAL_RUNTIME=PASS"

echo
echo "============================================================"
echo " SHARED_SCENE_FRAMEWORK_CERTIFICATION=PASS"
echo " production=UNTOUCHED"
echo "============================================================"
