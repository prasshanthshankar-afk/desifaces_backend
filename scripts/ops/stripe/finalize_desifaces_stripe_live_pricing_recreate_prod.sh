#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/home/azureuser/workspace/desifaces
COMPOSE="$ROOT/docker-compose.yml"
ENV_FILE="$ROOT/infra/.env"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
[[ -f "$COMPOSE" ]] || fail "production compose file missing"
[[ -f "$ENV_FILE" ]] || fail "production env file missing"
docker inspect df-svc-pricing >/dev/null 2>&1 || fail "df-svc-pricing missing"

echo "============================================================"
echo " desifaces — STRIPE LIVE PRICING FINAL RECREATE"
echo " build=FORBIDDEN"
echo " other_services=FORBIDDEN"
echo " secrets_printed=NO"
echo "============================================================"

echo
echo "===== 1. PRE-GATES ====="
docker compose -p desifaces -f "$COMPOSE" --env-file "$ENV_FILE" config --services | grep -Fxq svc-pricing || fail "svc-pricing missing from production compose"

ENV_GATE="$(python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); d={}
for line in p.read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        k,v=line.split('=',1); d[k.strip()]=v.strip()
ok=(d.get('STRIPE_SECRET_KEY','').startswith('sk_live_') and
    d.get('STRIPE_PUBLISHABLE_KEY','').startswith('pk_live_') and
    d.get('STRIPE_WEBHOOK_SECRET','').startswith('whsec_') and
    d.get('DF_PAYMENT_GATEWAY_ENABLED','').strip().lower() in {'1','true','yes','on'} and
    d.get('DF_PAYMENT_GATEWAY_PROVIDER','').strip().lower()=='stripe')
print('PASS' if ok else 'FAIL')
PY
)"
[[ "$ENV_GATE" == "PASS" ]] || fail "production env live Stripe gate failed"

OLD_ID="$(docker inspect -f '{{.Id}}' df-svc-pricing)"
OLD_IMAGE="$(docker inspect -f '{{.Image}}' df-svc-pricing)"
OTHERS_BEFORE="$(docker ps -a --format '{{.Names}}|{{.ID}}' | grep -v '^df-svc-pricing|' | sort | sha256sum | awk '{print $1}')"

echo "compose_service=svc-pricing"
echo "existing_live_env_gate=PASS"
echo "pricing_image_id=$OLD_IMAGE"
echo "COMPOSE_GATE=PASS"

echo
echo "===== 2. RECREATE PRICING ONLY ====="
cd "$ROOT"
docker compose -p desifaces -f "$COMPOSE" --env-file "$ENV_FILE" up -d --no-deps --no-build --force-recreate svc-pricing

echo
echo "===== 3. WAIT FOR HEALTH ====="
STATE=""
HEALTH=""
for _ in $(seq 1 36); do
  STATE="$(docker inspect -f '{{.State.Status}}' df-svc-pricing 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' df-svc-pricing 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    break
  fi
  sleep 5
done
[[ "$STATE" == "running" ]] || fail "pricing container not running"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "pricing health=$HEALTH"

NEW_ID="$(docker inspect -f '{{.Id}}' df-svc-pricing)"
NEW_IMAGE="$(docker inspect -f '{{.Image}}' df-svc-pricing)"
[[ "$NEW_ID" != "$OLD_ID" ]] || fail "pricing container was not recreated"
[[ "$NEW_IMAGE" == "$OLD_IMAGE" ]] || fail "pricing image changed"

echo "pricing_state=$STATE"
echo "pricing_health=$HEALTH"
echo "same_production_image=PASS"

echo
echo "===== 4. LIVE STRIPE RUNTIME GATE ====="
MODE="$(docker exec df-svc-pricing python - <<'PY'
import os
checks={
 'secret': os.getenv('STRIPE_SECRET_KEY','').startswith('sk_live_'),
 'publishable': os.getenv('STRIPE_PUBLISHABLE_KEY','').startswith('pk_live_'),
 'webhook': os.getenv('STRIPE_WEBHOOK_SECRET','').startswith('whsec_'),
 'gateway_enabled': os.getenv('DF_PAYMENT_GATEWAY_ENABLED','').strip().lower() in {'1','true','yes','on'},
 'gateway_provider': os.getenv('DF_PAYMENT_GATEWAY_PROVIDER','').strip().lower() == 'stripe',
}
for k,v in checks.items(): print(f"{k}={'PASS' if v else 'FAIL'}")
print('OVERALL=' + ('PASS' if all(checks.values()) else 'FAIL'))
PY
)"
printf '%s\n' "$MODE"
grep -Fq 'OVERALL=PASS' <<<"$MODE" || fail "pricing runtime live Stripe gate failed"

echo
echo "===== 5. OTHER-CONTAINER IMMUTABILITY ====="
OTHERS_AFTER="$(docker ps -a --format '{{.Names}}|{{.ID}}' | grep -v '^df-svc-pricing|' | sort | sha256sum | awk '{print $1}')"
[[ "$OTHERS_AFTER" == "$OTHERS_BEFORE" ]] || fail "another container identity changed"
echo "non_pricing_containers_unchanged=PASS"

echo
echo "===== 6. ENDPOINT CERTIFICATION ====="
LOCAL="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8009/api/health || true)"
PUBLIC="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 10 https://api.desifaces.ai/pricing/api/health || true)"
echo "local_pricing_health=$LOCAL"
echo "public_pricing_health=$PUBLIC"
[[ "$LOCAL" == "200" ]] || fail "local pricing health=$LOCAL"
[[ "$PUBLIC" == "200" ]] || fail "public pricing health=$PUBLIC"

echo
echo "============================================================"
echo " STRIPE_LIVE_PRICING_RUNTIME=PASS"
echo "============================================================"
echo "pricing_recreated_only=YES"
echo "production_image_changed=NO"
echo "customer_charge=NONE"
