#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
ROOT="/home/azureuser/workspace/desifaces-v3"
ENV_FILE="$ROOT/infra/.env"
API="df-v3-svc-fusion"
WORKER="df-v3-svc-fusion-worker"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ -f "$ENV_FILE" ]] || fail "DEV env file missing: $ENV_FILE"
docker inspect "$API" >/dev/null 2>&1 || fail "$API missing"
docker inspect "$WORKER" >/dev/null 2>&1 || fail "$WORKER missing"
[[ "$(docker inspect -f '{{.State.Status}}' "$WORKER")" == "running" ]] || fail "$WORKER not running"

echo "============================================================"
echo " desifaces DEV — SAFE SYNC3 KEY ROTATION"
echo " production_touch=NONE"
echo " env=$ENV_FILE"
echo "============================================================"

read -rsp "Paste freshly-created FULL Sync API key: " NEW_SYNC_KEY
echo
[[ -n "$NEW_SYNC_KEY" ]] || fail "empty key"

validate_key() {
  local key="$1"
  printf '%s' "$key" | docker exec -i "$WORKER" python -c '
import sys, httpx
key=sys.stdin.read().strip()
if not key:
    print("SYNC_PROVIDER_AUTH=FAIL")
    raise SystemExit(2)
try:
    r=httpx.get(
        "https://api.sync.so/v2/generations",
        headers={"x-api-key": key, "Accept": "application/json"},
        timeout=20,
    )
except Exception as exc:
    print("SYNC_PROVIDER_AUTH=ERROR")
    print(type(exc).__name__)
    raise SystemExit(3)
print("SYNC_PROVIDER_HTTP="+str(r.status_code))
if r.status_code != 200:
    print("SYNC_PROVIDER_AUTH=FAIL")
    raise SystemExit(2)
print("SYNC_PROVIDER_AUTH=PASS")
'
}

echo
echo "=== 1. VALIDATE CANDIDATE BEFORE SAVING ==="
if ! validate_key "$NEW_SYNC_KEY"; then
  unset NEW_SYNC_KEY
  fail "candidate Sync key rejected; canonical DEV env was not changed"
fi
echo "SYNC_CANDIDATE_PREFLIGHT=PASS"

OLD_SYNC_KEY="$(
python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys
for raw in Path(sys.argv[1]).read_text().splitlines():
    if raw.startswith("SYNC_API_KEY="):
        print(raw.split("=",1)[1], end="")
        break
PY
)"

write_key() {
  local key="$1"
  printf '%s' "$key" | python3 - "$ENV_FILE" <<'PY'
PY
}

# Use a temporary helper file descriptor so the secret is consumed from stdin
# rather than placed on the Python command line or echoed to terminal.
write_env_key() {
  local key="$1"
  SYNC_KEY_STDIN="$key" python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import os, sys
p=Path(sys.argv[1])
key=os.environ["SYNC_KEY_STDIN"].strip()
if not key:
    raise SystemExit("empty key")
lines=p.read_text().splitlines()
out=[]
found=False
for line in lines:
    if line.startswith("SYNC_API_KEY="):
        out.append("SYNC_API_KEY="+key)
        found=True
    else:
        out.append(line)
if not found:
    out.append("SYNC_API_KEY="+key)
p.write_text("\n".join(out)+"\n")
PY
  chmod 600 "$ENV_FILE"
}

rollback() {
  local rc=$?
  if (( rc == 0 )); then return 0; fi
  set +e
  echo
  echo "=== DEV SYNC KEY ROLLBACK ==="
  if [[ -n "$OLD_SYNC_KEY" ]]; then
    write_env_key "$OLD_SYNC_KEY"
    PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$API" 2>/dev/null)"
    if [[ -n "$PROJECT" ]]; then
      cd "$ROOT"
      V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" up -d --no-deps --force-recreate svc-fusion >/dev/null 2>&1 || true
      V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" --profile v3-execution up -d --no-deps --force-recreate svc-fusion-worker >/dev/null 2>&1 || true
    fi
    echo "DEV_SYNC_KEY_ROLLBACK=ATTEMPTED"
  else
    echo "DEV_SYNC_KEY_ROLLBACK=UNAVAILABLE_NO_PRIOR_KEY"
  fi
  unset NEW_SYNC_KEY OLD_SYNC_KEY
  exit "$rc"
}
trap rollback ERR

echo
echo "=== 2. SAVE VALIDATED KEY TO CANONICAL DEV ENV ==="
write_env_key "$NEW_SYNC_KEY"
echo "DEV_SYNC_KEY_SAVED=PASS"

echo
echo "=== 3. RECREATE ONLY FUSION API + WORKER ==="
cd "$ROOT"
PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$API")"
[[ -n "$PROJECT" ]] || fail "Compose project missing"

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" \
  up -d --no-deps --force-recreate svc-fusion

V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" --profile v3-execution \
  up -d --no-deps --force-recreate svc-fusion-worker

echo
echo "=== 4. VERIFY RUNNING WORKER USES ACCEPTED KEY ==="
docker exec -i "$WORKER" python - <<'PY'
import asyncio, httpx
from app.services.providers.sync3_adapter import Sync3Adapter

async def main():
    a=Sync3Adapter()
    assert a.api_key, "SYNC_API_KEY missing"
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.get(
            f"{a.base_url}/v2/generations",
            headers={"x-api-key": a.api_key, "Accept": "application/json"},
        )
    print("SYNC_PROVIDER_HTTP="+str(r.status_code))
    assert r.status_code == 200, f"Sync authentication failed: HTTP {r.status_code}"
    print("SYNC_PROVIDER_AUTH=PASS")

asyncio.run(main())
PY

trap - ERR
unset NEW_SYNC_KEY OLD_SYNC_KEY

echo
echo "============================================================"
echo " DEV SYNC3 KEY ROTATION COMPLETE"
echo " SYNC_PROVIDER_AUTH=PASS"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
