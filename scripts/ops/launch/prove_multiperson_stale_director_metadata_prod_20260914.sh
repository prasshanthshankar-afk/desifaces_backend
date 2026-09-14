#!/usr/bin/env bash
set -Eeuo pipefail

JOB="964f8fb3-4a2c-4df6-968a-34b283d9ccd0"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"

DIRECTOR="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
[[ -n "$DIRECTOR" ]] || fail "Director container missing"

echo "============================================================"
echo " desifaces — READ-ONLY STALE DIRECTOR METADATA PROOF"
echo " job_id=$JOB"
echo " mutation=NONE"
echo "============================================================"

docker exec "$DIRECTOR" python - "$JOB" <<'PY'
import os, asyncio, asyncpg, json, sys

JOB=sys.argv[1]

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(
            """
            select
                a.attempt_id,
                a.stage_run_id,
                a.attempt_no,
                a.state as attempt_state,
                a.error_code,
                a.error_message,
                a.metadata_json,
                s.state as stage_state,
                s.workflow_id,
                s.scene_id
            from public.v3_studio_stage_attempts a
            join public.v3_studio_stage_runs s
              on s.stage_run_id=a.stage_run_id
            where a.metadata_json::text like $1
            order by a.attempt_no desc
            """,
            "%"+JOB+"%",
        )

        print("MATCH_COUNT="+str(len(rows)))

        for r in rows:
            meta=dict(r["metadata_json"] or {})
            matches=[]
            for child in list(meta.get("children") or []):
                if str(child.get("fusion_job_id") or "") == JOB:
                    matches.append(child)

            print("ATTEMPT_ID="+str(r["attempt_id"]))
            print("STAGE_RUN_ID="+str(r["stage_run_id"]))
            print("WORKFLOW_ID="+str(r["workflow_id"]))
            print("SCENE_ID="+str(r["scene_id"]))
            print("ATTEMPT_NO="+str(r["attempt_no"]))
            print("ATTEMPT_STATE="+str(r["attempt_state"]))
            print("STAGE_STATE="+str(r["stage_state"]))
            print("ERROR_CODE="+str(r["error_code"]))
            print("CHILD="+json.dumps(matches, default=str)[:6000])
            print("---")
    finally:
        await conn.close()

asyncio.run(main())
PY

echo "============================================================"
echo "STALE_METADATA_PROOF=PASS"
echo "MUTATION=NONE"
echo "============================================================"
