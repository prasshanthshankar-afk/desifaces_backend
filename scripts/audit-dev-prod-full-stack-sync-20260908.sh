#!/usr/bin/env bash
set -Eeuo pipefail

DEV_HOST="${DEV_HOST:-desifaces-dev}"
PROD_HOST="${PROD_HOST:-desifaces-gpu}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" >/dev/null 2>&1 || true' EXIT

fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in ssh diff awk sed sort comm sha256sum; do need "$x"; done
[[ "$(uname -s)" == "Darwin" ]] || fail "run from the Mac release environment"

printf '%s\n' "============================================================"
printf '%s\n' " desifaces.ai — DEV ↔ PROD FULL STACK SYNC AUDIT"
printf '%s\n' "============================================================"
printf '%s\n' "MODE=READ_ONLY"
printf '%s\n' "DATABASE_MUTATION=NONE"
printf '%s\n' "CONTAINER_MUTATION=NONE"
printf '%s\n' "SOURCE_MUTATION=NONE"

for host in "$DEV_HOST" "$PROD_HOST"; do
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$host" 'hostname -s' >/dev/null || fail "cannot SSH to $host"
done

remote_snapshot(){
  local host="$1" role="$2" outfile="$3"
  ssh "$host" "ROLE='$role' bash -s" > "$outfile" <<'REMOTE'
set -Eeuo pipefail

ROLE="${ROLE:?}"
if [[ "$ROLE" == "prod" ]]; then
  ROOT="/home/azureuser/workspace/desifaces"
else
  ROOT=""
  for p in \
    /home/azureuser/workspace/desifaces-v3 \
    /home/azureuser/workspace/desifaces \
    /home/azureuser/workspace/desifaces-v2; do
    if [[ -f "$p/infra/.env" ]]; then ROOT="$p"; break; fi
  done
fi
[[ -n "$ROOT" && -f "$ROOT/infra/.env" ]] || { echo "FATAL|workspace_or_env_missing"; exit 2; }
ENV_FILE="$ROOT/infra/.env"

printf 'META|ROLE|%s\n' "$ROLE"
printf 'META|HOST|%s\n' "$(hostname -s)"
printf 'META|ROOT|%s\n' "$ROOT"

# Git/source state. Production is intentionally allowed to be non-git.
if [[ -d "$ROOT/.git" ]]; then
  printf 'SOURCE|GIT_HEAD|%s\n' "$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
  printf 'SOURCE|GIT_BRANCH|%s\n' "$(git -C "$ROOT" branch --show-current 2>/dev/null || true)"
  if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null || true)" ]]; then
    printf 'SOURCE|TRACKED_TREE|DIRTY\n'
  else
    printf 'SOURCE|TRACKED_TREE|CLEAN\n'
  fi
else
  printf 'SOURCE|GIT_HEAD|NON_GIT_RELEASE\n'
fi
if [[ -f "$ROOT/RELEASE" ]]; then
  while IFS= read -r line; do
    case "$line" in
      backend_*|web_sha=*|director_model=*|assistant_model=*) printf 'RELEASE|%s\n' "$line" ;;
    esac
  done < "$ROOT/RELEASE"
fi

# Sanitized env snapshot. Never print secret values.
python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1])
vals={}
for raw in p.read_text(encoding='utf-8', errors='replace').splitlines():
    s=raw.strip()
    if not s or s.startswith('#') or '=' not in s: continue
    k,v=s.split('=',1)
    vals[k.strip()]=v.strip().strip('"').strip("'")
secret=re.compile(r'(SECRET|PASSWORD|TOKEN|PRIVATE|CREDENTIAL|API_KEY|ACCESS_KEY|SIGNING|DATABASE_URL|REDIS_URL|CONNECTION_STRING|SAS|STRIPE|APPLE|GOOGLE|SMTP_PASSWORD)',re.I)
for k in sorted(vals):
    v=vals[k]
    if secret.search(k):
        print(f'ENVSECRET|{k}|'+('SET' if v else 'EMPTY'))
    else:
        print(f'ENVSAFE|{k}|{v}')
PY

# Locate DB container without assuming dev/prod naming.
DB_C=""
for c in desifaces-db desifaces-v3-db; do
  if docker inspect "$c" >/dev/null 2>&1; then DB_C="$c"; break; fi
done
if [[ -z "$DB_C" ]]; then
  DB_C="$(docker ps --format '{{.Names}}' | grep -E '(^|-)db$|desifaces.*db' | head -1 || true)"
fi
[[ -n "$DB_C" ]] || { echo 'FATAL|db_container_missing'; exit 3; }
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"
printf 'DB|CONTAINER|%s\n' "$DB_C"
printf 'DB|NAME|%s\n' "$DB_NAME"

psqlq(){ docker exec "$DB_C" psql -X -A -t -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c "$1"; }

# Schema fingerprint: table/column/type/nullability only; no customer data.
SCHEMA_SIG="$(psqlq "
SELECT md5(string_agg(x, E'\\n' ORDER BY x))
FROM (
  SELECT table_schema||'.'||table_name||'|'||column_name||'|'||data_type||'|'||is_nullable AS x
  FROM information_schema.columns
  WHERE table_schema IN ('public','core')
) s;")"
printf 'DBSIG|SCHEMA|%s\n' "${SCHEMA_SIG//[[:space:]]/}"

# Deterministic masterdata fingerprints excluding volatile timestamps.
for tbl in \
  public.face_generation_regions \
  public.tts_languages \
  public.tts_locales \
  public.tts_voices \
  public.tts_voice_locale_capabilities \
  public.tts_voice_model_capabilities \
  public.tts_model_locale_capabilities \
  public.tts_model_language_capabilities \
  public.tts_provider_models \
  public.tts_providers \
  public.pricing_skus \
  public.pricing_packages; do
  exists="$(psqlq "select to_regclass('$tbl') is not null;")"
  if [[ "$exists" != "t" ]]; then
    printf 'DBMD|%s|MISSING\n' "$tbl"
    continue
  fi
  count="$(psqlq "select count(*) from $tbl;")"; count="${count//[[:space:]]/}"
  sig="$(docker exec "$DB_C" psql -X -A -t -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c "
    SELECT md5(string_agg(j, E'\\n' ORDER BY j))
    FROM (
      SELECT (to_jsonb(t)
        - 'created_at' - 'updated_at' - 'discovered_at' - 'last_seen_at'
        - 'last_synced_at' - 'refreshed_at')::text AS j
      FROM $tbl t
    ) q;")"
  sig="${sig//[[:space:]]/}"
  printf 'DBMD|%s|count=%s|sig=%s\n' "$tbl" "$count" "$sig"
done

# Runtime service inventory and selected parity-critical envs.
for c in $(docker ps --format '{{.Names}}' | sort); do
  case "$c" in
    *svc-face*|*svc-audio*|*svc-core*|*svc-fusion*|*svc-pricing*|*svc-dashboard*|*svc-director*|*svc-assistant*)
      image="$(docker inspect "$c" --format '{{.Image}}' 2>/dev/null || true)"
      printf 'RUNTIME|CONTAINER|%s|IMAGE=%s\n' "$c" "$image"
      envs="$(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null || true)"
      for k in OPENAI_IMAGE_MODEL_T2I OPENAI_IMAGE_MODEL_EDIT DF_DIRECTOR_LLM_MODEL DF_ASSISTANT_LLM_MODEL; do
        v="$(printf '%s\n' "$envs" | awk -F= -v K="$k" '$1==K{sub(/^[^=]*=/,"");print;exit}')"
        [[ -n "$v" ]] && printf 'RUNTIMEENV|%s|%s|%s\n' "$c" "$k" "$v"
      done
      ;;
  esac
done

# Live contract probes where ports are known.
for spec in \
  'core|http://127.0.0.1:8000/api/health' \
  'audio|http://127.0.0.1:8004/api/health' \
  'pricing|http://127.0.0.1:8009/api/health'; do
  n="${spec%%|*}"; u="${spec#*|}"
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 "$u" 2>/dev/null || true)"
  printf 'HEALTH|%s|%s\n' "$n" "$code"
done
countries="$(curl -fsS --max-time 5 http://127.0.0.1:8004/api/audio/catalog/countries 2>/dev/null || true)"
[[ -n "$countries" ]] && printf 'CONTRACT|AUDIO_COUNTRIES|%s\n' "$countries"
REMOTE
}

DEV="$TMP/dev.txt"; PROD="$TMP/prod.txt"
remote_snapshot "$DEV_HOST" dev "$DEV"
remote_snapshot "$PROD_HOST" prod "$PROD"

echo
echo "===== 1. WORKSPACE / RELEASE STATE ====="
grep -E '^(META|SOURCE|RELEASE)\|' "$DEV" || true
grep -E '^(META|SOURCE|RELEASE)\|' "$PROD" || true

echo
echo "===== 2. infra/.env KEY PARITY ====="
grep '^ENVSECRET|' "$DEV" | cut -d'|' -f2 | sort -u > "$TMP/dev.secret.keys"
grep '^ENVSECRET|' "$PROD" | cut -d'|' -f2 | sort -u > "$TMP/prod.secret.keys"
grep '^ENVSAFE|' "$DEV" | cut -d'|' -f2 | sort -u > "$TMP/dev.safe.keys"
grep '^ENVSAFE|' "$PROD" | cut -d'|' -f2 | sort -u > "$TMP/prod.safe.keys"
cat "$TMP/dev.secret.keys" "$TMP/dev.safe.keys" | sort -u > "$TMP/dev.keys"
cat "$TMP/prod.secret.keys" "$TMP/prod.safe.keys" | sort -u > "$TMP/prod.keys"
comm -23 "$TMP/dev.keys" "$TMP/prod.keys" > "$TMP/prod.missing.keys"
comm -13 "$TMP/dev.keys" "$TMP/prod.keys" > "$TMP/prod.extra.keys"
if [[ -s "$TMP/prod.missing.keys" ]]; then
  echo "ENV_KEY_PARITY=FAIL"
  sed 's/^/PROD_MISSING_KEY=/' "$TMP/prod.missing.keys"
else
  echo "ENV_KEY_PARITY=PASS"
fi
[[ -s "$TMP/prod.extra.keys" ]] && sed 's/^/PROD_EXTRA_KEY=/' "$TMP/prod.extra.keys" || true

echo
echo "===== 3. PARITY-CRITICAL SAFE ENV VALUES ====="
for k in \
  OPENAI_IMAGE_MODEL_T2I OPENAI_IMAGE_MODEL_EDIT \
  DF_DIRECTOR_LLM_MODEL DF_ASSISTANT_LLM_MODEL \
  DF_V3_CANONICAL_ADAPTER_SHADOW_ENABLED \
  DF_SUBSCRIPTION_RECONCILER_ENABLED \
  FUSION_RECOVERY_ENABLED; do
  dv="$(awk -F'|' -v K="$k" '$1=="ENVSAFE" && $2==K {print $3;exit}' "$DEV")"
  pv="$(awk -F'|' -v K="$k" '$1=="ENVSAFE" && $2==K {print $3;exit}' "$PROD")"
  printf '%s|dev=%s|prod=%s\n' "$k" "${dv:-<unset>}" "${pv:-<unset>}"
  [[ "$dv" == "$pv" ]] || true
done

echo
echo "===== 4. SECRET PRESENCE PARITY — VALUES NEVER PRINTED ====="
for k in OPENAI_API_KEY JWT_SECRET DATABASE_URL REDIS_URL; do
  dv="$(awk -F'|' -v K="$k" '$1=="ENVSECRET" && $2==K {print $3;exit}' "$DEV")"
  pv="$(awk -F'|' -v K="$k" '$1=="ENVSECRET" && $2==K {print $3;exit}' "$PROD")"
  printf '%s|dev=%s|prod=%s\n' "$k" "${dv:-MISSING}" "${pv:-MISSING}"
done

echo
echo "===== 5. DB SCHEMA + MASTERDATA PARITY ====="
grep '^DBSIG|' "$DEV"; grep '^DBSIG|' "$PROD"
grep '^DBMD|' "$DEV" > "$TMP/dev.dbmd"
grep '^DBMD|' "$PROD" > "$TMP/prod.dbmd"
if diff -u "$TMP/dev.dbmd" "$TMP/prod.dbmd"; then
  echo "DB_MASTERDATA_PARITY=PASS"
else
  echo "DB_MASTERDATA_PARITY=FAIL"
fi
DEV_SCHEMA="$(awk -F'|' '$1=="DBSIG"&&$2=="SCHEMA"{print $3}' "$DEV")"
PROD_SCHEMA="$(awk -F'|' '$1=="DBSIG"&&$2=="SCHEMA"{print $3}' "$PROD")"
[[ -n "$DEV_SCHEMA" && "$DEV_SCHEMA" == "$PROD_SCHEMA" ]] && echo "DB_SCHEMA_PARITY=PASS" || echo "DB_SCHEMA_PARITY=FAIL"

echo
echo "===== 6. RUNTIME MODEL / SERVICE PARITY ====="
echo "--- DEV ---"; grep -E '^(RUNTIMEENV|HEALTH|CONTRACT)\|' "$DEV" || true
echo "--- PROD ---"; grep -E '^(RUNTIMEENV|HEALTH|CONTRACT)\|' "$PROD" || true

echo
echo "===== 7. PUBLIC WEB / API CONTRACT ====="
for u in \
  https://web.desifaces.ai/auth/login \
  https://api.desifaces.ai/director/api/health \
  https://api.desifaces.ai/assistant/api/health; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "$u" 2>/dev/null || true)"
  printf '%s HTTP=%s\n' "$u" "$code"
done

echo
echo "===== 8. MOBILE / WEB SOURCE RELEASE REFERENCES ====="
echo "WEB_CERTIFIED_SHA=e51de0181bc9dd74c4ace4ec5ab8891f26be83d2"
echo "MOBILE_TESTFLIGHT_SOURCE_SHA=55f0856a4b5b99dd877b764d86b7b68f1ab77459"
echo "MOBILE_FOOTER_FIX_SHA=ca4db97a172d6bdad64218a4b32b499dee47f864"
if command -v gh >/dev/null 2>&1; then
  web_head="$(gh api repos/prasshanthshankar-afk/desifaces_web/branches/release/web-production-launch-20260903-piku-media --jq .commit.sha 2>/dev/null || true)"
  mobile_fix_head="$(gh api repos/prasshanthshankar-afk/desifaces_frontend/branches/fix/mobile-v3-parity-footer-audio-20260904 --jq .commit.sha 2>/dev/null || true)"
  echo "WEB_RELEASE_BRANCH_HEAD=${web_head:-UNKNOWN}"
  echo "MOBILE_FIX_BRANCH_HEAD=${mobile_fix_head:-UNKNOWN}"
fi

echo
echo "============================================================"
if [[ ! -s "$TMP/prod.missing.keys" && "$DEV_SCHEMA" == "$PROD_SCHEMA" ]] && diff -q "$TMP/dev.dbmd" "$TMP/prod.dbmd" >/dev/null 2>&1; then
  echo "FULL_STACK_SYNC_CORE=PASS"
else
  echo "FULL_STACK_SYNC_CORE=FAIL"
fi
echo "AUDIT_COMPLETE=YES"
echo "NO_CHANGES_APPLIED=YES"
echo "============================================================"
