#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="/home/azureuser/workspace/desifaces"
ENV_FILE="$ROOT/infra/.env"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/infra/.env.pre-stripe-live-${TS}"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
[[ -f "$ENV_FILE" ]] || fail "production env file missing"
[[ -f "$ROOT/docker-compose.yml" ]] || fail "production docker-compose.yml missing"
docker inspect df-svc-pricing >/dev/null 2>&1 || fail "df-svc-pricing missing"

: "${STRIPE_SECRET_KEY:?missing protected STRIPE_SECRET_KEY}"
: "${STRIPE_PUBLISHABLE_KEY:?missing protected STRIPE_PUBLISHABLE_KEY}"
: "${STRIPE_WEBHOOK_SECRET:?missing protected STRIPE_WEBHOOK_SECRET}"

[[ "$STRIPE_SECRET_KEY" == sk_live_* ]] || fail "secret key is not live mode"
[[ "$STRIPE_PUBLISHABLE_KEY" == pk_live_* ]] || fail "publishable key is not live mode"
[[ "$STRIPE_WEBHOOK_SECRET" == whsec_* ]] || fail "webhook secret format invalid"

mkdir -p "$ROOT/infra"
cp -a "$ENV_FILE" "$BACKUP"
chmod 600 "$BACKUP"

BEFORE_OTHERS="$(docker ps -a --format '{{.Names}}|{{.ID}}' | grep -v '^df-svc-pricing|' | sort | sha256sum | awk '{print $1}')"
OLD_PRICING_ID="$(docker inspect -f '{{.Id}}' df-svc-pricing)"

python3 - "$ENV_FILE" <<'PY'
import os, sys, tempfile
from pathlib import Path

path = Path(sys.argv[1])
st = path.stat()
updates = {
    "STRIPE_SECRET_KEY": os.environ["STRIPE_SECRET_KEY"],
    "STRIPE_PUBLISHABLE_KEY": os.environ["STRIPE_PUBLISHABLE_KEY"],
    "STRIPE_WEBHOOK_SECRET": os.environ["STRIPE_WEBHOOK_SECRET"],
    "DF_PAYMENT_GATEWAY_ENABLED": "true",
    "DF_PAYMENT_GATEWAY_PROVIDER": "stripe",
}

lines = path.read_text().splitlines()
out = []
seen = set()
for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        out.append(line)
        continue
    key = line.split("=", 1)[0].strip()
    if key in updates:
        if key not in seen:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")

tmp = path.with_name(path.name + ".stripe-live.tmp")
tmp.write_text("\n".join(out) + "\n")
os.chmod(tmp, st.st_mode & 0o777)
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, path)
PY
chmod 600 "$ENV_FILE"

echo "============================================================"
echo " desifaces — STRIPE LIVE RUNTIME INSTALL"
echo " secret_values_printed=NO"
echo " pricing_service_only=YES"
echo "============================================================"
echo "env_backup=$BACKUP"
echo "ENV_BACKUP_GATE=PASS"

cd "$ROOT"
docker compose config --services | grep -Fxq 'svc-pricing' || fail "svc-pricing compose service missing"

echo "===== RECREATE PRICING ONLY ====="
docker compose up -d --no-deps --force-recreate svc-pricing >/dev/null

for i in $(seq 1 36); do
  STATE="$(docker inspect -f '{{.State.Status}}' df-svc-pricing 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' df-svc-pricing 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    break
  fi
  sleep 5
done
[[ "$STATE" == "running" ]] || fail "pricing container not running"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "pricing container unhealthy: $HEALTH"

NEW_PRICING_ID="$(docker inspect -f '{{.Id}}' df-svc-pricing)"
[[ "$NEW_PRICING_ID" != "$OLD_PRICING_ID" ]] || fail "pricing container was not recreated"
AFTER_OTHERS="$(docker ps -a --format '{{.Names}}|{{.ID}}' | grep -v '^df-svc-pricing|' | sort | sha256sum | awk '{print $1}')"
[[ "$AFTER_OTHERS" == "$BEFORE_OTHERS" ]] || fail "non-pricing container identity changed"

MODE_GATE="$(docker exec df-svc-pricing python - <<'PY'
import os
ok = (
    os.getenv('STRIPE_SECRET_KEY','').startswith('sk_live_') and
    os.getenv('STRIPE_PUBLISHABLE_KEY','').startswith('pk_live_') and
    os.getenv('STRIPE_WEBHOOK_SECRET','').startswith('whsec_') and
    os.getenv('DF_PAYMENT_GATEWAY_ENABLED','').strip().lower() in {'1','true','yes','on'} and
    os.getenv('DF_PAYMENT_GATEWAY_PROVIDER','').strip().lower() == 'stripe'
)
print('PASS' if ok else 'FAIL')
PY
)"
[[ "$MODE_GATE" == "PASS" ]] || fail "pricing runtime live-mode env gate failed"

echo "pricing_state=$STATE"
echo "pricing_health=$HEALTH"
echo "pricing_runtime_live_mode=PASS"
echo "non_pricing_containers_unchanged=PASS"
echo "STRIPE_LIVE_RUNTIME_INSTALL=PASS"
echo "customer_charge=NONE"
