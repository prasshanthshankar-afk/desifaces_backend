#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
LIVE_ENV="/home/azureuser/workspace/desifaces-v3/infra/.env"
TARGET="df-v3-svc-fusion-extension-stitch-worker"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ -f "$LIVE_ENV" ]] || fail "missing DEV runtime env"
docker inspect "$TARGET" >/dev/null 2>&1 || fail "$TARGET missing"

ROOT="$(docker inspect "$TARGET" -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
PROJECT="$(docker inspect "$TARGET" -f '{{index .Config.Labels "com.docker.compose.project"}}')"
[[ -n "$ROOT" && -d "$ROOT" ]] || fail "active stitch source tree missing: $ROOT"
[[ -f "$ROOT/docker-compose.yml" && -f "$ROOT/docker-compose.v3.yml" ]] || fail "stitch Compose files missing"

SRC="$ROOT/services/svc-fusion-extension/app/app/services/stitch_service.py"
[[ -f "$SRC" ]] || fail "stitch source missing: $SRC"

echo "============================================================"
echo " NEXT3 — STITCH OVERRIDE HOTFIX"
echo " host=$(hostname -s)"
echo " root=$ROOT"
echo " project=$PROJECT"
echo " production_touch=NONE"
echo "============================================================"

# Snapshot every runtime except the stitch worker; none may change.
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

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
cp "$SRC" "$SRC.next3-pre-override-$STAMP"

python3 - "$SRC" <<'PY'
from pathlib import Path
import re, sys

path=Path(sys.argv[1])
text=path.read_text()
m=re.search(r"(?ms)^def _xfade_pair\(.*?(?=^def |\Z)", text)
if not m:
    raise SystemExit("_xfade_pair not found")
block=m.group(0)

# The live conversation_handoff caller passes transition_style_override.
if "transition_style_override=" not in text:
    raise SystemExit("conversation_handoff override call not present; refusing unrelated patch")

if "transition_style_override" not in block.split(") -> None:",1)[0]:
    block=block.replace(
        "    transition_duration_sec: float,\n) -> None:",
        "    transition_duration_sec: float,\n    transition_style_override=None,\n) -> None:",
        1,
    )

if "transition_style_override" not in block:
    raise SystemExit("failed to add transition_style_override parameter")

if "transition_style = _transition_style()" in block:
    block=block.replace(
        "    transition_style = _transition_style()",
        "    transition_style = str(transition_style_override or _transition_style()).strip() or _transition_style()",
        1,
    )
elif "transition_style_override or _transition_style()" not in block:
    raise SystemExit("transition style assignment contract changed; refusing unsafe patch")

# Preserve the CFR normalization already deployed.
for marker in ("fps=30", "settb=AVTB", "setpts=PTS-STARTPTS"):
    if marker not in block:
        raise SystemExit("missing CFR marker: "+marker)

text=text[:m.start()]+block+text[m.end():]
path.write_text(text)
PY

python3 -m py_compile "$SRC"

python3 - "$SRC" <<'PY'
import ast,sys
tree=ast.parse(open(sys.argv[1]).read())
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="_xfade_pair")
args=[a.arg for a in fn.args.args+fn.args.kwonlyargs]
assert "transition_style_override" in args, args
print("STITCH_OVERRIDE_SOURCE_CONTRACT=PASS")
PY

compose(){
  docker compose     --env-file "$LIVE_ENV"     -f "$ROOT/docker-compose.yml"     -f "$ROOT/docker-compose.v3.yml"     -p "$PROJECT" "$@"
}

compose --profile v3-execution build svc-fusion-extension-stitch-worker
echo "STITCH_IMAGE_BUILD=PASS"

OLD_IMAGE="$(docker inspect "$TARGET" -f '{{.Image}}')"
ROLLBACK_TAG="desifaces-v3-svc-fusion-extension-stitch-worker:next3-pre-override-$STAMP"
docker tag "$OLD_IMAGE" "$ROLLBACK_TAG"

docker rm -f "$TARGET" >/dev/null
compose --profile v3-execution up -d --no-deps --force-recreate svc-fusion-extension-stitch-worker

for _ in $(seq 1 30); do
  [[ "$(docker inspect -f '{{.State.Status}}' "$TARGET" 2>/dev/null || true)" == "running" ]] && break
  sleep 1
done
[[ "$(docker inspect -f '{{.State.Status}}' "$TARGET")" == "running" ]] || {
  docker tag "$ROLLBACK_TAG" desifaces-v3-svc-fusion-extension-stitch-worker:latest >/dev/null 2>&1 || true
  compose --profile v3-execution up -d --no-deps --force-recreate svc-fusion-extension-stitch-worker >/dev/null 2>&1 || true
  fail "stitch worker failed to start; rollback attempted"
}

docker exec -i "$TARGET" python - <<'PY'
import inspect
from app.services import stitch_service as s

sig=inspect.signature(s._xfade_pair)
assert "transition_style_override" in sig.parameters, sig

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
assert "fps=30" in fc and "settb=AVTB" in fc and "setpts=PTS-STARTPTS" in fc
print("STITCH_OVERRIDE_RUNTIME_CONTRACT=PASS")
print("STITCH_CFR_RUNTIME_CONTRACT=PASS")
PY

for c in "${UNTOUCHED[@]}"; do
  before="${BEFORE[$c]}"
  if [[ "$before" == "ABSENT" ]]; then
    docker inspect "$c" >/dev/null 2>&1 && fail "untouched runtime unexpectedly appeared: $c"
  else
    after="$(docker inspect -f '{{.State.Status}}|{{.State.StartedAt}}|{{.Image}}|{{.RestartCount}}' "$c")"
    [[ "$after" == "$before" ]] || fail "untouched runtime changed: $c"
  fi
done

echo "UNTOUCHED_RUNTIME_INVARIANCE=PASS"
echo "NEXT3_STITCH_OVERRIDE_HOTFIX=PASS"
echo "PRODUCTION_TOUCH=NONE"
