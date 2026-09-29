#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || {
  echo "FAIL: DEV host required"
  exit 1
}

STORY_ID="${STORY_ID:-}"

echo "============================================================"
echo " desifaces DEV — LEGACY SHARED-SCENE SNAPSHOT REPAIR"
echo "============================================================"
echo "story_id=${STORY_ID:-ALL_APPROVED_SHARED_SCENE_WORKFLOWS}"
echo "production=UNTOUCHED"

docker inspect df-svc-director >/dev/null 2>&1 || {
  echo "FAIL: df-svc-director missing"
  exit 1
}

docker exec -i -e STORY_ID="$STORY_ID" df-svc-director python - <<'PY'
from __future__ import annotations

import asyncio
import json
import os

from app.db import open_business_pool, close_pools


def as_dict(value):
    return dict(value or {}) if isinstance(value, dict) else {}


def clean(value):
    return str(value or "").strip()


def normalize_gender(value):
    raw = clean(value).lower()
    if raw in {"female", "f", "woman", "girl"}:
        return "female"
    if raw in {"male", "m", "man", "boy"}:
        return "male"
    return ""


async def main():
    story_id = clean(os.environ.get("STORY_ID"))
    pool = await open_business_pool()
    repaired = 0
    skipped = 0
    failed = 0
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select workflow_id,story_id,account_id,metadata_json
                from public.v3_studio_workflows
                where state::text <> 'canceled'
                  and metadata_json->>'workflow_kind'='shared_scene_conversation_story'
                  and coalesce((metadata_json->>'shared_scene_people_approved')::boolean,false)=true
                  and ($1::uuid is null or story_id=$1::uuid)
                order by updated_at,workflow_id
                """,
                story_id or None,
            )

            for workflow in rows:
                workflow_id = workflow["workflow_id"]
                metadata = as_dict(workflow["metadata_json"])
                snapshot = metadata.get("shared_scene_people_snapshot")
                if isinstance(snapshot, list) and snapshot:
                    print(f"SNAPSHOT_ALREADY_READY workflow_id={workflow_id}")
                    skipped += 1
                    continue

                speaker_rows = await conn.fetch(
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

                speakers = []
                for row in speaker_rows:
                    participant_metadata = as_dict(row["metadata_json"])
                    persona = as_dict(row["persona_json"])
                    explicit = as_dict(participant_metadata.get("explicit_face_constraints"))
                    gender = normalize_gender(
                        explicit.get("gender")
                        or explicit.get("gender_presentation")
                        or persona.get("gender")
                        or persona.get("gender_presentation")
                    )
                    speakers.append({
                        "participant_id": str(row["participant_id"]),
                        "display_name": clean(row["display_name"]) or "Speaker",
                        "gender_presentation": gender or None,
                        "age_presentation": clean(
                            explicit.get("age")
                            or persona.get("age_presentation")
                            or persona.get("age")
                        ) or None,
                        "country_code": clean(
                            explicit.get("country_code")
                            or persona.get("country_code")
                        ).upper() or None,
                        "region_code": clean(
                            explicit.get("region_code")
                            or persona.get("region_code")
                        ) or None,
                    })

                if len(speakers) < 2 or any(not item["gender_presentation"] for item in speakers):
                    print(
                        "SNAPSHOT_REPAIR_BLOCKED "
                        f"workflow_id={workflow_id} "
                        f"speaker_count={len(speakers)} "
                        f"incomplete={[x['display_name'] for x in speakers if not x['gender_presentation']]}"
                    )
                    failed += 1
                    continue

                metadata["shared_scene_people_snapshot"] = sorted(
                    speakers,
                    key=lambda item: item["participant_id"],
                )
                metadata["shared_scene_state_version"] = int(
                    metadata.get("shared_scene_state_version") or 0
                ) + 1

                async with conn.transaction():
                    await conn.execute(
                        """
                        update public.v3_studio_workflows
                        set metadata_json=$2::jsonb,updated_at=now()
                        where workflow_id=$1
                        """,
                        workflow_id,
                        json.dumps(metadata, ensure_ascii=False),
                    )

                print(
                    "SNAPSHOT_REPAIRED "
                    f"workflow_id={workflow_id} "
                    f"story_id={workflow['story_id']} "
                    f"speaker_count={len(speakers)} "
                    f"state_version={metadata['shared_scene_state_version']}"
                )
                repaired += 1

        print(f"REPAIRED={repaired}")
        print(f"ALREADY_READY={skipped}")
        print(f"BLOCKED={failed}")
        if failed:
            raise SystemExit(2)
        print("LEGACY_SHARED_SCENE_SNAPSHOT_REPAIR=PASS")
    finally:
        await close_pools()


asyncio.run(main())
PY
