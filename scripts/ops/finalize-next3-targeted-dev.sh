#!/usr/bin/env bash
set -Eeuo pipefail

WEB_SHA="${1:-ab2a09ce198f8450cd5ecfcf9ebe23b747e9476f}"
EXPECTED_HOST="desifaces-dev"
LIVE_ENV="/home/azureuser/workspace/desifaces-v3/infra/.env"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ "$WEB_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact web SHA required"
[[ -f "$LIVE_ENV" ]] || fail "missing DEV V3 runtime env"

for c in df-v3-svc-director df-v3-svc-director-worker df-v3-svc-fusion-worker df-v3-svc-fusion-extension-stitch-worker df-web-dev; do
  docker inspect "$c" >/dev/null 2>&1 || fail "required DEV container missing: $c"
done

FUSION_ROOT="$(docker inspect df-v3-svc-fusion-worker -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
FUSION_PROJECT="$(docker inspect df-v3-svc-fusion-worker -f '{{index .Config.Labels "com.docker.compose.project"}}')"
[[ -n "$FUSION_ROOT" && -d "$FUSION_ROOT" ]] || fail "Fusion worker source tree missing: $FUSION_ROOT"
[[ -f "$FUSION_ROOT/docker-compose.yml" && -f "$FUSION_ROOT/docker-compose.v3.yml" ]] || fail "Fusion Compose files missing"

echo "============================================================"
echo " NEXT3 — TARGETED FINALIZATION"
echo " host=$(hostname -s)"
echo " fusion_root=$FUSION_ROOT"
echo " fusion_project=$FUSION_PROJECT"
echo " web_sha=$WEB_SHA"
echo " production_touch=NONE"
echo "============================================================"

# Prove already-applied bounded backend fixes before touching the remaining worker.
docker exec -i df-v3-svc-director python - <<'PY'
from app.studio_e2e_routes import fusion_execution
name=type(fusion_execution).__name__
assert name == "ParallelOrphanReconciledParentPricedSceneFusionExecutionService", name
print("DIRECTOR_RECOVERY_RUNTIME=PASS")
PY

docker exec -i df-v3-svc-fusion-extension-stitch-worker python - <<'PY'
import inspect
from app.services import stitch_service
src=inspect.getsource(stitch_service._xfade_pair)
assert "fps=30" in src and "settb=AVTB" in src and "setpts=PTS-STARTPTS" in src
assert "concat" in inspect.getsource(stitch_service.stitch_videos)
print("STITCH_CFR_RUNTIME=PASS")
PY

[[ "$(docker inspect -f '{{.State.Status}}' df-v3-svc-director-worker)" == "running" ]] || fail "Director worker is not running"
[[ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' df-v3-svc-director-worker)" == "unless-stopped" ]] || fail "Director worker is not durable"
echo "DIRECTOR_WORKER_RUNTIME=PASS"

# Preserve only the services this targeted finalization must not change.
UNTOUCHED=(df-v3-svc-director df-v3-svc-director-worker df-v3-svc-fusion-extension-stitch-worker df-v3-svc-face df-v3-svc-audio df-v3-svc-pricing df-v3-svc-fusion desifaces-v3-db desifaces-v3-redis)
declare -A BEFORE
for c in "${UNTOUCHED[@]}"; do
  if docker inspect "$c" >/dev/null 2>&1; then
    BEFORE["$c"]="$(docker inspect -f '{{.State.Status}}|{{.State.StartedAt}}|{{.Image}}|{{.RestartCount}}' "$c")"
  else
    BEFORE["$c"]="ABSENT"
  fi
done

# The prior completion already restored the performance-proven Sync3-parallel
# worker source into this durable tree. Refuse to proceed unless those gates exist.
grep -Rqs "DF_SYNC3_PROVIDER_CONCURRENCY" "$FUSION_ROOT/services/svc-fusion/app/app" || fail "Sync3 provider concurrency implementation missing"
grep -Rqs "DF_SYNC3_CONCURRENCY_WAIT_SECONDS" "$FUSION_ROOT/services/svc-fusion/app/app" || fail "Sync3 wait implementation missing"
echo "FUSION_PARALLEL_SOURCE=PASS"

# Set exactly three runtime values. No other service configuration is changed.
python3 - "$FUSION_ROOT/docker-compose.v3.yml" <<'PY'
from pathlib import Path
import re,sys

path=Path(sys.argv[1])
text=path.read_text()
m=re.search(r"(?ms)^  svc-fusion-worker:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text)
if not m:
    raise SystemExit("svc-fusion-worker override missing")
block=m.group(0)
lines=block.splitlines()
try:
    env_i=next(i for i,line in enumerate(lines) if line.strip()=="environment:")
except StopIteration:
    raise SystemExit("svc-fusion-worker environment missing")

def set_key(lines,key,value):
    pat=re.compile(rf"^\s+{re.escape(key)}:\s*")
    for i,line in enumerate(lines):
        if pat.match(line):
            indent=line[:len(line)-len(line.lstrip())]
            lines[i]=f'{indent}{key}: "{value}"'
            return
    lines.insert(env_i+1,f'      {key}: "{value}"')

set_key(lines,"DF_FUSION_WORKER_CONCURRENCY","8")
set_key(lines,"DF_SYNC3_PROVIDER_CONCURRENCY","1")
set_key(lines,"DF_SYNC3_CONCURRENCY_WAIT_SECONDS","900")
newblock="\n".join(lines)+"\n"
path.write_text(text[:m.start()]+newblock+text[m.end():])
PY

compose(){
  docker compose     --env-file "$LIVE_ENV"     -f "$FUSION_ROOT/docker-compose.yml"     -f "$FUSION_ROOT/docker-compose.v3.yml"     -p "$FUSION_PROJECT" "$@"
}

# Structural Compose validation: no grep/window assumptions.
compose --profile v3-execution config --format json >/tmp/df-next3-targeted-compose.json
python3 - /tmp/df-next3-targeted-compose.json <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
env=cfg["services"]["svc-fusion-worker"].get("environment") or {}
required={
    "DF_FUSION_WORKER_CONCURRENCY":"8",
    "DF_SYNC3_PROVIDER_CONCURRENCY":"1",
    "DF_SYNC3_CONCURRENCY_WAIT_SECONDS":"900",
}
bad={k:(env.get(k),v) for k,v in required.items() if str(env.get(k)) != v}
if bad:
    raise SystemExit("resolved Fusion worker env mismatch: "+repr(bad))
print("FUSION_RUNTIME_CONFIG=PASS")
PY

python3 -m py_compile "$FUSION_ROOT/services/svc-fusion/app/app/workers/fusion_worker.py"
compose --profile v3-execution build svc-fusion-worker
echo "FUSION_WORKER_BUILD=PASS"

OLD_IMAGE="$(docker inspect df-v3-svc-fusion-worker -f '{{.Image}}')"
docker tag "$OLD_IMAGE" "desifaces-v3-svc-fusion-worker:next3-pre-final" >/dev/null 2>&1 || true

docker rm -f df-v3-svc-fusion-worker >/dev/null
compose --profile v3-execution up -d --no-deps --force-recreate svc-fusion-worker

for _ in $(seq 1 30); do
  [[ "$(docker inspect -f '{{.State.Status}}' df-v3-svc-fusion-worker 2>/dev/null || true)" == "running" ]] && break
  sleep 1
done
[[ "$(docker inspect -f '{{.State.Status}}' df-v3-svc-fusion-worker)" == "running" ]] || fail "Fusion worker did not start"

ENV_DUMP="$(docker inspect df-v3-svc-fusion-worker --format '{{range .Config.Env}}{{println .}}{{end}}')"
grep -qx 'DF_FUSION_WORKER_CONCURRENCY=8' <<<"$ENV_DUMP" || fail "worker concurrency != 8"
grep -qx 'DF_SYNC3_PROVIDER_CONCURRENCY=1' <<<"$ENV_DUMP" || fail "Sync3 concurrency != 1"
grep -qx 'DF_SYNC3_CONCURRENCY_WAIT_SECONDS=900' <<<"$ENV_DUMP" || fail "Sync3 wait != 900"
docker exec df-v3-svc-fusion-worker sh -lc 'grep -Rqs "DF_SYNC3_PROVIDER_CONCURRENCY" /app/app && grep -Rqs "DF_SYNC3_CONCURRENCY_WAIT_SECONDS" /app/app' || fail "running worker lost Sync3 gating implementation"
echo "FUSION_WORKER_RUNTIME=PASS"
echo "FUSION_WORKER_CONCURRENCY=8"
echo "SYNC3_PROVIDER_CONCURRENCY=1"

# Confirm the targeted backend change did not move anything else.
for c in "${UNTOUCHED[@]}"; do
  before="${BEFORE[$c]}"
  if [[ "$before" == "ABSENT" ]]; then
    docker inspect "$c" >/dev/null 2>&1 && fail "untouched container unexpectedly appeared: $c"
  else
    after="$(docker inspect -f '{{.State.Status}}|{{.State.StartedAt}}|{{.Image}}|{{.RestartCount}}' "$c")"
    [[ "$after" == "$before" ]] || fail "untouched container changed: $c"
  fi
done
echo "BACKEND_INVARIANCE=PASS"

# Deploy only the repaired DEV web image through its existing candidate/rollback path.
WEB_ROOT=""
for candidate in /home/azureuser/workspace/desifaces_web /home/azureuser/workspace/desifaces-web /home/azureuser/workspace/desifaces_frontend; do
  [[ -d "$candidate/.git" || -f "$candidate/.git" ]] || continue
  remote="$(git -C "$candidate" remote get-url origin 2>/dev/null || true)"
  if [[ "$remote" == *"prasshanthshankar-afk/desifaces_web"* ]]; then
    WEB_ROOT="$candidate"
    break
  fi
done
[[ -n "$WEB_ROOT" ]] || fail "desifaces_web repository not found"

WEB_DEPLOY="/tmp/deploy-next3-web-dev-${WEB_SHA}.sh"
git -C "$WEB_ROOT" fetch --no-tags origin "$WEB_SHA"
git -C "$WEB_ROOT" show "$WEB_SHA:scripts/ops/deploy-next3-web-dev.sh" > "$WEB_DEPLOY"
chmod 700 "$WEB_DEPLOY"
WEB_REPO_ROOT="$WEB_ROOT" bash "$WEB_DEPLOY" "$WEB_SHA"
rm -f "$WEB_DEPLOY"

[[ "$(docker inspect -f '{{.Config.Image}}' df-web-dev)" == "desifaces-web-next3:$WEB_SHA" ]] || fail "wrong active DEV web image"
echo "WEB_STATUS_RECONCILIATION=PASS"

# Read-only proof of the existing failed scene. Do not dispatch from shell.
docker exec -i df-v3-svc-director python - <<'PY'
import os, asyncio, asyncpg
STAGE="69b968f3-793e-433e-bdd9-03ec2afa43e8"

async def main():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        row=await conn.fetchrow("""
          select state from public.v3_studio_stage_runs
          where stage_run_id=$1::uuid
        """,STAGE)
        att=await conn.fetchrow("""
          select attempt_no,state,metadata_json
          from public.v3_studio_stage_attempts
          where stage_run_id=$1::uuid
          order by attempt_no desc limit 1
        """,STAGE)
        print("SCENE_STAGE_STATE="+str(row["state"] if row else "missing"))
        if att:
            children=list((att["metadata_json"] or {}).get("children") or [])
            ready=sum(
                str(x.get("status") or "").lower() in {"succeeded","completed","complete","ready"}
                and bool(x.get("video_url"))
                for x in children
            )
            failed=sum(str(x.get("status") or "").lower() in {"failed","error","canceled","cancelled","blocked"} for x in children)
            print("SCENE_ATTEMPT_NO="+str(att["attempt_no"]))
            print("SCENE_ATTEMPT_STATE="+str(att["state"]))
            print("SCENE_CHILD_TOTAL="+str(len(children)))
            print("SCENE_CHILD_READY="+str(ready))
            print("SCENE_CHILD_FAILED="+str(failed))
    finally:
        await conn.close()
asyncio.run(main())
PY

echo "============================================================"
echo " NEXT3_TARGETED_FINALIZATION=PASS"
echo " GENERAL_FUSION_WORKER_CONCURRENCY=8"
echo " SYNC3_PROVIDER_CONCURRENCY=1"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
