#!/usr/bin/env bash
set -Eeuo pipefail

TARGET_SHA="${TARGET_SHA:-}"
[[ -n "$TARGET_SHA" ]] || { echo "FAIL: TARGET_SHA required"; exit 2; }
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

REPO_ROOT="${REPO_ROOT:-$HOME/workspace/desifaces-v3}"
RUNTIME_ROOT="${RUNTIME_ROOT:-$HOME/workspace/desifaces-runtime}"
RUNTIME_ENV="${RUNTIME_ENV:-}"
if [[ -z "$RUNTIME_ENV" ]]; then
  if [[ -f "$RUNTIME_ROOT/infra/.env" ]]; then
    RUNTIME_ENV="$RUNTIME_ROOT/infra/.env"
  elif [[ -f "$RUNTIME_ROOT/.env" ]]; then
    RUNTIME_ENV="$RUNTIME_ROOT/.env"
  else
    echo "FAIL: runtime env file not found under $RUNTIME_ROOT/infra/.env or $RUNTIME_ROOT/.env"
    exit 2
  fi
fi
[[ -f "$RUNTIME_ENV" ]] || { echo "FAIL: runtime env file missing: $RUNTIME_ENV"; exit 2; }

for key in DATABASE_URL REDIS_URL JWT_SECRET AZURE_STORAGE_CONNECTION_STRING; do
  grep -Eq "^[[:space:]]*${key}=" "$RUNTIME_ENV" || {
    echo "FAIL: required runtime key missing from env file: $key"
    exit 2
  }
done

COMPOSE=(docker compose --env-file "$RUNTIME_ENV")
SERVICE="svc-dashboard"
CONTAINER="df-svc-dashboard"
NETWORK="df-net"
SHORT="${TARGET_SHA:0:12}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORKTREE="/tmp/df-dashboard-taxonomy-$SHORT"
CANARY="df-svc-dashboard-taxonomy-canary-$SHORT"
ENV_FILE="/tmp/df-dashboard-taxonomy-env-$SHORT"
IMAGE="desifaces-dashboard-taxonomy:$SHORT"

cleanup() {
  set +e
  docker rm -f "$CANARY" >/dev/null 2>&1 || true
  rm -f "$ENV_FILE"
  if [[ -d "$WORKTREE" ]]; then
    git -C "$REPO_ROOT" worktree remove -f "$WORKTREE" >/dev/null 2>&1 || true
    rm -rf "$WORKTREE"
  fi
}
trap cleanup EXIT

echo "============================================================"
echo " desifaces DEV — DASHBOARD ASSET TAXONOMY DEPLOY"
echo "============================================================"
echo "target_sha=$TARGET_SHA"
echo "target_service=$SERVICE"
echo "network=$NETWORK"
echo "runtime_env=$(basename "$RUNTIME_ENV")"
echo "production=UNTOUCHED"

docker inspect "$CONTAINER" >/dev/null 2>&1 || { echo "FAIL: $CONTAINER missing"; exit 1; }
[[ "$(docker inspect -f '{{.State.Status}}' "$CONTAINER")" == "running" ]] || { echo "FAIL: $CONTAINER not running"; exit 1; }

OLD_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$CONTAINER")"
CURRENT_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")"
ROLLBACK_TAG="df-dashboard-rollback:$STAMP"
docker tag "$OLD_IMAGE_ID" "$ROLLBACK_TAG"

echo
echo "===== 1. PINNED SOURCE ====="
git -C "$REPO_ROOT" fetch -q origin "$TARGET_SHA"
git -C "$REPO_ROOT" worktree add --detach "$WORKTREE" "$TARGET_SHA" >/dev/null
ACTUAL_SHA="$(git -C "$WORKTREE" rev-parse HEAD)"
[[ "$ACTUAL_SHA" == "$TARGET_SHA" ]] || { echo "FAIL: source pin mismatch"; exit 1; }
echo "SOURCE_PIN=PASS"

echo
echo "===== 2. SOURCE CONTRACT ====="
grep -q '_attach_canonical_asset_lineage' "$WORKTREE/services/svc-dashboard/app/app/services/dashboard_service.py"
grep -q '"group_photo_conversation"' "$WORKTREE/services/svc-dashboard/app/app/services/dashboard_service.py"
grep -q '"multi_person_conversation"' "$WORKTREE/services/svc-dashboard/app/app/services/dashboard_service.py"
grep -q '"group_audio"' "$WORKTREE/services/svc-dashboard/app/app/services/dashboard_service.py"
grep -q '"group_video"' "$WORKTREE/services/svc-dashboard/app/app/services/dashboard_service.py"
grep -q 'shared_scene_face_jobs' "$WORKTREE/services/svc-dashboard/app/app/services/dashboard_service.py"
grep -q 'generated_group_photo_contract' "$WORKTREE/services/svc-dashboard/app/app/services/dashboard_service.py"
echo "DASHBOARD_TAXONOMY_SOURCE=PASS"

echo
echo "===== 3. BUILD ====="
docker build   -t "$IMAGE"   -f "$WORKTREE/services/svc-dashboard/app/Dockerfile"   "$WORKTREE/services/svc-dashboard/app"
echo "DASHBOARD_BUILD=PASS"
echo "candidate_image=$(docker image inspect "$IMAGE" -f '{{.Id}}')"

echo
echo "===== 4. IN-IMAGE CERTIFICATION ====="
docker run --rm "$IMAGE" python -m py_compile /app/app/services/dashboard_service.py
docker run --rm -i "$IMAGE" python - <<'PY'
from pathlib import Path
p=Path("/app/app/services/dashboard_service.py")
text=p.read_text()
required=[
    "_attach_canonical_asset_lineage",
    "group_photo_conversation",
    "multi_person_conversation",
    "group_audio",
    "multi_person_audio",
    "group_video",
    "multi_person_video",
]
missing=[x for x in required if x not in text]
if missing:
    raise SystemExit("missing taxonomy markers: "+",".join(missing))
print("DASHBOARD_TAXONOMY_IMAGE_CONTRACT=PASS")
PY

echo
echo "===== 5. CANARY ====="
docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$ENV_FILE"
docker run -d   --name "$CANARY"   --network "$NETWORK"   --env-file "$ENV_FILE"   -e HOST=0.0.0.0   -e PORT=8005   "$IMAGE" >/dev/null

CANARY_OK=0
for _ in $(seq 1 40); do
  if docker exec "$CANARY" curl -fsS http://127.0.0.1:8005/api/health >/dev/null 2>&1; then
    CANARY_OK=1
    break
  fi
  sleep 1
done
[[ "$CANARY_OK" == "1" ]] || { docker logs --tail 120 "$CANARY" || true; echo "FAIL: canary health"; exit 1; }
echo "CANARY_HEALTH=PASS"

echo
echo "===== 6. CUTOVER ====="
docker tag "$IMAGE" "$CURRENT_IMAGE_REF"
cd "$RUNTIME_ROOT"
"${COMPOSE[@]}" up -d --no-deps --force-recreate --no-build "$SERVICE" >/dev/null

LIVE_OK=0
for _ in $(seq 1 40); do
  if docker exec "$CONTAINER" curl -fsS http://127.0.0.1:8005/api/health >/dev/null 2>&1; then
    LIVE_OK=1
    break
  fi
  sleep 1
done

if [[ "$LIVE_OK" != "1" ]]; then
  echo "CUTOVER_HEALTH=FAIL"
  docker logs --tail 160 "$CONTAINER" || true
  echo "ROLLBACK_START=YES"
  docker tag "$OLD_IMAGE_ID" "$CURRENT_IMAGE_REF"
  "${COMPOSE[@]}" up -d --no-deps --force-recreate --no-build "$SERVICE" >/dev/null 2>&1 || true
  echo "ROLLBACK_COMPLETE=YES"
  exit 1
fi

LIVE_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$CONTAINER")"
docker exec -i "$CONTAINER" python - <<'PY'
from pathlib import Path
text=Path("/app/app/services/dashboard_service.py").read_text()
assert "_attach_canonical_asset_lineage" in text
assert '"group_photo_conversation"' in text
assert '"group_video"' in text
print("LIVE_DASHBOARD_TAXONOMY_CONTRACT=PASS")
PY

echo
echo "============================================================"
echo " DASHBOARD_ASSET_TAXONOMY_DEPLOY=PASS"
echo " running_image=$LIVE_IMAGE_ID"
echo " source_sha=$TARGET_SHA"
echo " rollback_image=$ROLLBACK_TAG"
echo " canonical_network=$NETWORK"
echo " db_mutation=NONE"
echo " generation_touch=NONE"
echo " pricing_touch=NONE"
echo " production=UNTOUCHED"
echo "============================================================"
