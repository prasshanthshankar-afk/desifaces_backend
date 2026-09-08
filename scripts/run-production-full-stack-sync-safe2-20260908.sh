#!/usr/bin/env bash
set -Eeuo pipefail

REPO="prasshanthshankar-afk/desifaces_backend"
REF="audit/full-stack-sync-20260908"
SRC="scripts/apply-production-full-stack-sync-20260908.sh"
DEV_HOST="${DEV_HOST:-desifaces-dev}"
DEV_ROOT="/home/azureuser/workspace/desifaces-v3"
TMP="$(mktemp /tmp/desifaces-full-sync-safe2.XXXXXX.sh)"
trap 'rm -f "$TMP"' EXIT

need(){ command -v "$1" >/dev/null 2>&1 || { echo "FAIL: missing required command: $1" >&2; exit 2; }; }
for x in gh python3 ssh base64 tr; do need "$x"; done
[[ "$(uname -s)" == "Darwin" ]] || { echo "FAIL: run from Mac release environment" >&2; exit 2; }

gh api "repos/$REPO/contents/$SRC?ref=$REF" --jq .content | base64 -d > "$TMP"

python3 - "$TMP" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1])
s=p.read_text()

# 1) nounset-safe local declarations.
old_decl='  local table="$1" where="$2" file="$RUN/md/${table}.csv"\n'
new_decl='  local table="$1"\n  local where="$2"\n  local file="$RUN/md/${table}.csv"\n'
if old_decl in s:
    s=s.replace(old_decl,new_decl,1)

# 2) quote-safe nested SSH transport for filtered exports.
old='''  ssh "$DEV_HOST" "DBC='$DB_C' DBU='$DB_USER' DBN='$DB_NAME' T='$table' W='$where' bash -s" > "$file" <<'REMOTE'\nset -Eeuo pipefail\nSQL="COPY (SELECT * FROM public.\\\"$T\\\""\n[[ -n "$W" ]] && SQL+=" WHERE $W"\nSQL+=") TO STDOUT WITH CSV HEADER"\ndocker exec "$DBC" psql -X -q -v ON_ERROR_STOP=1 -U "$DBU" -d "$DBN" -c "$SQL"\nREMOTE\n'''
new='''  local dbc64 dbu64 dbn64 t64 w64\n  dbc64="$(printf '%s' "$DB_C" | base64 | tr -d '\\n')"\n  dbu64="$(printf '%s' "$DB_USER" | base64 | tr -d '\\n')"\n  dbn64="$(printf '%s' "$DB_NAME" | base64 | tr -d '\\n')"\n  t64="$(printf '%s' "$table" | base64 | tr -d '\\n')"\n  w64="$(printf '%s' "$where" | base64 | tr -d '\\n')"\n  ssh "$DEV_HOST" "DBC64='$dbc64' DBU64='$dbu64' DBN64='$dbn64' T64='$t64' W64='$w64' bash -s" > "$file" <<'REMOTE'\nset -Eeuo pipefail\nDBC="$(printf '%s' "$DBC64" | base64 -d)"\nDBU="$(printf '%s' "$DBU64" | base64 -d)"\nDBN="$(printf '%s' "$DBN64" | base64 -d)"\nT="$(printf '%s' "$T64" | base64 -d)"\nW="$(printf '%s' "$W64" | base64 -d)"\nSQL="COPY (SELECT * FROM public.\\\"$T\\\""\n[[ -n "$W" ]] && SQL+=" WHERE $W"\nSQL+=") TO STDOUT WITH CSV HEADER"\ndocker exec "$DBC" psql -X -q -v ON_ERROR_STOP=1 -U "$DBU" -d "$DBN" -c "$SQL"\nREMOTE\n'''
if old not in s:
    raise SystemExit('FAIL: unsafe export transport block not found')
s=s.replace(old,new,1)

# 3) Replace generic PK-first merge with explicit business/natural keys.
merge_re=re.compile(r'''cat > \"\$RUN/merge\.sql\" <<'SQL'\n.*?\nSQL\nscp -q \"\$RUN/merge\.sql\"''',re.S)
merge='''cat > "$RUN/merge.sql" <<'SQL'
\\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE
  r record;
  cols text;
  updates text;
  key_sql text;
  q text;
BEGIN
  FOR r IN
    SELECT * FROM (VALUES
      ('face_generation_regions', ARRAY['code']::text[], ARRAY['id']::text[]),
      ('tts_languages', ARRAY['language_code']::text[], ARRAY[]::text[]),
      ('tts_locales', ARRAY['locale']::text[], ARRAY[]::text[]),
      ('tts_locale_aliases', ARRAY['alias_key']::text[], ARRAY[]::text[]),
      ('tts_providers', ARRAY['provider_code']::text[], ARRAY[]::text[]),
      ('tts_provider_models', ARRAY['provider_code','model_code']::text[], ARRAY[]::text[]),
      ('tts_model_language_capabilities', ARRAY['provider_code','model_code','language_code']::text[], ARRAY[]::text[]),
      ('tts_model_locale_capabilities', ARRAY['provider_code','model_code','locale']::text[], ARRAY[]::text[]),
      ('tts_voices', ARRAY['provider','voice_name']::text[], ARRAY['id']::text[])
    ) v(t,keyarr,preservearr)
  LOOP
    SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
      INTO cols
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name=r.t AND is_generated='NEVER';

    SELECT string_agg(format('%1$I = EXCLUDED.%1$I',c.column_name), ', ' ORDER BY c.ordinal_position)
      INTO updates
    FROM information_schema.columns c
    WHERE c.table_schema='public' AND c.table_name=r.t AND c.is_generated='NEVER'
      AND NOT (c.column_name = ANY(r.keyarr))
      AND NOT (c.column_name = ANY(r.preservearr))
      AND c.column_name NOT IN ('created_at','updated_at','discovered_at','last_seen_at','last_synced_at','refreshed_at');

    SELECT string_agg(quote_ident(x), ', ') INTO key_sql FROM unnest(r.keyarr) x;
    IF updates IS NULL THEN
      updates := format('%1$I = EXCLUDED.%1$I', r.keyarr[1]);
    END IF;

    q := format(
      'INSERT INTO public.%1$I (%2$s) SELECT %2$s FROM sync_stage.%1$I ON CONFLICT (%3$s) DO UPDATE SET %4$s',
      r.t, cols, key_sql, updates
    );
    EXECUTE q;
  END LOOP;
END $$;

-- Existing prod voices can have different UUIDs than dev for the same provider-native voice.
-- Remap staged capability rows to the production voice identity before merging dependents.
UPDATE sync_stage.tts_voice_model_capabilities c
SET voice_id = p.id
FROM sync_stage.tts_voices s
JOIN public.tts_voices p
  ON p.provider = s.provider
 AND p.voice_name = s.voice_name
WHERE c.voice_id = s.id;

UPDATE sync_stage.tts_voice_locale_capabilities c
SET voice_id = p.id
FROM sync_stage.tts_voices s
JOIN public.tts_voices p
  ON p.provider = s.provider
 AND p.voice_name = s.voice_name
WHERE c.voice_id = s.id;

INSERT INTO public.tts_voice_model_capabilities
SELECT * FROM sync_stage.tts_voice_model_capabilities
ON CONFLICT (provider_code, voice_id, model_code) DO UPDATE SET
  is_enabled=EXCLUDED.is_enabled,
  is_approved=EXCLUDED.is_approved,
  supports_styles=EXCLUDED.supports_styles,
  supports_emotions=EXCLUDED.supports_emotions,
  supports_streaming=EXCLUDED.supports_streaming,
  source=EXCLUDED.source,
  source_version=EXCLUDED.source_version,
  meta_json=EXCLUDED.meta_json;

INSERT INTO public.tts_voice_locale_capabilities
SELECT * FROM sync_stage.tts_voice_locale_capabilities
ON CONFLICT (voice_id, locale, accent_code) DO UPDATE SET
  is_native_fit=EXCLUDED.is_native_fit,
  is_recommended=EXCLUDED.is_recommended,
  is_enabled=EXCLUDED.is_enabled,
  is_approved=EXCLUDED.is_approved,
  quality_score=EXCLUDED.quality_score,
  source=EXCLUDED.source,
  source_version=EXCLUDED.source_version,
  meta_json=EXCLUDED.meta_json;

COMMIT;
SQL
scp -q "$RUN/merge.sql"'''
if merge_re.search(s) is None:
    raise SystemExit('FAIL: merge.sql block not found')
s=merge_re.sub(merge,s,count=1)
p.write_text(s)
PY

bash -n "$TMP"
grep -Fq "ARRAY['code']::text[]" "$TMP"
grep -Fq "ARRAY['provider','voice_name']::text[]" "$TMP"
grep -Fq 'UPDATE sync_stage.tts_voice_model_capabilities' "$TMP"
grep -Fq 'UPDATE sync_stage.tts_voice_locale_capabilities' "$TMP"
echo "SYNC_LAUNCHER_NATURAL_KEY_FIX=PASS"

# Read-only preflight against dev, including natural-key uniqueness checks.
echo "===== PREFLIGHT: DEV MASTERDATA KEYS ====="
ssh "$DEV_HOST" "DEV_ROOT='$DEV_ROOT' bash -s" <<'REMOTE'
set -Eeuo pipefail
DB_C=""
for c in desifaces-v3-db desifaces-db; do docker inspect "$c" >/dev/null 2>&1 && { DB_C="$c"; break; }; done
[[ -n "$DB_C" ]] || exit 4
DB_USER="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec "$DB_C" sh -lc 'printf %s "$POSTGRES_DB"')"
q(){ docker exec "$DB_C" psql -X -A -t -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c "$1" | tr -d '[:space:]'; }

for spec in \
 'face_generation_regions|code' \
 'tts_languages|language_code' \
 'tts_locales|locale' \
 'tts_locale_aliases|alias_key' \
 'tts_providers|provider_code' \
 'tts_provider_models|provider_code,model_code' \
 'tts_model_language_capabilities|provider_code,model_code,language_code' \
 'tts_model_locale_capabilities|provider_code,model_code,locale' \
 'tts_voices|provider,voice_name'; do
  t="${spec%%|*}"; k="${spec#*|}"
  d="$(q "SELECT count(*) FROM (SELECT $k,count(*) FROM public.$t GROUP BY $k HAVING count(*)>1) x")"
  echo "NATURAL_KEY|$t|duplicates=$d"
  [[ "$d" == 0 ]] || exit 5
done

for sql in \
 "select count(*) from public.tts_providers where provider_code='azure'" \
 "select count(*) from public.tts_provider_models where provider_code='azure'" \
 "select count(*) from public.tts_model_locale_capabilities where provider_code='azure'" \
 "select count(*) from public.tts_voices where provider='azure'"; do
  n="$(q "$sql")"; [[ "$n" =~ ^[0-9]+$ ]] || exit 5
done

echo "DEV_MASTERDATA_KEY_PREFLIGHT=PASS"
REMOTE

echo "SYNC_LAUNCHER_PREFLIGHT=PASS"
exec bash "$TMP"
