#!/usr/bin/env bash
# DEV-only, read-only evidence to identify existing shared-photo 422 regressions.
set -Eeuo pipefail
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "DEV_ONLY=FAIL"; exit 1; }
docker inspect df-svc-director >/dev/null
docker exec -i df-svc-director python - <<'PY'
import asyncio, json, os
from uuid import UUID
import asyncpg

WF = UUID("9957cbb9-e337-446d-ad63-0500c4276a9f")
STAGE = UUID("6d52fe52-fcb1-4853-b10c-9db569884305")
async def main():
    dsn=os.environ["DATABASE_URL"].replace("postgresql+asyncpg://","postgresql://")
    conn=await asyncpg.connect(dsn,timeout=10)
    try:
        async with conn.transaction(readonly=True):
            row=await conn.fetchrow("""
              SELECT w.owner_user_id, w.account_id, w.project_id, w.metadata_json AS workflow_meta,
                     s.scene_id,s.metadata_json AS stage_meta,s.state
              FROM public.v3_studio_workflows w JOIN public.v3_studio_stage_runs s
              ON s.workflow_id=w.workflow_id WHERE w.workflow_id=$1 AND s.stage_run_id=$2
            """,WF,STAGE)
            if not row:
                print("WORKFLOW_STAGE=NOT_FOUND");return
            print("STAGE_STATE=",row["state"])
            sm=row["stage_meta"] or {}
            wm=row["workflow_meta"] or {}
            if isinstance(sm,str):sm=json.loads(sm)
            if isinstance(wm,str):wm=json.loads(wm)
            keys=["shared_scene_draft_media_id","shared_scene_media_id"]
            print("STAGE_MEDIA_FIELDS=",{k:bool(sm.get(k)) for k in keys})
            print("WORKFLOW_SOURCE_MODE=",str(wm.get("shared_scene_source_mode") or "unset"))
            print("SPEAKER_TARGET_COUNT=",len(sm.get("speaker_targets") or {}))
            candidates=await conn.fetch("""
              SELECT id,kind,lifecycle_state,account_id,project_id,meta_json
              FROM public.media_assets WHERE user_id=$1
              AND kind IN ('image','source_image','face_image','face_source_image')
              ORDER BY created_at DESC LIMIT 15
            """,row["owner_user_id"])
            print("RECENT_OWNER_IMAGE_ASSETS=",len(candidates))
            selected=set(str(sm[k]) for k in keys if sm.get(k))
            for item in candidates:
                meta=item["meta_json"] or {}
                if isinstance(meta,str):meta=json.loads(meta)
                v=meta.get("shared_scene_validation") or {}
                if isinstance(v,str):
                    try:v=json.loads(v)
                    except ValueError:v={}
                account_ok=item["account_id"] is None or str(item["account_id"])==str(row["account_id"])
                print("MEDIA",{"id":str(item["id"]),"selected_in_stage":str(item["id"]) in selected,
                  "kind":item["kind"],"active":item["lifecycle_state"]=="active",
                  "account_eligible":account_ok,"has_shared_scene_validation":bool(v),
                  "validation_allow":v.get("allow"),"validation_status":v.get("status"),
                  "validation_expected_speakers":v.get("expected_speakers")})
            print("IMPORTANT=Actual failed request media ID may not persist after a rejected PUT.")
            print("NO_DATABASE_WRITES=PASS")
    finally:
        await conn.close()
asyncio.run(main())
PY
