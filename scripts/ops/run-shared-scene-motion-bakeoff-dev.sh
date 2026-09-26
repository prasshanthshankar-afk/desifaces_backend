#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
ROOT="/home/azureuser/workspace/desifaces-v3"
TARGET_SHA="b82233c3559e15dcb31f7e72fb1f25f8d06a4cb0"
WT="/tmp/desifaces-motion-bakeoff-$TARGET_SHA"
LIVE="df-v3-svc-fusion"
IMAGE="desifaces-motion-bakeoff:$TARGET_SHA"
ENV_FILE="/tmp/desifaces-motion-bakeoff.env"
LOG="/tmp/desifaces-motion-bakeoff-$(date -u +%Y%m%dT%H%M%SZ).log"

fail(){ echo "FAIL: $*" >&2; exit 1; }

cleanup(){
  set +e
  rm -f "$ENV_FILE"
  if git -C "$ROOT" worktree list --porcelain 2>/dev/null | grep -Fxq "worktree $WT"; then
    git -C "$ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
docker inspect "$LIVE" >/dev/null 2>&1 || fail "$LIVE missing"
[[ "$(docker inspect -f '{{.State.Status}}' "$LIVE")" == "running" ]] || fail "$LIVE not running"
NETWORK="$(docker inspect "$LIVE" -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' | sed '/^$/d' | head -n1)"
[[ "$NETWORK" == "df-v3-net" ]] || fail "unexpected DEV network=$NETWORK"

echo "============================================================"
echo " desifaces DEV — FINAL SYNC3 vs OMNIHUMAN MOTION BAKEOFF"
echo " target_sha=$TARGET_SHA"
echo " live_runtime_mutation=NONE"
echo " production_touch=NONE"
echo "============================================================"

python3 - "$LIVE" "$ENV_FILE" <<'PY'
import json, os, subprocess, sys
container, path = sys.argv[1:]
obj=json.loads(subprocess.check_output(["docker","inspect",container]))[0]
with open(path,"w",encoding="utf-8") as f:
    for item in obj["Config"].get("Env") or []:
        if "\n" in item or "\r" in item:
            raise SystemExit("invalid environment newline")
        f.write(item+"\n")
os.chmod(path,0o600)
PY

grep -Eq '^DATABASE_URL=.+' "$ENV_FILE" || fail "DATABASE_URL missing from DEV Fusion runtime"
grep -Eq '^FAL_KEY=.+' "$ENV_FILE" || fail "FAL_KEY missing from DEV Fusion runtime"
echo "BAKEOFF_RUNTIME_ENV=PASS"

git -C "$ROOT" fetch --no-tags origin "$TARGET_SHA"
git -C "$ROOT" cat-file -e "$TARGET_SHA^{commit}"
if git -C "$ROOT" worktree list --porcelain | grep -Fxq "worktree $WT"; then
  git -C "$ROOT" worktree remove --force "$WT"
elif [[ -e "$WT" ]]; then
  fail "unrecognized bakeoff worktree exists"
fi
git -C "$ROOT" worktree add --detach "$WT" "$TARGET_SHA"
[[ "$(git -C "$WT" rev-parse HEAD)" == "$TARGET_SHA" ]] || fail "bakeoff worktree SHA mismatch"

grep -Fq 'request_json["mask_url"]' "$WT/services/svc-fusion/app/app/services/providers/omnihuman_adapter.py"
grep -Fq 'make_vertical_speaker_mask' "$WT/scripts/ops/run-shared-scene-motion-bakeoff-dev.py"
python3 -m py_compile \
  "$WT/services/svc-fusion/app/app/services/providers/omnihuman_adapter.py" \
  "$WT/scripts/ops/run-shared-scene-motion-bakeoff-dev.py"
echo "OMNIHUMAN_MASK_BAKEOFF_SOURCE=PASS"

echo
echo "=== BUILD ISOLATED BAKEOFF IMAGE — LIVE FUSION IS NOT REPLACED ==="
docker build \
  -f "$WT/services/svc-fusion/app/Dockerfile" \
  -t "$IMAGE" \
  "$WT"
echo "BAKEOFF_IMAGE_BUILD=PASS"

echo
echo "=== RUN ONE REPRESENTATIVE TURN ==="
docker run --rm \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  "$IMAGE" \
  python /repo/scripts/ops/run-shared-scene-motion-bakeoff-dev.py \
  2>&1 | tee "$LOG"

grep -Fq "SHARED-SCENE MOTION BAKEOFF READY" "$LOG" || fail "bakeoff did not complete"
grep -Fq "SYNC3_BASELINE_VIDEO_URL=" "$LOG" || fail "Sync3 baseline URL missing"
grep -Fq "OMNIHUMAN_MASK_VIDEO_URL=" "$LOG" || fail "OmniHuman candidate URL missing"
grep -Fq "PRODUCTION_TOUCH=NONE" "$LOG" || fail "production guard marker missing"

echo
echo "============================================================"
echo " FINAL MOTION BAKEOFF COMPLETE"
echo " log=$LOG"
echo " LIVE_RUNTIME_MUTATION=NONE"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
