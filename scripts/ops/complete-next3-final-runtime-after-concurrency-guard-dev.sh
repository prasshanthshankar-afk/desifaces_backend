#!/usr/bin/env bash
set -Eeuo pipefail

WEB_SHA="${1:-ab2a09ce198f8450cd5ecfcf9ebe23b747e9476f}"
EXPECTED_HOST="desifaces-dev"
LIVE_ENV="/home/azureuser/workspace/desifaces-v3/infra/.env"
OLD_FUSION_IMAGE="sha256:7f49847a65e585cdae1fbb5241a07ad5fef3ef6d239fb3bfa2cfeafcb9b5e987"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ "$WEB_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact web SHA required"
[[ -f "$LIVE_ENV" ]] || fail "missing DEV V3 runtime env"
docker image inspect "$OLD_FUSION_IMAGE" >/dev/null 2>&1 ||   fail "pre-fix performance-proven Fusion worker image is no longer available"

for c in   df-v3-svc-director   df-v3-svc-director-worker   df-v3-svc-fusion-worker   df-v3-svc-fusion-extension-stitch-worker
do
  docker inspect "$c" >/dev/null 2>&1 || fail "required target container missing: $c"
done

FUSION_ROOT="$(docker inspect df-v3-svc-fusion-worker -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
FUSION_PROJECT="$(docker inspect df-v3-svc-fusion-worker -f '{{index .Config.Labels "com.docker.compose.project"}}')"
[[ -n "$FUSION_ROOT" && -d "$FUSION_ROOT" ]] || fail "active durable Fusion worker source tree missing: $FUSION_ROOT"
[[ -f "$FUSION_ROOT/docker-compose.yml" && -f "$FUSION_ROOT/docker-compose.v3.yml" ]] ||   fail "durable Fusion worker Compose files missing"

echo "============================================================"
echo " NEXT3 — COMPLETE FINAL RUNTIME AFTER PERFORMANCE GUARD"
echo " host=$(hostname -s)"
echo " fusion_root=$FUSION_ROOT"
echo " fusion_project=$FUSION_PROJECT"
echo " preserved_worker_image=$OLD_FUSION_IMAGE"
echo " production_touch=NONE"
echo "============================================================"

# The prior bounded deployment intentionally stopped because the canonical build
# lost the proven parallel-worker runtime. Restore that exact worker application
# code from the immutable pre-fix image before changing only its provider gate.
TMP="df-next3-preserved-fusion-source-$$"
docker rm -f "$TMP" >/dev/null 2>&1 || true
docker create --name "$TMP" "$OLD_FUSION_IMAGE" >/dev/null
trap 'docker rm -f "$TMP" >/dev/null 2>&1 || true' EXIT

rm -rf "$FUSION_ROOT/services/svc-fusion/app/app"
mkdir -p "$FUSION_ROOT/services/svc-fusion/app/app"
docker cp "$TMP:/app/app/." "$FUSION_ROOT/services/svc-fusion/app/app/"
docker rm -f "$TMP" >/dev/null
trap - EXIT

grep -Rqs "DF_SYNC3_PROVIDER_CONCURRENCY" "$FUSION_ROOT/services/svc-fusion/app/app" ||   fail "preserved Fusion worker does not contain Sync3 provider-concurrency gate"
grep -Rqs "DF_SYNC3_CONCURRENCY_WAIT_SECONDS" "$FUSION_ROOT/services/svc-fusion/app/app" ||   fail "preserved Fusion worker does not contain Sync3 concurrency wait contract"
echo "PRESERVED_SYNC3_PARALLEL_WORKER_SOURCE=PASS"

python3 - "$FUSION_ROOT/docker-compose.v3.yml" <<'PY'
from pathlib import Path
import re,sys

path=Path(sys.argv[1])
text=path.read_text()
m=re.search(r"(?ms)^  svc-fusion-worker:\n(.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)", text)
if not m:
    raise SystemExit("svc-fusion-worker override missing")
block=m.group(0)
env=re.search(r"(?m)^\s+environment:\s*$", block)
if not env:
    raise SystemExit("svc-fusion-worker environment missing")

def set_env(block: str, key: str, value: str) -> str:
    pat=rf"(?m)^(\s+{re.escape(key)}:\s*).*$"
    if re.search(pat, block):
        return re.sub(pat, rf'\g<1>"{value}"', block, count=1)
    lines=block.splitlines()
    idx=next(i for i,line in enumerate(lines) if line.strip()=="environment:")
    lines.insert(idx+1, f'      {key}: "{value}"')
    return "\n".join(lines)+"\n"

block=set_env(block,"DF_FUSION_WORKER_CONCURRENCY","8")
block=set_env(block,"DF_SYNC3_PROVIDER_CONCURRENCY","1")
block=set_env(block,"DF_SYNC3_CONCURRENCY_WAIT_SECONDS","900")
text=text[:m.start()]+block+text[m.end():]
path.write_text(text)
PY

python3 -m py_compile "$FUSION_ROOT/services/svc-fusion/app/app/workers/fusion_worker.py"
echo "FUSION_WORKER_SOURCE_SYNTAX=PASS"

compose(){
  docker compose     --env-file "$LIVE_ENV"     -f "$FUSION_ROOT/docker-compose.yml"     -f "$FUSION_ROOT/docker-compose.v3.yml"     -p "$FUSION_PROJECT" "$@"
}

compose config >/tmp/df-next3-final-fusion-worker-compose.yml

grep -A30 -E '^  svc-fusion-worker:' /tmp/df-next3-final-fusion-worker-compose.yml |   grep -q 'DF_FUSION_WORKER_CONCURRENCY: "8"' || fail "resolved worker concurrency is not 8"
grep -A30 -E '^  svc-fusion-worker:' /tmp/df-next3-final-fusion-worker-compose.yml |   grep -q 'DF_SYNC3_PROVIDER_CONCURRENCY: "1"' || fail "resolved Sync3 provider concurrency is not 1"
echo "FUSION_PERFORMANCE_CONFIG_PREFLIGHT=PASS"

compose --profile v3-execution build svc-fusion-worker
echo "FUSION_WORKER_IMAGE_BUILD=PASS"

# Cut over only the Fusion worker. The image build and all source/config gates
# above complete first, so this is the only mutable backend action in this script.
CURRENT_IMAGE="$(docker inspect df-v3-svc-fusion-worker -f '{{.Image}}')"
docker rm -f df-v3-svc-fusion-worker >/dev/null
compose --profile v3-execution up -d --no-deps --force-recreate svc-fusion-worker

for _ in $(seq 1 30); do
  [[ "$(docker inspect -f '{{.State.Status}}' df-v3-svc-fusion-worker 2>/dev/null || true)" == "running" ]] && break
  sleep 1
done
[[ "$(docker inspect -f '{{.State.Status}}' df-v3-svc-fusion-worker)" == "running" ]] || {
  echo "worker failed to start; restoring previous image"
  docker tag "$CURRENT_IMAGE" desifaces-v3-svc-fusion-worker:latest >/dev/null 2>&1 || true
  compose --profile v3-execution up -d --no-deps --force-recreate svc-fusion-worker >/dev/null 2>&1 || true
  fail "Fusion worker cutover failed"
}

ENV_DUMP="$(docker inspect df-v3-svc-fusion-worker --format '{{range .Config.Env}}{{println .}}{{end}}')"
grep -qx 'DF_FUSION_WORKER_CONCURRENCY=8' <<<"$ENV_DUMP" || fail "Fusion worker concurrency not preserved at 8"
grep -qx 'DF_SYNC3_PROVIDER_CONCURRENCY=1' <<<"$ENV_DUMP" || fail "Sync3 provider concurrency not set to 1"
grep -qx 'DF_SYNC3_CONCURRENCY_WAIT_SECONDS=900' <<<"$ENV_DUMP" || fail "Sync3 wait contract not preserved"

docker exec df-v3-svc-fusion-worker sh -lc   'grep -Rqs "DF_SYNC3_PROVIDER_CONCURRENCY" /app/app && grep -Rqs "DF_SYNC3_CONCURRENCY_WAIT_SECONDS" /app/app' ||   fail "running worker lost Sync3 concurrency implementation"

echo "SYNC3_PROVIDER_CONCURRENCY=1"
echo "FUSION_WORKER_CONCURRENCY_PRESERVED=8"
echo "PRESERVED_SYNC3_PARALLEL_WORKER_RUNTIME=PASS"

docker exec -i df-v3-svc-director python - <<'PY'
from app.studio_e2e_routes import fusion_execution
name=type(fusion_execution).__name__
assert name == "ParallelOrphanReconciledParentPricedSceneFusionExecutionService", name
print("NEXT3_RESILIENT_PARALLEL_EXECUTOR=PASS")
PY

docker exec -i df-v3-svc-director python - <<'PY'
from pathlib import Path
s=Path("/app/app/fusion_execution_parallel_dispatch.py").read_text()
assert "A terminal child failure has already been persisted" in s
assert '"retry_scope": "failed_child_only"' in s
print("NEXT3_FAILED_CHILD_STATUS_CONTRACT=PASS")
PY

docker exec -i df-v3-svc-fusion-extension-stitch-worker python - <<'PY'
import inspect
from app.services import stitch_service
src=inspect.getsource(stitch_service._xfade_pair)
assert "fps=30" in src
assert "settb=AVTB" in src
src2=inspect.getsource(stitch_service.stitch_videos)
assert "concat" in src2
print("NEXT3_CFR_XFADE_GUARD=PASS")
print("NEXT3_STITCH_FALLBACK_PRESERVED=PASS")
PY

[[ "$(docker inspect -f '{{.State.Status}}' df-v3-svc-director-worker)" == "running" ]] ||   fail "Director worker is not running"
[[ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' df-v3-svc-director-worker)" == "unless-stopped" ]] ||   fail "Director worker restart policy is not durable"
echo "DIRECTOR_WORKER_DURABILITY=PASS"

for c in   df-v3-svc-face   df-v3-svc-face-worker   df-v3-svc-audio   df-v3-svc-audio-worker   df-v3-svc-pricing   df-v3-svc-core   df-v3-svc-dashboard   df-v3-svc-fusion   df-v3-svc-fusion-extension   desifaces-v3-db   desifaces-v3-redis
do
  [[ "$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)" == "running" ]] ||     fail "untouched runtime not running: $c"
done
echo "UNTOUCHED_RUNTIME_HEALTH=PASS"

WEB_ROOT=""
for candidate in   /home/azureuser/workspace/desifaces_web   /home/azureuser/workspace/desifaces-web   /home/azureuser/workspace/desifaces_frontend
do
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

echo "NEXT3_WEB_STATUS_RECONCILIATION_DEPLOY=PASS"
df -h /

echo "============================================================"
echo " NEXT3_FINAL_FIX_DEPLOY=PASS"
echo " SYNC3_PROVIDER_CONCURRENCY=1"
echo " GENERAL_FUSION_WORKER_CONCURRENCY=8"
echo " EXISTING_SUCCESSFUL_CHILDREN=PRESERVED_BY_RETRY_CONTRACT"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
