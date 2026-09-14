#!/usr/bin/env bash
set -Eeuo pipefail

# Azure Managed Run Command normally launches as root with HOME unset.  Re-enter as
# the VM admin user so Git ownership, Docker access, and the DEV workspace match the
# interactive environment used by the certified baseline.  This is certification
# harness behavior only; it does not modify application source or production.
if [[ "$(id -u)" -eq 0 ]]; then
  command -v runuser >/dev/null 2>&1 || { echo "FAIL: runuser unavailable" >&2; exit 1; }
  exec runuser -u azureuser -- env HOME=/home/azureuser PATH="$PATH" bash "$0" "$@"
fi

export HOME="${HOME:-/home/azureuser}"

EXPECTED_HOST="desifaces-dev"
BASE_SHA="26b1dc59dde47cb79ff9ee08fd43d1dbaf0e73b5"
SOURCE_SHA="19f0102459618ddcc272433d437817b037ac28bf"
SOURCE_BRANCH="fix/v3-orphan-authoritative-status-20260914"
IMAGE="desifaces-svc-director:orphan-cert-${SOURCE_SHA:0:12}"
CANDIDATE="df-director-orphan-cert"
REPO="/home/azureuser/workspace/desifaces-v3"
WT="$(mktemp -d /tmp/desifaces-director-orphan-cert.XXXXXX)"
ENVFILE="$(mktemp /tmp/desifaces-director-orphan-env.XXXXXX)"
INSPECT="$(mktemp /tmp/desifaces-director-orphan-inspect.XXXXXX.json)"

cleanup() {
  docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
  if git -C "$REPO" worktree list --porcelain 2>/dev/null | grep -Fq "worktree $WT"; then
    git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
  rm -rf "$WT" "$ENVFILE" "$INSPECT" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "$1=PASS"; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard expected=$EXPECTED_HOST current=$(hostname -s)"
[[ -d "$REPO/.git" ]] || fail "DEV backend repo missing: $REPO"
command -v docker >/dev/null 2>&1 || fail "docker missing"
command -v git >/dev/null 2>&1 || fail "git missing"

cat <<EOF
============================================================
 desifaces — V3 ORPHAN CHILD AUTHORITATIVE STATUS DEV CERT
 source_sha=$SOURCE_SHA
 base_sha=$BASE_SHA
 production_touch=NONE
 database_mutation=NONE_BY_CERT_LOGIC
 host_port_touch=NONE
============================================================
EOF

# Fetch the exact recovery candidate without moving the active DEV checkout.
git -C "$REPO" fetch --quiet origin "$SOURCE_BRANCH"
git -C "$REPO" cat-file -e "${SOURCE_SHA}^{commit}" 2>/dev/null || fail "source commit unavailable"
git -C "$REPO" cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null || fail "base commit unavailable"
git -C "$REPO" worktree add --quiet --detach "$WT" "$SOURCE_SHA"
pass SOURCE_PIN_GATE

# The production code mutation is deliberately one file only; one test file accompanies it.
mapfile -t CHANGED < <(git -C "$WT" diff --name-only "$BASE_SHA" "$SOURCE_SHA" | sort)
EXPECTED=(
  "services/svc-director/app/app/fusion_execution_orphan_recovery.py"
  "services/svc-director/tests/test_fusion_orphan_authoritative_status.py"
)
if [[ "${#CHANGED[@]}" -ne "${#EXPECTED[@]}" ]]; then
  printf 'unexpected changed files:\n%s\n' "${CHANGED[*]}" >&2
  fail "candidate diff is not exactly two files"
fi
for f in "${EXPECTED[@]}"; do
  printf '%s\n' "${CHANGED[@]}" | grep -Fxq "$f" || fail "expected diff missing: $f"
done
pass SOURCE_DIFF_GATE

# Assert critical V3 contracts are byte-for-byte untouched by this fix.
for untouched in \
  services/svc-director/app/app/fusion_execution_parent_pricing.py \
  services/svc-director/app/app/fusion_execution_performance.py \
  services/svc-director/app/app/fusion_execution_resilient.py \
  services/svc-director/app/app/fusion_execution.py \
  services/svc-director/app/app/main.py
 do
  git -C "$WT" diff --quiet "$BASE_SHA" "$SOURCE_SHA" -- "$untouched" || fail "unexpected contract mutation: $untouched"
done
pass PARENT_PRICING_CONTRACT_UNCHANGED
pass CHILD_PRICING_SUPPRESSION_UNCHANGED
pass DIRECTOR_ROUTE_SURFACE_UNCHANGED
pass SINGLE_PERSON_REGRESSION_SURFACE_UNCHANGED

# Prove the recovery is based on the old known-good semantic: authoritative/full
# Fusion state is consulted before declaring an existing provider child still active.
grep -Fq 'needs_full = (' "$WT/services/svc-director/app/app/fusion_execution_orphan_recovery.py" || fail "authoritative full-status gate missing"
grep -Fq 'status_full = getattr(fusion_client, "status_full", None)' "$WT/services/svc-director/app/app/fusion_execution_orphan_recovery.py" || fail "full status fallback missing"
grep -Fq 'fail closed' "$WT/services/svc-director/app/app/fusion_execution_orphan_recovery.py" || fail "fail-closed safety contract missing"
pass AUTHORITATIVE_STATUS_RECOVERY_CONTRACT

# Build an immutable Director candidate from exactly the pinned source.
docker build \
  --pull=false \
  -f "$WT/services/svc-director/app/Dockerfile.v3" \
  -t "$IMAGE" \
  "$WT" >/tmp/desifaces-director-orphan-build.log 2>&1 || {
    tail -n 120 /tmp/desifaces-director-orphan-build.log >&2 || true
    fail "Director candidate image build failed"
  }
pass CANDIDATE_IMAGE_BUILD

DIRECTOR="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
[[ -n "$DIRECTOR" ]] || fail "DEV Director API container not found"
docker inspect "$DIRECTOR" > "$INSPECT"

python3 - "$INSPECT" "$ENVFILE" <<'PY'
import json,sys
obj=json.load(open(sys.argv[1]))[0]
skip={"PORT","DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP"}
with open(sys.argv[2],"w") as out:
    for item in obj.get("Config",{}).get("Env",[]):
        if "=" not in item:
            continue
        k,v=item.split("=",1)
        if k in skip:
            continue
        if "\n" in v or "\r" in v:
            raise SystemExit(f"unsupported newline env: {k}")
        out.write(f"{k}={v}\n")
PY
chmod 600 "$ENVFILE"
NETWORK="$(python3 - "$INSPECT" <<'PY'
import json,sys
nets=list(json.load(open(sys.argv[1]))[0].get("NetworkSettings",{}).get("Networks",{}))
if not nets:
    raise SystemExit(2)
print(nets[0])
PY
)"
[[ -n "$NETWORK" ]] || fail "DEV Director Docker network unresolved"
pass DEV_RUNTIME_DISCOVERY

# Run focused safety tests inside the exact image. These cover both the current
# production failure and the no-new-duplicate regression guard.
docker run --rm -i \
  --env-file "$ENVFILE" \
  -e DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP=false \
  --entrypoint python "$IMAGE" <<'PY'
import asyncio
from app.fusion_execution_orphan_recovery import _authoritative_child_status

class Fake:
    def __init__(self, light, full=None, full_raises=False):
        self.light=dict(light)
        self.full=dict(full or {})
        self.full_raises=full_raises
        self.light_calls=0
        self.full_calls=0
    async def status(self, *, headers, job_id):
        self.light_calls += 1
        return dict(self.light)
    async def status_full(self, *, headers, job_id):
        self.full_calls += 1
        if self.full_raises:
            raise RuntimeError("full unavailable")
        return dict(self.full)

async def main():
    # Exact production defect: light says queued, authoritative full state is complete.
    f=Fake(
        {"status":"queued","artifacts":[]},
        {"status":"succeeded","artifacts":[{"kind":"video","url":"https://example.invalid/completed.mp4?sig=fresh"}]},
    )
    state,url=await _authoritative_child_status(f,headers={},job_id="completed",persisted_state="queued")
    assert state=="succeeded" and url and "completed.mp4" in url
    assert f.light_calls==1 and f.full_calls==1
    print("STALE_QUEUED_FULL_SUCCESS_REUSE=PASS")

    # A genuinely active child must remain blocked; never create a duplicate render.
    f=Fake({"status":"running"},{"status":"running"})
    state,url=await _authoritative_child_status(f,headers={},job_id="running",persisted_state="queued")
    assert state=="running" and not url and f.full_calls==1
    print("TRUE_RUNNING_FAIL_CLOSED=PASS")

    # Failed/canceled children remain eligible for the established failed-child retry path.
    f=Fake({"status":"failed"})
    state,url=await _authoritative_child_status(f,headers={},job_id="failed",persisted_state="queued")
    assert state=="failed" and not url and f.full_calls==0
    print("TERMINAL_FAILURE_RETRY_SEMANTICS=PASS")

    # Existing light success remains fast; no behavior change to healthy jobs.
    f=Fake({"status":"succeeded","video_url":"https://example.invalid/light.mp4?sig=fresh"})
    state,url=await _authoritative_child_status(f,headers={},job_id="light-success",persisted_state="queued")
    assert state=="succeeded" and url and f.full_calls==0
    print("HEALTHY_LIGHT_SUCCESS_UNCHANGED=PASS")

    # If authoritative status is unavailable, retain the active state and fail closed.
    f=Fake({"status":"queued"},full_raises=True)
    state,url=await _authoritative_child_status(f,headers={},job_id="unknown",persisted_state="queued")
    assert state=="queued" and not url and f.full_calls==1
    print("FULL_STATUS_FAILURE_FAIL_CLOSED=PASS")

asyncio.run(main())
PY
pass FOCUSED_SAFETY_TESTS

# Import the full Director application from the candidate image. No source bind mounts.
docker run --rm \
  --env-file "$ENVFILE" \
  -e DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP=false \
  --entrypoint python "$IMAGE" \
  -c 'from app.main import app; assert any(getattr(r,"path","")=="/api/health" for r in app.routes); print("DIRECTOR_APP_IMPORT=PASS")'

# Start an isolated DEV candidate on the same Docker network with no host port.
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
docker run -d \
  --name "$CANDIDATE" \
  --network "$NETWORK" \
  --env-file "$ENVFILE" \
  -e PORT=8011 \
  -e DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP=false \
  "$IMAGE" >/dev/null

READY=0
for _ in $(seq 1 45); do
  if docker exec "$CANDIDATE" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8011/api/health >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done
if (( READY != 1 )); then
  docker logs --tail 160 "$CANDIDATE" >&2 || true
  fail "isolated Director candidate failed health"
fi
pass ISOLATED_CANDIDATE_HEALTH

echo "CANDIDATE_IMAGE=$IMAGE"
echo "CANDIDATE_SOURCE_SHA=$SOURCE_SHA"
echo "PRODUCTION_TOUCH=NONE"
echo "HOST_PORT_TOUCH=NONE"
echo "NEW_PROVIDER_JOB_CREATED=NO"
echo "============================================================"
echo "DEV_ORPHAN_AUTHORITATIVE_STATUS_CERT=PASS"
echo "PRODUCTION_PROMOTION_GATE=PASS"
echo "============================================================"
