#!/usr/bin/env bash
set -Eeuo pipefail

REPO="prasshanthshankar-afk/desifaces_backend"
RELEASE_SHA="793db700365e6d0fcbf9345b97737afac497afc0"
SSH_HOST="${SSH_HOST:-desifaces-gpu}"
ROOT="/home/azureuser/workspace/desifaces"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="/tmp/desifaces-backend-final-$STAMP"
PATCH="$RUN/backend-final.tar.gz"
REMOTE_PATCH="/tmp/desifaces-backend-final-$STAMP.tar.gz"

fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in gh git tar ssh scp; do need "$x"; done
[[ "$(uname -s)" == "Darwin" ]] || fail "run from the Mac release environment"
trap 'rm -rf "$RUN" >/dev/null 2>&1 || true' EXIT
mkdir -p "$RUN/repo" "$RUN/patch"

printf '%s\n' "============================================================"
printf '%s\n' " desifaces.ai — FINAL PRODUCTION BACKEND RUNTIME CLOSEOUT"
printf '%s\n' "============================================================"
printf 'release_sha=%s\n' "$RELEASE_SHA"
printf '%s\n' "database_migrations=NONE"
printf '%s\n' "live_db_restore=NONE"
printf '%s\n' "web_action=NONE"
printf '%s\n' "mobile_action=NONE"
printf '%s\n' "director_model=gpt-5.6-sol"
printf '%s\n' "assistant_model=gpt-5.6-terra"

H="$(ssh -o BatchMode=yes -o ConnectTimeout=12 "$SSH_HOST" 'hostname -s')" || fail "cannot SSH to $SSH_HOST"
[[ "$H" == desifaces-gpu* ]] || fail "wrong production host: $H"
ssh "$SSH_HOST" "test -f '$ROOT/RELEASE' && test -f '$ROOT/infra/.env' && docker inspect desifaces-db >/dev/null 2>&1 && docker inspect desifaces-redis >/dev/null 2>&1" || fail "production preservation preflight failed"
printf '%s\n' "PRODUCTION_PRESERVATION_PREFLIGHT=PASS"

printf '%s\n' ""
printf '%s\n' "===== 1. ASSEMBLE IMMUTABLE BACKEND RUNTIME PATCH ON MAC ====="
gh repo clone "$REPO" "$RUN/repo" -- --filter=blob:none --no-checkout >/dev/null
(
  cd "$RUN/repo"
  git checkout --detach "$RELEASE_SHA" >/dev/null
  [[ "$(git rev-parse HEAD)" == "$RELEASE_SHA" ]]
)
FILES=(
  deploy/production/docker-compose.v3-app.production.yml
  services/svc-assistant/app/app/llm.py
  services/svc-assistant/app/app/main.py
  services/svc-assistant/app/app/retrieval.py
  services/svc-assistant/app/tests/test_llm_startup_resilience.py
  services/svc-assistant/app/tests/test_retrieval_startup_resilience.py
  services/svc-assistant/knowledge/pricing.md
  services/svc-assistant/knowledge/privacy_and_support.md
  services/svc-assistant/knowledge/product_basics.md
  services/svc-assistant/knowledge/troubleshooting.md
)
for p in "${FILES[@]}"; do
  mkdir -p "$RUN/patch/$(dirname "$p")"
  cp "$RUN/repo/$p" "$RUN/patch/$p"
done
python3 - "$RUN/patch/services/svc-assistant/knowledge" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
expected={'pricing.md','privacy_and_support.md','product_basics.md','troubleshooting.md'}
seen={p.name for p in root.glob('*.md')}
assert seen == expected, (seen, expected)
for p in root.glob('*.md'):
    p.read_text(encoding='utf-8')
print('CANONICAL_KNOWLEDGE_UTF8=PASS files=4')
PY
COPYFILE_DISABLE=1 tar --no-xattrs -C "$RUN/patch" -czf "$PATCH" . 2>/dev/null || COPYFILE_DISABLE=1 tar -C "$RUN/patch" -czf "$PATCH" .
[[ -s "$PATCH" ]] || fail "runtime patch is empty"
if tar -tzf "$PATCH" | grep -Eq '(^|/)\._'; then fail "AppleDouble metadata present in runtime patch"; fi
printf '%s\n' "BACKEND_RUNTIME_PACKAGE=PASS"

printf '%s\n' ""
printf '%s\n' "===== 2. FINALIZE BACKEND ON PRODUCTION ====="
scp -q "$PATCH" "$SSH_HOST:$REMOTE_PATCH"
ssh -tt "$SSH_HOST" "ROOT='$ROOT' REMOTE_PATCH='$REMOTE_PATCH' STAMP='$STAMP' RELEASE_SHA='$RELEASE_SHA' bash -s" <<'REMOTE'
set -Eeuo pipefail

fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
BACKUP="/home/azureuser/backups/desifaces-backend-runtime-pre-final-$STAMP"
mkdir -p "$BACKUP"

printf '%s\n' "----- A. PRODUCTION SAFETY PRECHECK -----"
[[ "$(hostname -s)" == desifaces-gpu* ]] || fail "wrong host"
[[ -f "$ROOT/infra/.env" ]] || fail "production env missing"
docker inspect desifaces-db >/dev/null 2>&1 || fail "production DB missing"
docker inspect desifaces-redis >/dev/null 2>&1 || fail "production Redis missing"
printf '%s\n' "DATABASE_ACTION=NONE"
printf '%s\n' "REDIS_ACTION=NONE"

printf '%s\n' "----- B. VERIFY OPENAI MODEL ACCESS BEFORE MUTATION -----"
OPENAI_KEY="$(docker inspect df-v3-svc-director --format '{{range .Config.Env}}{{println .}}{{end}}' | awk -F= '$1=="OPENAI_API_KEY"{sub(/^[^=]*=/,""); print; exit}')"
[[ -n "$OPENAI_KEY" ]] || fail "OPENAI_API_KEY not present in Director container"
DF_OPENAI_KEY="$OPENAI_KEY" python3 - <<'PY'
import os, urllib.error, urllib.request
key=os.environ['DF_OPENAI_KEY']
for model in ('gpt-5.6-sol','gpt-5.6-terra'):
    req=urllib.request.Request(
        'https://api.openai.com/v1/models/'+model,
        headers={'Authorization':'Bearer '+key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            code=r.status
    except urllib.error.HTTPError as e:
        raise SystemExit(f'FAIL: OpenAI model access {model} HTTP {e.code}')
    except Exception as e:
        raise SystemExit(f'FAIL: OpenAI model access {model}: {type(e).__name__}')
    if code != 200:
        raise SystemExit(f'FAIL: OpenAI model access {model} HTTP {code}')
    print(f'MODEL_ACCESS_PASS model={model}')
print('OPENAI_MODEL_ACCESS=PASS')
PY
unset OPENAI_KEY

printf '%s\n' "----- C. BACK UP CURRENT BACKEND SOURCE/OVERLAY -----"
FILES=(
  deploy/production/docker-compose.v3-app.production.yml
  services/svc-assistant/app/app/llm.py
  services/svc-assistant/app/app/main.py
  services/svc-assistant/app/app/retrieval.py
  services/svc-assistant/app/tests/test_llm_startup_resilience.py
  services/svc-assistant/app/tests/test_retrieval_startup_resilience.py
  services/svc-assistant/knowledge/pricing.md
  services/svc-assistant/knowledge/privacy_and_support.md
  services/svc-assistant/knowledge/product_basics.md
  services/svc-assistant/knowledge/troubleshooting.md
)
for p in "${FILES[@]}"; do
  if [[ -f "$ROOT/$p" ]]; then mkdir -p "$BACKUP/$(dirname "$p")"; cp -p "$ROOT/$p" "$BACKUP/$p"; fi
done
cp -p "$ROOT/RELEASE" "$BACKUP/RELEASE"
printf 'SOURCE_BACKUP=PASS path=%s\n' "$BACKUP"

printf '%s\n' "----- D. APPLY CERTIFIED RUNTIME PATCH -----"
tar -xzf "$REMOTE_PATCH" -C "$ROOT"
rm -f "$REMOTE_PATCH"
find "$ROOT/services/svc-assistant/knowledge" -maxdepth 1 -type f -name '._*' -delete
python3 - "$ROOT/services/svc-assistant/knowledge" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
expected={'pricing.md','privacy_and_support.md','product_basics.md','troubleshooting.md'}
seen={p.name for p in root.glob('*.md')}
assert seen == expected, (seen, expected)
for p in root.glob('*.md'):
    p.read_text(encoding='utf-8')
assert not list(root.glob('._*'))
print('PRODUCTION_CANONICAL_KNOWLEDGE=PASS files=4 apple_double=0')
PY

COMPOSE=(docker compose --env-file "$ROOT/infra/.env" -f "$ROOT/docker-compose.yml" -f "$ROOT/deploy/production/docker-compose.v3-app.production.yml")
"${COMPOSE[@]}" config >/tmp/desifaces-backend-final-compose.yml
python3 - /tmp/desifaces-backend-final-compose.yml <<'PY'
from pathlib import Path
text=Path('/tmp/desifaces-backend-final-compose.yml').read_text()
for token in ('gpt-5.6-sol','gpt-5.6-terra'):
    if token not in text:
        raise SystemExit('FAIL: rendered compose missing '+token)
print('RENDERED_MODEL_CONFIG=PASS')
PY

printf '%s\n' "----- E. BUILD ASSISTANT ONCE; RECREATE ONLY DIRECTOR + PIKU -----"
"${COMPOSE[@]}" build svc-assistant
"${COMPOSE[@]}" up -d --no-deps --force-recreate svc-director svc-director-worker svc-assistant
printf '%s\n' "BACKEND_RUNTIME_RECREATE=APPLIED services=svc-director,svc-director-worker,svc-assistant"

wait_http(){
  local name="$1" url="$2" max="${3:-60}" i code
  for ((i=1;i<=max;i++)); do
    code="$(curl -sS --max-time 4 -o /tmp/df-backend-final-health.json -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [[ "$code" == 200 ]]; then printf 'PASS %s %s\n' "$name" "$url"; return 0; fi
    sleep 2
  done
  return 1
}
wait_http director http://127.0.0.1:18011/api/health 45 || { docker logs --tail 100 df-v3-svc-director 2>&1 || true; fail "Director health failed"; }
wait_http assistant http://127.0.0.1:18012/api/health 45 || { docker logs --tail 100 df-v3-svc-assistant 2>&1 || true; fail "Assistant health failed"; }

curl -fsS http://127.0.0.1:18011/api/health >/tmp/df-director-final.json
curl -fsS http://127.0.0.1:18012/api/health >/tmp/df-assistant-final.json
python3 - <<'PY'
import json
D=json.load(open('/tmp/df-director-final.json'))
A=json.load(open('/tmp/df-assistant-final.json'))
print('DIRECTOR_HEALTH='+json.dumps(D,sort_keys=True))
print('ASSISTANT_HEALTH='+json.dumps(A,sort_keys=True))
assert D.get('llm_configured') is True and D.get('runtime_ready') is True, D
assert A.get('llm_configured') is True and A.get('runtime_ready') is True, A
assert A.get('knowledge_ready') is True, A
assert A.get('knowledge_files_loaded') == 4, A
assert A.get('knowledge_files_skipped') == 0, A
print('DIRECTOR_ASSISTANT_RUNTIME=PASS')
PY

printf '%s\n' "----- F. VERIFY UNCHANGED BACKEND SERVICES -----"
for spec in \
  'core|http://127.0.0.1:8000/api/health' \
  'fusion|http://127.0.0.1:8002/api/health' \
  'face|http://127.0.0.1:8003/api/health' \
  'audio|http://127.0.0.1:8004/api/health' \
  'dashboard|http://127.0.0.1:8005/api/health' \
  'fusion-extension|http://127.0.0.1:8006/api/health' \
  'pricing|http://127.0.0.1:8009/api/health'; do
  name="${spec%%|*}"; url="${spec#*|}"; wait_http "$name" "$url" 15 || fail "$name health failed"
done
printf '%s\n' "BACKEND_SERVICE_HEALTH=PASS"

python3 - "$ROOT/RELEASE" "$RELEASE_SHA" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); sha=sys.argv[2]
lines=p.read_text().splitlines()
keys={
 'backend_runtime_release_sha': sha,
 'director_model': 'gpt-5.6-sol',
 'assistant_model': 'gpt-5.6-terra',
}
out=[]; seen=set()
for line in lines:
    k=line.split('=',1)[0] if '=' in line else ''
    if k in keys:
        out.append(k+'='+keys[k]); seen.add(k)
    else:
        out.append(line)
for k,v in keys.items():
    if k not in seen: out.append(k+'='+v)
p.write_text('\n'.join(out).rstrip()+'\n')
PY

printf '%s\n' "============================================================"
printf '%s\n' " PRODUCTION BACKEND RUNTIME CLOSEOUT PASS"
printf '%s\n' "============================================================"
printf '%s\n' "DATABASE_ACTION=NONE"
printf '%s\n' "PRODUCTION_DATABASE_PRESERVED=PASS"
printf '%s\n' "PRODUCTION_BACKEND=PASS"
printf '%s\n' "BACKEND_FROZEN_FOR_WEB_PHASE=YES"
REMOTE

printf '%s\n' "============================================================"
printf '%s\n' " BACKEND FINALIZATION PASS"
printf '%s\n' "============================================================"
printf '%s\n' "PRODUCTION_DATABASE_PRESERVED=PASS"
printf '%s\n' "PRODUCTION_BACKEND=PASS"
printf '%s\n' "NEXT_PHASE=WEB"
