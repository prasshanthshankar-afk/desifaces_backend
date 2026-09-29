#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 1; }

ROOT="$HOME/workspace/desifaces-runtime"
ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT/infra/.env}"
REF="${SOURCE_REF:-fix/next3-shared-scene-profile-lock-fix-20260928}"
SOURCE_SHA="$(gh api "repos/prasshanthshankar-afk/desifaces_backend/commits/$REF" --jq .sha)"
LIVE="df-svc-director"
SERVICE="svc-director"
NETWORK="df-net"
CANDIDATE="desifaces-svc-director:shared-scene-state-candidate"
PREP="df-svc-director-state-prep"
CANARY="df-svc-director-state-canary"
FILES=(
  "shared_scene_state_routes.py"
  "shared_scene_routes.py"
  "studio_routes_runtime.py"
  "studio_preflight_routes.py"
)

fail(){ echo "FAIL: $*" >&2; exit 1; }

echo "============================================================"
echo " desifaces DEV — SHARED-SCENE STATE FRAMEWORK"
echo "============================================================"
echo "source_ref=$REF"
echo "source_sha=$SOURCE_SHA"
echo "production=UNTOUCHED"

[[ -f "$ROOT/docker-compose.yml" ]] || fail "canonical compose missing"
[[ -f "$ENV_FILE" ]] || fail "runtime env missing"
docker inspect "$LIVE" >/dev/null 2>&1 || fail "$LIVE missing"
docker network inspect "$NETWORK" >/dev/null 2>&1 || fail "$NETWORK missing"

OLD_ID="$(docker inspect "$LIVE" --format '{{.Image}}')"
LIVE_REF="$(docker inspect "$LIVE" --format '{{.Config.Image}}')"
echo "live_image=$OLD_ID"
echo "live_image_ref=$LIVE_REF"

PATCH_DIR="$(mktemp -d /tmp/df-shared-scene-state.XXXXXX)"
trap 'rm -rf "$PATCH_DIR"' EXIT

echo
echo "===== 1. FETCH + COMPILE TARGET FILES ====="
for name in "${FILES[@]}"; do
  gh api     "repos/prasshanthshankar-afk/desifaces_backend/contents/services/svc-director/app/app/${name}?ref=$SOURCE_SHA"     --jq .content | base64 -d > "$PATCH_DIR/$name"
  python3 -m py_compile "$PATCH_DIR/$name"
  echo "SOURCE_OK=$name"
done

grep -q '/shared-scene-state' "$PATCH_DIR/shared_scene_state_routes.py"
grep -q '/shared-scene-source' "$PATCH_DIR/shared_scene_state_routes.py"
grep -q '/shared-scene-group-photo-spec' "$PATCH_DIR/shared_scene_state_routes.py"
grep -q '/shared-scene-draft' "$PATCH_DIR/shared_scene_routes.py"
grep -q 'shared_scene_people_snapshot' "$PATCH_DIR/studio_preflight_routes.py"
grep -q 'shared_scene_state_routes' "$PATCH_DIR/studio_routes_runtime.py"
grep -q 'snapshot_status' "$PATCH_DIR/shared_scene_state_routes.py"
grep -q 'shared_scene_people_snapshot_repair_requires_complete_profiles' "$PATCH_DIR/shared_scene_state_routes.py"
grep -q 'post-approval source command atomically freezes' "$PATCH_DIR/shared_scene_state_routes.py"
echo "SOURCE_CONTRACT=PASS"
echo "LEGACY_SNAPSHOT_REPAIR_SOURCE=PASS"

echo
echo "===== 2. TARGET DIFF ====="
for name in "${FILES[@]}"; do
  echo "--- $name"
  if docker exec "$LIVE" test -f "/app/app/$name"; then
    docker cp "$LIVE:/app/app/$name" "$PATCH_DIR/live-$name"
    diff -u "$PATCH_DIR/live-$name" "$PATCH_DIR/$name" || true
  else
    echo "NEW_FILE=/app/app/$name"
  fi
done

echo
echo "===== 3. SURGICAL CANDIDATE ====="
docker rm -f "$PREP" "$CANARY" >/dev/null 2>&1 || true
docker image rm "$CANDIDATE" >/dev/null 2>&1 || true

docker create --name "$PREP" "$OLD_ID" >/dev/null
for name in "${FILES[@]}"; do
  docker cp "$PATCH_DIR/$name" "$PREP:/app/app/$name"
done
docker commit --change "LABEL org.opencontainers.image.revision=$SOURCE_SHA" "$PREP" "$CANDIDATE" >/dev/null
docker rm "$PREP" >/dev/null

NEW_ID="$(docker image inspect "$CANDIDATE" --format '{{.Id}}')"
echo "candidate_image=$NEW_ID"

EXCLUDES='! -path "/app/app/shared_scene_state_routes.py" ! -path "/app/app/shared_scene_routes.py" ! -path "/app/app/studio_routes_runtime.py" ! -path "/app/app/studio_preflight_routes.py"'
docker run --rm --entrypoint sh "$OLD_ID" -c   "find /app/app -type f -name '*.py' $EXCLUDES -exec sha256sum {} \; | sort"   > /tmp/director-state-before.txt
docker run --rm --entrypoint sh "$CANDIDATE" -c   "find /app/app -type f -name '*.py' $EXCLUDES -exec sha256sum {} \; | sort"   > /tmp/director-state-after.txt
diff -u /tmp/director-state-before.txt /tmp/director-state-after.txt
echo "NON_TARGET_DIRECTOR_CODE=BYTE_IDENTICAL"

echo
echo "===== 4. CANARY ====="
ENV_SNAPSHOT="$PATCH_DIR/live.env"
docker inspect "$LIVE" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$ENV_SNAPSHOT"

docker run -d   --name "$CANARY"   --network "$NETWORK"   --env-file "$ENV_SNAPSHOT"   --restart no   "$CANDIDATE" >/dev/null

OK=0
for _ in $(seq 1 45); do
  if docker exec "$CANARY" sh -lc     'curl -fsS http://127.0.0.1:${PORT:-8011}/api/health >/dev/null' 2>/dev/null
  then
    OK=1
    break
  fi
  sleep 2
done
(( OK == 1 )) || {
  docker logs --tail 120 "$CANARY" 2>&1 || true
  fail "canary health failed"
}

docker exec -i "$CANARY" python - <<'PY'
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
from pathlib import Path
source=Path("/app/app/shared_scene_state_routes.py").read_text(encoding="utf-8")
assert "snapshot_status" in source
assert "shared_scene_people_snapshot_repair_requires_complete_profiles" in source
assert "post-approval source command atomically freezes" in source
print("CANONICAL_SHARED_SCENE_ROUTES=PASS")
print("LEGACY_SNAPSHOT_REPAIR_CANARY=PASS")
PY

echo "CANARY_HEALTH=PASS"
docker rm -f "$CANARY" >/dev/null

echo
echo "===== 5. PROMOTE DIRECTOR ONLY ====="
docker tag "$OLD_ID" desifaces-svc-director:rollback-shared-scene-state
docker tag "$NEW_ID" "$LIVE_REF"

rollback(){
  rc=$?
  set +e
  docker tag "$OLD_ID" "$LIVE_REF"
  RUNTIME_ENV_FILE="$ENV_FILE" docker compose     --project-directory "$ROOT"     --env-file "$ENV_FILE"     -f "$ROOT/docker-compose.yml"     up -d --no-build --no-deps --force-recreate "$SERVICE" >/dev/null 2>&1
  exit "$rc"
}
trap rollback ERR

RUNTIME_ENV_FILE="$ENV_FILE" docker compose   --project-directory "$ROOT"   --env-file "$ENV_FILE"   -f "$ROOT/docker-compose.yml"   up -d --no-build --no-deps --force-recreate "$SERVICE" >/dev/null

LIVE_OK=0
for _ in $(seq 1 45); do
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 4 http://127.0.0.1:18011/api/health 2>/dev/null || true)"
  if [[ "$CODE" == "200" ]]; then LIVE_OK=1; break; fi
  sleep 2
done
(( LIVE_OK == 1 )) || fail "live Director health failed"

[[ "$(docker inspect "$LIVE" --format '{{.Image}}')" == "$NEW_ID" ]] || fail "running image mismatch"
[[ "$(docker inspect "$LIVE" --format '{{.RestartCount}}')" == "0" ]] || fail "Director restarted"

docker exec -i "$LIVE" python - <<'PY'
from app.main import app
paths={getattr(r,"path","") for r in app.routes}
required={
 "/api/director/studio-workflows/{workflow_id}/shared-scene-state",
 "/api/director/studio-workflows/{workflow_id}/shared-scene-source",
 "/api/director/studio-workflows/{workflow_id}/shared-scene-group-photo-spec",
 "/api/director/studio-workflows/{workflow_id}/stage-runs/{stage_run_id}/shared-scene-draft",
}
assert required.issubset(paths)
from pathlib import Path
source=Path("/app/app/shared_scene_state_routes.py").read_text(encoding="utf-8")
assert "snapshot_status" in source
assert "shared_scene_people_snapshot_repair_requires_complete_profiles" in source
assert "post-approval source command atomically freezes" in source
print("LIVE_SHARED_SCENE_STATE_FRAMEWORK=PASS")
print("LIVE_LEGACY_SNAPSHOT_REPAIR=PASS")
PY

BAD_C="$(docker ps -a --format '{{.Names}}' | grep -Ei 'v3|next3' || true)"
BAD_N="$(docker network ls --format '{{.Name}}' | grep -Ei 'v3|next3' || true)"
[[ -z "$BAD_C" ]] || { echo "$BAD_C"; fail "versioned container name detected"; }
[[ -z "$BAD_N" ]] || { echo "$BAD_N"; fail "versioned network name detected"; }

trap - ERR

echo
echo "============================================================"
echo " SHARED_SCENE_STATE_FRAMEWORK_DEPLOY=PASS"
REV="$(docker image inspect "$NEW_ID" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
[[ "$REV" == "$SOURCE_SHA" ]] || fail "image source revision mismatch"
echo " running_image=$NEW_ID"
echo " source_sha=$SOURCE_SHA"
echo " canonical_network=$NETWORK"
echo " production=UNTOUCHED"
echo "============================================================"
