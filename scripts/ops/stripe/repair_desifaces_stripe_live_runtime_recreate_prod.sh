#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE="/home/azureuser/workspace/desifaces/infra/.env"
C="df-svc-pricing"

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
[[ -f "$ENV_FILE" ]] || fail "production env file missing"
docker inspect "$C" >/dev/null 2>&1 || fail "$C missing"
[[ "$(docker inspect -f '{{.State.Status}}' "$C")" == "running" ]] || fail "$C is not currently running"

ENV_GATE="$(python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys
vals={}
for line in Path(sys.argv[1]).read_text().splitlines():
    s=line.strip()
    if not s or s.startswith('#') or '=' not in line:
        continue
    k,v=line.split('=',1)
    vals[k.strip()]=v.strip()
ok=(
    vals.get('STRIPE_SECRET_KEY','').startswith('sk_live_') and
    vals.get('STRIPE_PUBLISHABLE_KEY','').startswith('pk_live_') and
    vals.get('STRIPE_WEBHOOK_SECRET','').startswith('whsec_') and
    vals.get('DF_PAYMENT_GATEWAY_ENABLED','').lower() in {'1','true','yes','on'} and
    vals.get('DF_PAYMENT_GATEWAY_PROVIDER','').lower() == 'stripe'
)
print('PASS' if ok else 'FAIL')
PY
)"
[[ "$ENV_GATE" == "PASS" ]] || fail "production env does not contain complete Stripe LIVE runtime values"

echo "============================================================"
echo " desifaces — STRIPE LIVE PRICING RECREATE REPAIR"
echo " secrets_printed=NO"
echo " existing_live_env_gate=PASS"
echo " customer_charge=NONE"
echo "============================================================"

SERVICE="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$C")"
WORKDIR="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$C")"
CONFIG_FILES="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$C")"
PROJECT="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$C")"

[[ -n "$SERVICE" && "$SERVICE" != "<no value>" ]] || fail "compose service label missing"
[[ -n "$WORKDIR" && "$WORKDIR" != "<no value>" ]] || fail "compose working_dir label missing"
[[ -n "$CONFIG_FILES" && "$CONFIG_FILES" != "<no value>" ]] || fail "compose config_files label missing"
[[ -n "$PROJECT" && "$PROJECT" != "<no value>" ]] || fail "compose project label missing"

echo "compose_service=$SERVICE"
echo "compose_project=$PROJECT"
echo "compose_workdir=$WORKDIR"
echo "compose_config_files=$CONFIG_FILES"

IFS=',' read -r -a FILES <<< "$CONFIG_FILES"
COMPOSE=(docker compose --project-name "$PROJECT" --env-file "$ENV_FILE")
for f in "${FILES[@]}"; do
  [[ -f "$f" ]] || fail "compose file missing: $f"
  COMPOSE+=(-f "$f")
done

cd "$WORKDIR"
"${COMPOSE[@]}" config --services | grep -Fxq "$SERVICE" || fail "derived compose service not present"
echo "COMPOSE_DISCOVERY_GATE=PASS"

BEFORE_OTHERS="$(docker ps -a --format '{{.Names}}|{{.ID}}' | grep -v '^df-svc-pricing|' | sort | sha256sum | awk '{print $1}')"
OLD_ID="$(docker inspect -f '{{.Id}}' "$C")"

echo "===== RECREATE DERIVED PRICING SERVICE ONLY ====="
"${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" >/dev/null

STATE=""
HEALTH=""
for _ in $(seq 1 36); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$C" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$C" 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    break
  fi
  sleep 5
done
[[ "$STATE" == "running" ]] || fail "pricing container not running"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "pricing container unhealthy: $HEALTH"

NEW_ID="$(docker inspect -f '{{.Id}}' "$C")"
[[ "$NEW_ID" != "$OLD_ID" ]] || fail "pricing container was not recreated"
AFTER_OTHERS="$(docker ps -a --format '{{.Names}}|{{.ID}}' | grep -v '^df-svc-pricing|' | sort | sha256sum | awk '{print $1}')"
[[ "$AFTER_OTHERS" == "$BEFORE_OTHERS" ]] || fail "non-pricing container identity changed"

MODE_GATE="$(docker exec "$C" python - <<'PY'
import os
ok=(
 os.getenv('STRIPE_SECRET_KEY','').startswith('sk_live_') and
 os.getenv('STRIPE_PUBLISHABLE_KEY','').startswith('pk_live_') and
 os.getenv('STRIPE_WEBHOOK_SECRET','').startswith('whsec_') and
 os.getenv('DF_PAYMENT_GATEWAY_ENABLED','').lower() in {'1','true','yes','on'} and
 os.getenv('DF_PAYMENT_GATEWAY_PROVIDER','').lower() == 'stripe'
)
print('PASS' if ok else 'FAIL')
PY
)"
[[ "$MODE_GATE" == "PASS" ]] || fail "pricing runtime is not in Stripe LIVE mode"

PUBLIC_CODE="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 15 https://api.desifaces.ai/pricing/api/health || true)"
[[ "$PUBLIC_CODE" == "200" ]] || fail "public pricing health=$PUBLIC_CODE"

echo "pricing_state=$STATE"
echo "pricing_health=$HEALTH"
echo "pricing_runtime_live_mode=PASS"
echo "non_pricing_containers_unchanged=PASS"
echo "public_pricing_health=$PUBLIC_CODE"
echo "STRIPE_LIVE_RUNTIME_REPAIR=PASS"
echo "customer_charge=NONE"
