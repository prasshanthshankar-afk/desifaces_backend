#!/usr/bin/env bash
set -Eeuo pipefail

DEV_HOST="${DEV_HOST:-desifaces-dev}"
PROD_HOST="${PROD_HOST:-desifaces-gpu}"
DEV_ROOT="/home/azureuser/workspace/desifaces-v3"
PROD_ROOT="/home/azureuser/workspace/desifaces"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp -d /tmp/desifaces-env-align-${STAMP}.XXXXXX)"
trap 'rm -rf "$TMP" >/dev/null 2>&1 || true' EXIT

fail(){ echo "FAIL: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }
for x in ssh scp python3 diff sort comm; do need "$x"; done
[[ "$(uname -s)" == "Darwin" ]] || fail "run from Mac release environment"

for h in "$DEV_HOST" "$PROD_HOST"; do
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$h" 'hostname -s' >/dev/null || fail "cannot SSH to $h"
done

echo "============================================================"
echo " desifaces.ai — DEV ↔ PROD ENV SEMANTIC ALIGNMENT"
echo "============================================================"
echo "MODE=GUARDED"
echo "CUSTOMER_DATA_ACTION=NONE"
echo "DB_REDIS_RECREATE=FORBIDDEN"
echo "SECRETS_COPY=FORBIDDEN"
echo "PRODUCTION_SPECIFIC_SETTINGS=PRESERVED"

# Get sanitized snapshots. Secret values are never emitted.
snapshot(){
  local host="$1" root="$2" out="$3"
  ssh "$host" "ROOT='$root' python3 - <<'PY'
from pathlib import Path
import os,re
p=Path(os.environ['ROOT'])/'infra/.env'
if not p.exists(): raise SystemExit('env missing: '+str(p))
secret=re.compile(r'(SECRET|PASSWORD|TOKEN|PRIVATE|CREDENTIAL|API_KEY|ACCESS_KEY|SIGNING|DATABASE_URL|REDIS_URL|CONNECTION_STRING|SAS|STRIPE|APPLE|GOOGLE|SMTP_PASSWORD)',re.I)
vals={}
for raw in p.read_text(errors='replace').splitlines():
    s=raw.strip()
    if not s or s.startswith('#') or '=' not in s: continue
    k,v=s.split('=',1); k=k.strip(); v=v.strip().strip(chr(34)).strip(chr(39)); vals[k]=v
for k in sorted(vals):
    if secret.search(k): print(f'SECRET|{k}|'+('SET' if vals[k] else 'EMPTY'))
    else: print(f'SAFE|{k}|{vals[k]}')
PY" > "$out"
}
snapshot "$DEV_HOST" "$DEV_ROOT" "$TMP/dev.env"
snapshot "$PROD_HOST" "$PROD_ROOT" "$TMP/prod.env"

echo
echo "===== 1. KEY PARITY ====="
cut -d'|' -f2 "$TMP/dev.env" | sort -u > "$TMP/dev.keys"
cut -d'|' -f2 "$TMP/prod.env" | sort -u > "$TMP/prod.keys"
comm -23 "$TMP/dev.keys" "$TMP/prod.keys" | sed 's/^/PROD_MISSING_KEY=/' || true
comm -13 "$TMP/dev.keys" "$TMP/prod.keys" | sed 's/^/PROD_EXTRA_KEY=/' || true

echo
echo "===== 2. IMAGE CONFIG BEFORE ====="
for k in F_IMAGE_PROVIDER_DEFAULT OPENAI_IMAGE_MODEL_T2I OPENAI_IMAGE_MODEL_EDIT OPENAI_IMAGE_SIZE OPENAI_IMAGE_QUALITY; do
  d="$(awk -F'|' -v K="$k" '$1=="SAFE"&&$2==K{print $3;exit}' "$TMP/dev.env")"
  p="$(awk -F'|' -v K="$k" '$1=="SAFE"&&$2==K{print $3;exit}' "$TMP/prod.env")"
  echo "$k|dev=${d:-<unset>}|prod=${p:-<unset>}"
done

# Build patch strictly from dev for approved shared non-secret runtime keys.
python3 - "$TMP/dev.env" "$TMP/patch.tsv" <<'PY'
import sys
allow={
 'F_IMAGE_PROVIDER_DEFAULT','OPENAI_IMAGE_MODEL_T2I','OPENAI_IMAGE_MODEL_EDIT','OPENAI_IMAGE_SIZE','OPENAI_IMAGE_QUALITY',
 'AUDIO_OUTPUT_CONTAINER','AZURE_AUDIO_CONTAINER','FACE_INPUT_CONTAINER','FACE_OUTPUT_CONTAINER','OPENAI_IMAGE_MODERATION'
}
vals={}
for line in open(sys.argv[1]):
    parts=line.rstrip('\n').split('|',2)
    if len(parts)==3 and parts[0]=='SAFE': vals[parts[1]]=parts[2]
with open(sys.argv[2],'w') as f:
    for k in sorted(allow):
        if vals.get(k,'')!='': f.write(k+'\t'+vals[k]+'\n')
PY

scp -q "$TMP/patch.tsv" "$PROD_HOST:/tmp/desifaces-env-align-$STAMP.tsv"

echo
echo "===== 3. GUARDED PRODUCTION PATCH ====="
ssh "$PROD_HOST" "ROOT='$PROD_ROOT' STAMP='$STAMP' PATCH='/tmp/desifaces-env-align-$STAMP.tsv' bash -s" <<'REMOTE'
set -Eeuo pipefail
ENV="$ROOT/infra/.env"
[[ -f "$ENV" ]] || { echo 'FAIL: canonical prod env missing'; exit 2; }
[[ -f "$ROOT/docker-compose.yml" ]] || { echo 'FAIL: canonical compose missing'; exit 2; }
OVERLAY="$ROOT/deploy/production/docker-compose.v3-app.production.yml"
[[ -f "$OVERLAY" ]] || { echo 'FAIL: production overlay missing'; exit 2; }

BACKUP="/home/azureuser/backups/desifaces-env-align-$STAMP"
mkdir -p "$BACKUP"
cp "$ENV" "$BACKUP/infra.env.before"
sha256sum "$ENV" > "$BACKUP/infra.env.before.sha256"
DB_ID="$(docker inspect -f '{{.Id}}' desifaces-db)"
REDIS_ID="$(docker inspect -f '{{.Id}}' desifaces-redis)"
WEB_ID="$(docker inspect -f '{{.Id}}' df-v3-web-prod 2>/dev/null || true)"

echo "BACKUP_DIR=$BACKUP"

python3 - "$ENV" "$PATCH" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); patch=Path(sys.argv[2])
updates={}
for raw in patch.read_text().splitlines():
    if not raw.strip(): continue
    k,v=raw.split('\t',1); updates[k]=v
lines=p.read_text().splitlines()
seen=set(); out=[]
for line in lines:
    s=line.strip()
    if s and not s.startswith('#') and '=' in s:
        k=s.split('=',1)[0].strip()
        if k in updates:
            out.append(f'{k}={updates[k]}'); seen.add(k); continue
    out.append(line)
for k in sorted(updates):
    if k not in seen: out.append(f'{k}={updates[k]}')
p.write_text('\n'.join(out)+'\n')
PY

# Verify file values before any service recreation.
for spec in \
  'F_IMAGE_PROVIDER_DEFAULT=openai' \
  'OPENAI_IMAGE_MODEL_T2I=gpt-image-2' \
  'OPENAI_IMAGE_MODEL_EDIT=gpt-image-2'; do
  grep -Fxq "$spec" "$ENV" || { echo "FAIL: expected env value missing: $spec"; cp "$BACKUP/infra.env.before" "$ENV"; exit 3; }
done

echo "PRODUCTION_ENV_FILE_ALIGN=PASS"

# Resolve only currently-running compose services that actually receive image model env.
mapfile -t SERVICES < <(
  for c in $(docker ps --format '{{.Names}}'); do
    envs="$(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null || true)"
    if printf '%s\n' "$envs" | grep -q '^OPENAI_IMAGE_MODEL_'; then
      docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.service"}}' 2>/dev/null || true
    fi
  done | awk 'NF' | sort -u
)
[[ ${#SERVICES[@]} -gt 0 ]] || { echo 'FAIL: no image-model compose services discovered'; cp "$BACKUP/infra.env.before" "$ENV"; exit 3; }
printf 'IMAGE_ENV_SERVICES=%s\n' "${SERVICES[*]}"

cd "$ROOT"
docker compose --env-file "$ENV" -f docker-compose.yml -f deploy/production/docker-compose.v3-app.production.yml config >/tmp/desifaces-env-align-compose-$STAMP.yml
# hard gate: rendered compose must contain new model and must not render 1.5 for these keys
if grep -E 'OPENAI_IMAGE_MODEL_(T2I|EDIT):[[:space:]]*gpt-image-1\.5' /tmp/desifaces-env-align-compose-$STAMP.yml; then
  echo 'FAIL: rendered compose still contains gpt-image-1.5'; cp "$BACKUP/infra.env.before" "$ENV"; exit 3
fi
grep -q 'OPENAI_IMAGE_MODEL_T2I: gpt-image-2' /tmp/desifaces-env-align-compose-$STAMP.yml || { echo 'FAIL: rendered T2I not gpt-image-2'; cp "$BACKUP/infra.env.before" "$ENV"; exit 3; }
grep -q 'OPENAI_IMAGE_MODEL_EDIT: gpt-image-2' /tmp/desifaces-env-align-compose-$STAMP.yml || { echo 'FAIL: rendered EDIT not gpt-image-2'; cp "$BACKUP/infra.env.before" "$ENV"; exit 3; }
echo 'COMPOSE_IMAGE_MODEL_PARITY=PASS'

# Recreate only services that consume image model env. Never include DB/Redis/web.
for forbidden in db redis postgres web; do
  for s in "${SERVICES[@]}"; do
    [[ "$s" != *"$forbidden"* ]] || { echo "FAIL: forbidden service discovered: $s"; cp "$BACKUP/infra.env.before" "$ENV"; exit 3; }
  done
done

docker compose --env-file "$ENV" -f docker-compose.yml -f deploy/production/docker-compose.v3-app.production.yml up -d --no-deps --force-recreate "${SERVICES[@]}"

[[ "$(docker inspect -f '{{.Id}}' desifaces-db)" == "$DB_ID" ]] || { echo 'FAIL: DB container changed'; exit 4; }
[[ "$(docker inspect -f '{{.Id}}' desifaces-redis)" == "$REDIS_ID" ]] || { echo 'FAIL: Redis container changed'; exit 4; }
if [[ -n "$WEB_ID" ]]; then [[ "$(docker inspect -f '{{.Id}}' df-v3-web-prod)" == "$WEB_ID" ]] || { echo 'FAIL: web container changed'; exit 4; }; fi

echo 'DB_CONTAINER_UNCHANGED=PASS'
echo 'REDIS_CONTAINER_UNCHANGED=PASS'
echo 'WEB_CONTAINER_UNCHANGED=PASS'

sleep 5
bad=0
for c in $(docker ps --format '{{.Names}}'); do
  envs="$(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null || true)"
  if printf '%s\n' "$envs" | grep -q '^OPENAI_IMAGE_MODEL_'; then
    t="$(printf '%s\n' "$envs" | awk -F= '$1=="OPENAI_IMAGE_MODEL_T2I"{print $2;exit}')"
    e="$(printf '%s\n' "$envs" | awk -F= '$1=="OPENAI_IMAGE_MODEL_EDIT"{print $2;exit}')"
    echo "RUNTIME|$c|T2I=${t:-unset}|EDIT=${e:-unset}"
    [[ "$t" == gpt-image-2 && "$e" == gpt-image-2 ]] || bad=1
  fi
done
[[ $bad -eq 0 ]] || { echo 'FAIL: runtime model parity incomplete'; exit 5; }
echo 'RUNTIME_IMAGE_MODEL_PARITY=PASS'

# Basic health checks without touching data.
for spec in 'core|http://127.0.0.1:8000/api/health' 'audio|http://127.0.0.1:8004/api/health' 'pricing|http://127.0.0.1:8009/api/health'; do
  n="${spec%%|*}"; u="${spec#*|}"; code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "$u" || true)"; echo "HEALTH|$n|$code"; [[ "$code" == 200 ]] || exit 5
done

echo 'PRODUCTION_ENV_ALIGNMENT=PASS'
REMOTE

# Re-snapshot and report remaining non-secret differences without auto-mutating them.
snapshot "$PROD_HOST" "$PROD_ROOT" "$TMP/prod.after.env"

echo
echo "===== 4. IMAGE CONFIG AFTER ====="
for k in F_IMAGE_PROVIDER_DEFAULT OPENAI_IMAGE_MODEL_T2I OPENAI_IMAGE_MODEL_EDIT OPENAI_IMAGE_SIZE OPENAI_IMAGE_QUALITY; do
  d="$(awk -F'|' -v K="$k" '$1=="SAFE"&&$2==K{print $3;exit}' "$TMP/dev.env")"
  p="$(awk -F'|' -v K="$k" '$1=="SAFE"&&$2==K{print $3;exit}' "$TMP/prod.after.env")"
  echo "$k|dev=${d:-<unset>}|prod=${p:-<unset>}"
  [[ "$d" == "$p" ]] || fail "$k still differs"
done

echo "IMAGE_ENV_PARITY=PASS"

echo
echo "===== 5. REMAINING SAFE ENV DIFFERENCES (REPORT ONLY) ====="
python3 - "$TMP/dev.env" "$TMP/prod.after.env" <<'PY'
import sys

def load(p):
 d={}
 for line in open(p):
  a=line.rstrip('\n').split('|',2)
  if len(a)==3 and a[0]=='SAFE': d[a[1]]=a[2]
 return d
a,b=load(sys.argv[1]),load(sys.argv[2])
for k in sorted(set(a)|set(b)):
 if a.get(k)!=b.get(k): print(f'SAFE_DIFF|{k}|dev={a.get(k,"<unset>")}|prod={b.get(k,"<unset>")}')
PY

echo "============================================================"
echo " DEV ↔ PROD ENV ALIGNMENT COMPLETE"
echo "============================================================"
echo "IMAGE_ENV_PARITY=PASS"
echo "SECRET_VALUES_COPIED=NO"
echo "DB_REDIS_WEB_PRESERVED=PASS"
