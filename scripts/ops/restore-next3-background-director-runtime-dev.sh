#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
LIVE_ENV="/home/azureuser/workspace/desifaces-v3/infra/.env"
TARGET="df-v3-svc-director"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ -f "$LIVE_ENV" ]] || fail "missing DEV runtime env"
docker inspect "$TARGET" >/dev/null 2>&1 || fail "$TARGET missing"

ROOT="$(docker inspect "$TARGET" -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
PROJECT="$(docker inspect "$TARGET" -f '{{index .Config.Labels "com.docker.compose.project"}}')"
[[ -n "$ROOT" && -d "$ROOT" ]] || fail "active Director source tree missing: $ROOT"
[[ -f "$ROOT/docker-compose.yml" && -f "$ROOT/docker-compose.v3.yml" ]] || fail "Director Compose files missing"

ROUTE="$ROOT/services/svc-director/app/app/studio_e2e_routes.py"
RUNTIME="$ROOT/services/svc-director/app/app/fusion_execution_runtime.py"
ASSEMBLY="$ROOT/services/svc-director/app/app/studio_routes_runtime.py"
[[ -f "$ROUTE" && -f "$RUNTIME" && -f "$ASSEMBLY" ]] || fail "Director runtime source files missing"

echo "============================================================"
echo " NEXT3 — RESTORE BACKGROUND DIRECTOR RUNTIME"
echo " host=$(hostname -s)"
echo " root=$ROOT"
echo " project=$PROJECT"
echo " production_touch=NONE"
echo "============================================================"

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

python3 - "$ROUTE" "$RUNTIME" "$ASSEMBLY" <<'PY'
from pathlib import Path
import sys

route=Path(sys.argv[1])
runtime=Path(sys.argv[2])
assembly=Path(sys.argv[3])

text=route.read_text()
explicit=(
    "from .fusion_execution import SceneFusionBridgeError\n"
    "from .fusion_execution_parallel_dispatch import (\n"
    "    ParallelOrphanReconciledParentPricedSceneFusionExecutionService,\n"
    ")\n"
)
canonical="from .fusion_execution import SceneFusionBridgeError, SceneFusionExecutionService\n"

if explicit in text:
    text=text.replace(explicit,canonical,1)
elif canonical not in text:
    raise SystemExit("unexpected Director Fusion import contract")

text=text.replace(
    "fusion_execution = ParallelOrphanReconciledParentPricedSceneFusionExecutionService(\n",
    "fusion_execution = SceneFusionExecutionService(\n",
    1,
)

if "fusion_execution = SceneFusionExecutionService(" not in text:
    raise SystemExit("runtime-installed SceneFusionExecutionService constructor missing")

route.write_text(text)

runtime_text=runtime.read_text()
required=(
    "_fusion_execution.SceneFusionExecutionService = (\n"
    "    BackgroundFinalizedParallelSceneFusionExecutionService\n"
    ")"
)
if required not in runtime_text:
    raise SystemExit("background-finalized Fusion runtime alias missing")

assembly_text=assembly.read_text()
if assembly_text.index("fusion_execution_runtime") > assembly_text.index("studio_e2e_routes"):
    raise SystemExit("Fusion runtime must install before studio_e2e_routes import")

print("DIRECTOR_BACKGROUND_ALIAS_SOURCE=PASS")
PY

python3 -m py_compile "$ROUTE" "$RUNTIME" "$ASSEMBLY"

compose(){
  docker compose \
    --env-file "$LIVE_ENV" \
    -f "$ROOT/docker-compose.yml" \
    -f "$ROOT/docker-compose.v3.yml" \
    -p "$PROJECT" "$@"
}

compose build svc-director
echo "DIRECTOR_IMAGE_BUILD=PASS"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ROLLBACK="${TARGET}-rollback-${STAMP}"

docker rename "$TARGET" "$ROLLBACK"
docker update --restart=no "$ROLLBACK" >/dev/null
docker stop "$ROLLBACK" >/dev/null
echo "DIRECTOR_ROLLBACK_CONTAINER=$ROLLBACK"

rollback(){
  local rc=$?
  set +e
  echo "===== DIRECTOR AUTOMATIC ROLLBACK ====="
  docker rm -f "$TARGET" >/dev/null 2>&1 || true
  if docker inspect "$ROLLBACK" >/dev/null 2>&1; then
    docker rename "$ROLLBACK" "$TARGET" >/dev/null 2>&1 || true
    docker update --restart=unless-stopped "$TARGET" >/dev/null 2>&1 || true
    docker start "$TARGET" >/dev/null 2>&1 || true
  fi
  echo "DIRECTOR_ROLLBACK=COMPLETE"
  exit "$rc"
}
trap rollback ERR

compose up -d --no-deps --force-recreate svc-director

for _ in $(seq 1 30); do
  state="$(docker inspect -f '{{.State.Status}}' "$TARGET" 2>/dev/null || true)"
  [[ "$state" == "running" ]] && break
  sleep 1
done
[[ "$(docker inspect -f '{{.State.Status}}' "$TARGET")" == "running" ]] || fail "Director failed to start"

docker exec -i "$TARGET" python - <<'PY'
from app.studio_e2e_routes import fusion_execution
from app.fusion_execution_background_read import _background_enabled

name=type(fusion_execution).__name__
assert name == "BackgroundFinalizedParallelSceneFusionExecutionService", name
assert _background_enabled() is True
print("DIRECTOR_BACKGROUND_FINALIZED_RUNTIME=PASS")
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
echo "UNTOUCHED_RUNTIME_INVARIANCE=PASS"
echo "NEXT3_BACKGROUND_DIRECTOR_RUNTIME=PASS"
echo "PRODUCTION_TOUCH=NONE"
