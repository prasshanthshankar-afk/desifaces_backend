#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
TARGET="df-v3-svc-director"
IMAGE="desifaces-v3-svc-director:latest"
LIVE_ENV="/home/azureuser/workspace/desifaces-v3/infra/.env"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
docker inspect "$TARGET" >/dev/null 2>&1 || fail "$TARGET missing"
[[ -f "$LIVE_ENV" ]] || fail "missing DEV runtime env"

ROOT="$(docker inspect "$TARGET" -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
PROJECT="$(docker inspect "$TARGET" -f '{{index .Config.Labels "com.docker.compose.project"}}')"
[[ -n "$ROOT" && -d "$ROOT" ]] || fail "Director source tree missing: $ROOT"

ROUTE="$ROOT/services/svc-director/app/app/studio_e2e_routes.py"
[[ -f "$ROUTE" ]] || fail "studio_e2e_routes.py missing"

echo "============================================================"
echo " NEXT3 — FINAL DIRECTOR BACKGROUND CUTOVER"
echo " host=$(hostname -s)"
echo " root=$ROOT"
echo " project=$PROJECT"
echo " production_touch=NONE"
echo "============================================================"

# Patch only the route construction. Runtime installer is imported explicitly so
# class selection cannot depend on package import timing.
python3 - "$ROUTE" <<'PY'
from pathlib import Path
import sys

p=Path(sys.argv[1])
t=p.read_text()

old_alias="from .fusion_execution import SceneFusionBridgeError, SceneFusionExecutionService\n"
old_parallel=(
    "from .fusion_execution import SceneFusionBridgeError\n"
    "from .fusion_execution_parallel_dispatch import (\n"
    "    ParallelOrphanReconciledParentPricedSceneFusionExecutionService,\n"
    ")\n"
)
new=(
    "from .fusion_execution import SceneFusionBridgeError\n"
    "from . import fusion_execution_runtime as _fusion_execution_runtime  # install V3 runtime patches before instantiation\n"
    "from .fusion_execution_background_read import BackgroundFinalizedParallelSceneFusionExecutionService\n"
)

if old_alias in t:
    t=t.replace(old_alias,new,1)
elif old_parallel in t:
    t=t.replace(old_parallel,new,1)
elif new not in t:
    raise SystemExit("unexpected Fusion import block")

t=t.replace(
    "fusion_execution = SceneFusionExecutionService(\n",
    "fusion_execution = BackgroundFinalizedParallelSceneFusionExecutionService(\n",
    1,
)
t=t.replace(
    "fusion_execution = ParallelOrphanReconciledParentPricedSceneFusionExecutionService(\n",
    "fusion_execution = BackgroundFinalizedParallelSceneFusionExecutionService(\n",
    1,
)

if "fusion_execution = BackgroundFinalizedParallelSceneFusionExecutionService(" not in t:
    raise SystemExit("background-finalized constructor missing")
if "fusion_execution_runtime as _fusion_execution_runtime" not in t:
    raise SystemExit("runtime installer import missing")

p.write_text(t)
print("DIRECTOR_BACKGROUND_EXPLICIT_SOURCE=PASS")
PY

python3 -m py_compile "$ROUTE" \
  "$ROOT/services/svc-director/app/app/fusion_execution_runtime.py" \
  "$ROOT/services/svc-director/app/app/fusion_execution_background_read.py"

UNTOUCHED=(
  df-v3-svc-director-worker
  df-v3-svc-fusion-worker
  df-v3-svc-fusion-extension-stitch-worker
  df-web-dev
  df-v3-svc-face
  df-v3-svc-audio
  df-v3-svc-pricing
  df-v3-svc-fusion
  desifaces-v3-db
  desifaces-v3-redis
)
declare -A BEFORE
for c in "${UNTOUCHED[@]}"; do
  if docker inspect "$c" >/dev/null 2>&1; then
    BEFORE["$c"]="$(docker inspect -f '{{.State.Status}}|{{.State.StartedAt}}|{{.Image}}|{{.RestartCount}}' "$c")"
  else
    BEFORE["$c"]="ABSENT"
  fi
done

# Validate the live Director networking/port contract before cutover.
NETWORK="$(docker inspect "$TARGET" -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | sed '/^$/d')"
[[ "$(printf '%s\n' "$NETWORK" | wc -l)" -eq 1 ]] || fail "Director must have exactly one network: $NETWORK"
PORT_BIND="$(docker inspect "$TARGET" -f '{{with index .HostConfig.PortBindings "8011/tcp"}}{{(index . 0).HostIp}}:{{(index . 0).HostPort}}{{end}}')"
[[ "$PORT_BIND" == "127.0.0.1:18011" ]] || fail "unexpected Director port binding: $PORT_BIND"
echo "DIRECTOR_RUNTIME_TOPOLOGY=PASS network=$NETWORK port=$PORT_BIND"

compose(){
  docker compose \
    --env-file "$LIVE_ENV" \
    -f "$ROOT/docker-compose.yml" \
    -f "$ROOT/docker-compose.v3.yml" \
    -p "$PROJECT" "$@"
}

compose build svc-director
echo "DIRECTOR_IMAGE_BUILD=PASS"

# Verify the built image before changing the live container.
docker run --rm --env-file "$LIVE_ENV" --entrypoint python "$IMAGE" - <<'PY'
from app.studio_e2e_routes import fusion_execution
from app.fusion_execution_background_read import BackgroundFinalizedParallelSceneFusionExecutionService
from app.fusion_execution_parallel_dispatch import ParallelOrphanReconciledParentPricedSceneFusionExecutionService

assert type(fusion_execution).__name__ == "BackgroundFinalizedParallelSceneFusionExecutionService", type(fusion_execution).__name__
assert isinstance(fusion_execution, BackgroundFinalizedParallelSceneFusionExecutionService)
assert isinstance(fusion_execution, ParallelOrphanReconciledParentPricedSceneFusionExecutionService)
assert getattr(ParallelOrphanReconciledParentPricedSceneFusionExecutionService, "_preserved_child_url_refresh_installed", False) is True
print("DIRECTOR_CANDIDATE_BACKGROUND_RUNTIME=PASS")
print("DIRECTOR_CANDIDATE_SELECTIVE_RETRY=PASS")
PY

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ROLLBACK="${TARGET}-rollback-${STAMP}"
ENV_FILE="/tmp/next3-director-${STAMP}.env"
LABEL_FILE="/tmp/next3-director-${STAMP}.labels"

python3 - "$TARGET" "$ENV_FILE" "$LABEL_FILE" <<'PY'
import json, os, subprocess, sys
container, env_file, label_file=sys.argv[1:]
o=json.loads(subprocess.check_output(["docker","inspect",container]))[0]
with open(env_file,"w",encoding="utf-8") as f:
    for item in o["Config"].get("Env") or []:
        if "\n" in item or "\r" in item: raise SystemExit("invalid newline in env")
        f.write(item+"\n")
with open(label_file,"w",encoding="utf-8") as f:
    for k,v in sorted((o["Config"].get("Labels") or {}).items()):
        v="" if v is None else str(v)
        if "\n" in v or "\r" in v: raise SystemExit("invalid newline in label")
        f.write(f"{k}={v}\n")
os.chmod(env_file,0o600)
print("DIRECTOR_RUNTIME_CONFIG_CAPTURE=PASS")
PY

docker rename "$TARGET" "$ROLLBACK"
docker update --restart=no "$ROLLBACK" >/dev/null
docker stop "$ROLLBACK" >/dev/null
echo "DIRECTOR_ROLLBACK_CONTAINER=$ROLLBACK"

rollback(){
  rc=$?
  set +e
  echo "===== DIRECTOR AUTOMATIC ROLLBACK ====="
  docker rm -f "$TARGET" >/dev/null 2>&1 || true
  if docker inspect "$ROLLBACK" >/dev/null 2>&1; then
    docker rename "$ROLLBACK" "$TARGET" >/dev/null 2>&1 || true
    docker update --restart=unless-stopped "$TARGET" >/dev/null 2>&1 || true
    docker start "$TARGET" >/dev/null 2>&1 || true
  fi
  rm -f "$ENV_FILE" "$LABEL_FILE"
  echo "DIRECTOR_ROLLBACK=COMPLETE"
  exit "$rc"
}
trap rollback ERR

docker run -d \
  --name "$TARGET" \
  --restart unless-stopped \
  --network "$NETWORK" \
  --network-alias svc-director \
  -p 127.0.0.1:18011:8011 \
  --env-file "$ENV_FILE" \
  --label-file "$LABEL_FILE" \
  "$IMAGE" >/dev/null

for _ in $(seq 1 30); do
  state="$(docker inspect -f '{{.State.Status}}' "$TARGET" 2>/dev/null || true)"
  [[ "$state" == "running" ]] && break
  sleep 1
done
[[ "$(docker inspect -f '{{.State.Status}}' "$TARGET")" == "running" ]] || fail "new Director failed to start"
sleep 3
[[ "$(docker inspect -f '{{.State.Status}}' "$TARGET")" == "running" ]] || fail "new Director exited after startup"

docker exec -i "$TARGET" python - <<'PY'
from app.studio_e2e_routes import fusion_execution
from app.fusion_execution_background_read import BackgroundFinalizedParallelSceneFusionExecutionService, _background_enabled
from app.fusion_execution_parallel_dispatch import ParallelOrphanReconciledParentPricedSceneFusionExecutionService

name=type(fusion_execution).__name__
assert name == "BackgroundFinalizedParallelSceneFusionExecutionService", name
assert isinstance(fusion_execution, BackgroundFinalizedParallelSceneFusionExecutionService)
assert isinstance(fusion_execution, ParallelOrphanReconciledParentPricedSceneFusionExecutionService)
assert _background_enabled() is True
assert getattr(ParallelOrphanReconciledParentPricedSceneFusionExecutionService, "_preserved_child_url_refresh_installed", False) is True
print("DIRECTOR_BACKGROUND_FINALIZED_RUNTIME=PASS")
print("DIRECTOR_SELECTIVE_RETRY_RUNTIME=PASS")
print("HTTP_SYNC_READ_ONLY_BACKGROUND_MODE=PASS")
PY

for c in "${UNTOUCHED[@]}"; do
  before="${BEFORE[$c]}"
  if [[ "$before" == "ABSENT" ]]; then
    docker inspect "$c" >/dev/null 2>&1 && fail "untouched runtime unexpectedly appeared: $c"
  else
    after="$(docker inspect -f '{{.State.Status}}|{{.State.StartedAt}}|{{.Image}}|{{.RestartCount}}' "$c")"
    [[ "$after" == "$before" ]] || fail "untouched runtime changed: $c before=$before after=$after"
  fi
done

trap - ERR
rm -f "$ENV_FILE" "$LABEL_FILE"
echo "UNTOUCHED_RUNTIME_INVARIANCE=PASS"
echo "NEXT3_DIRECTOR_BACKGROUND_FINAL=PASS"
echo "PRODUCTION_TOUCH=NONE"
