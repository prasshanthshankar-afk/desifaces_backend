#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
LIVE_ENV="/home/azureuser/workspace/desifaces-v3/infra/.env"
TARGET="df-v3-svc-fusion-extension-stitch-worker"
NEW_IMAGE="desifaces-v3-svc-fusion-extension-stitch-worker:latest"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ -f "$LIVE_ENV" ]] || fail "missing DEV runtime env"
docker inspect "$TARGET" >/dev/null 2>&1 || fail "$TARGET missing"
docker image inspect "$NEW_IMAGE" >/dev/null 2>&1 || fail "already-built stitch hotfix image missing"

ROOT="$(docker inspect "$TARGET" -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
PROJECT="$(docker inspect "$TARGET" -f '{{index .Config.Labels "com.docker.compose.project"}}')"
[[ -n "$ROOT" && -d "$ROOT" ]] || fail "active stitch source tree missing: $ROOT"
[[ -f "$ROOT/docker-compose.yml" && -f "$ROOT/docker-compose.v3.yml" ]] || fail "stitch Compose files missing"

echo "============================================================"
echo " NEXT3 — RESUME STITCH HOTFIX CUTOVER"
echo " host=$(hostname -s)"
echo " root=$ROOT"
echo " project=$PROJECT"
echo " new_image=$NEW_IMAGE"
echo " production_touch=NONE"
echo " rebuild=NONE"
echo "============================================================"

# Verify the already-built image before touching the live worker. Parse source
# directly so no runtime secrets/config are required in this candidate gate.
docker run --rm -i --entrypoint python "$NEW_IMAGE" - <<'PY'
import ast
from pathlib import Path

path=Path("/app/app/services/stitch_service.py")
src=path.read_text()
tree=ast.parse(src)
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="_xfade_pair")
args=[a.arg for a in fn.args.args+fn.args.kwonlyargs]
assert "transition_style_override" in args, args
block=ast.get_source_segment(src,fn) or ""
for marker in ("fps=30","settb=AVTB","setpts=PTS-STARTPTS","transition_style_override"):
    assert marker in block, marker
assert "transition_style_override=" in src
print("STITCH_HOTFIX_IMAGE_CONTRACT=PASS")
PY

UNTOUCHED=(
  df-v3-svc-director
  df-v3-svc-director-worker
  df-v3-svc-fusion-worker
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

compose(){
  docker compose     --env-file "$LIVE_ENV"     -f "$ROOT/docker-compose.yml"     -f "$ROOT/docker-compose.v3.yml"     -p "$PROJECT" "$@"
}

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ROLLBACK_TAG="desifaces-v3-svc-fusion-extension-stitch-worker:rollback-$STAMP"

# The current container's original parent manifest is no longer addressable by
# Docker, which is why docker tag failed in the previous attempt. Snapshot the
# actual running container filesystem instead; this creates a real rollback image.
docker commit "$TARGET" "$ROLLBACK_TAG" >/dev/null
docker image inspect "$ROLLBACK_TAG" >/dev/null
echo "STITCH_ROLLBACK_IMAGE=PASS tag=$ROLLBACK_TAG"

rollback(){
  local rc=$?
  set +e
  echo "===== STITCH HOTFIX AUTOMATIC ROLLBACK ====="
  docker rm -f "$TARGET" >/dev/null 2>&1 || true
  docker tag "$ROLLBACK_TAG" "$NEW_IMAGE" >/dev/null 2>&1 || true
  compose --profile v3-execution up -d --no-deps --force-recreate svc-fusion-extension-stitch-worker >/dev/null 2>&1 || true
  echo "STITCH_HOTFIX_ROLLBACK=ATTEMPTED"
  exit "$rc"
}
trap rollback ERR

# No rebuild: NEW_IMAGE is the candidate that already passed the source gate.
docker rm -f "$TARGET" >/dev/null
compose --profile v3-execution up -d --no-deps --force-recreate svc-fusion-extension-stitch-worker

for _ in $(seq 1 30); do
  [[ "$(docker inspect -f '{{.State.Status}}' "$TARGET" 2>/dev/null || true)" == "running" ]] && break
  sleep 1
done
[[ "$(docker inspect -f '{{.State.Status}}' "$TARGET")" == "running" ]] || fail "new stitch worker did not start"

docker exec -i "$TARGET" python - <<'PY'
import inspect
from app.services import stitch_service as s

sig=inspect.signature(s._xfade_pair)
assert "transition_style_override" in sig.parameters, sig
caller=inspect.getsource(s.stitch_videos)
assert "transition_style_override=" in caller, "live conversation handoff caller missing override"

calls=[]
s._require_nonempty_file=lambda _p: None
s._ensure_parent_dir=lambda _p: None
s._probe_duration_seconds=lambda _p: 2.0
s._run=lambda cmd, **_kw: calls.append(list(cmd))
s._xfade_pair(
    "left.mp4",
    "right.mp4",
    "out.mp4",
    transition_duration_sec=0.2,
    transition_style_override="fade",
)
fc=calls[0][calls[0].index("-filter_complex")+1]
assert "xfade=transition=fade:" in fc
assert "fps=30" in fc
assert "settb=AVTB" in fc
assert "setpts=PTS-STARTPTS" in fc
print("STITCH_OVERRIDE_RUNTIME_CONTRACT=PASS")
print("STITCH_CFR_RUNTIME_CONTRACT=PASS")
print("CONVERSATION_HANDOFF_CALLER_CONTRACT=PASS")
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
echo "NEXT3_STITCH_HOTFIX_CUTOVER=PASS"
echo "PRODUCTION_TOUCH=NONE"
