#!/usr/bin/env bash
set -Eeuo pipefail

PROD_HOST="${PROD_HOST:-desifaces-gpu}"
DEV_HOST="${DEV_HOST:-desifaces-dev}"
APPLY_SHA="233d1b6fc332a387f7b625285a93e9695ad688eb"
WORK="/tmp/desifaces-next2-country-code-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$WORK"

fail(){ echo "BLOCKER: $*" >&2; exit 2; }

column_meta(){
  local host="$1" out="$2"
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$host" "python3 -" >"$out" <<'PY'
import json, subprocess
names=subprocess.check_output(["docker","ps","--format","{{.Names}}"], text=True).splitlines()
apps=[n for n in names if n.endswith("svc-core") or n.endswith("svc-pricing") or n.endswith("svc-director")]
if not apps:
    print(json.dumps({"error":"no application container"})); raise SystemExit(0)
apps.sort(key=lambda n:(0 if "v3" in n else 1,n))
c=apps[0]
code=r'''
import asyncio, json, os
import asyncpg
dsn=(os.getenv("DATABASE_URL") or os.getenv("DF_DATABASE_URL") or os.getenv("POSTGRES_DSN") or "").strip()
dsn=dsn.replace("postgresql+asyncpg://","postgresql://",1)
async def main():
    if not dsn:
        print(json.dumps({"error":"dsn missing"})); return
    conn=await asyncpg.connect(dsn)
    row=await conn.fetchrow("""
      select
        format_type(a.atttypid,a.atttypmod) as data_type,
        a.attnotnull as not_null,
        pg_get_expr(ad.adbin,ad.adrelid) as default_expr,
        a.attidentity::text as identity_kind,
        a.attgenerated::text as generated_kind,
        coll.collname as collation
      from pg_attribute a
      join pg_class c on c.oid=a.attrelid
      join pg_namespace n on n.oid=c.relnamespace
      left join pg_attrdef ad on ad.adrelid=a.attrelid and ad.adnum=a.attnum
      left join pg_collation coll on coll.oid=a.attcollation and a.attcollation<>0
      where n.nspname='core'
        and c.relname='users'
        and a.attname='country_code'
        and a.attnum>0
        and not a.attisdropped
    """)
    count=await conn.fetchval("select count(*) from core.users")
    if row is None:
        print(json.dumps({"exists":False,"user_count":int(count or 0)}))
    else:
        d=dict(row); d["exists"]=True; d["user_count"]=int(count or 0)
        print(json.dumps(d))
    await conn.close()
asyncio.run(main())
'''
p=subprocess.run(["docker","exec","-i",c,"python","-"], input=code, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
if p.returncode:
    print(json.dumps({"error":"metadata query failed","detail":p.stderr[-500:]}))
else:
    print(p.stdout.strip())
PY
}

echo "===== 1. PROVE AUTHORITATIVE PROD COLUMN ====="
column_meta "$PROD_HOST" "$WORK/prod.json"
column_meta "$DEV_HOST" "$WORK/dev-before.json"
cat "$WORK/prod.json"
cat "$WORK/dev-before.json"

python3 - "$WORK/prod.json" "$WORK/dev-before.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1])); d=json.load(open(sys.argv[2]))
if p.get('error') or d.get('error'):
    raise SystemExit(f"BLOCKER: metadata capture failed prod={p.get('error')} dev={d.get('error')}")
if not p.get('exists'):
    raise SystemExit('BLOCKER: PROD core.users.country_code is missing')
expected={
  'data_type':'text',
  'not_null':False,
  'default_expr':None,
  'identity_kind':'',
  'generated_kind':'',
}
for k,v in expected.items():
    if p.get(k)!=v:
        raise SystemExit(f"BLOCKER: PROD country_code unexpected {k}={p.get(k)!r}; expected {v!r}")
if d.get('exists'):
    for k,v in expected.items():
        if d.get(k)!=v:
            raise SystemExit(f"BLOCKER: DEV country_code exists but differs {k}={d.get(k)!r}; expected {v!r}")
    print('DEV_COUNTRY_CODE_ALREADY_MATCHES=YES')
else:
    print('DEV_COUNTRY_CODE_REPAIR_REQUIRED=YES')
print('PROD_COUNTRY_CODE_CONTRACT=PASS_TEXT_NULLABLE_NO_DEFAULT')
PY

NEED_REPAIR="$(python3 - "$WORK/dev-before.json" <<'PY'
import json,sys
print('NO' if json.load(open(sys.argv[1])).get('exists') else 'YES')
PY
)"

if [[ "$NEED_REPAIR" == "YES" ]]; then
  echo "===== 2. ADD EXACT PROD COLUMN TO DEV ====="
  ssh -o BatchMode=yes -o ConnectTimeout=12 "$DEV_HOST" "python3 -" <<'PY'
import subprocess
names=subprocess.check_output(["docker","ps","--format","{{.Names}}"], text=True).splitlines()
apps=[n for n in names if n.endswith("svc-core") or n.endswith("svc-pricing") or n.endswith("svc-director")]
apps.sort(key=lambda n:(0 if "v3" in n else 1,n))
if not apps: raise SystemExit('BLOCKER: no DEV application container')
c=apps[0]
code=r'''
import asyncio, os
import asyncpg
dsn=(os.getenv("DATABASE_URL") or os.getenv("DF_DATABASE_URL") or os.getenv("POSTGRES_DSN") or "").strip()
dsn=dsn.replace("postgresql+asyncpg://","postgresql://",1)
async def main():
    conn=await asyncpg.connect(dsn)
    before=await conn.fetchval("select count(*) from core.users")
    async with conn.transaction():
        exists=await conn.fetchval("""
          select exists(
            select 1 from information_schema.columns
            where table_schema='core' and table_name='users' and column_name='country_code'
          )
        """)
        if not exists:
            await conn.execute("alter table core.users add column country_code text null")
        row=await conn.fetchrow("""
          select format_type(a.atttypid,a.atttypmod) as data_type,
                 a.attnotnull as not_null,
                 pg_get_expr(ad.adbin,ad.adrelid) as default_expr,
                 a.attidentity::text as identity_kind,
                 a.attgenerated::text as generated_kind
          from pg_attribute a
          join pg_class c on c.oid=a.attrelid
          join pg_namespace n on n.oid=c.relnamespace
          left join pg_attrdef ad on ad.adrelid=a.attrelid and ad.adnum=a.attnum
          where n.nspname='core' and c.relname='users' and a.attname='country_code'
            and a.attnum>0 and not a.attisdropped
        """)
        assert row is not None
        assert row['data_type']=='text', row
        assert row['not_null'] is False, row
        assert row['default_expr'] is None, row
        assert row['identity_kind']=='', row
        assert row['generated_kind']=='', row
        after=await conn.fetchval("select count(*) from core.users")
        assert before==after, (before,after)
    await conn.close()
    print('DEV_COUNTRY_CODE_REPAIR=PASS')
    print('DEV_USER_ROWCOUNT_PRESERVED='+str(before))
asyncio.run(main())
'''
p=subprocess.run(["docker","exec","-i",c,"python","-"], input=code, text=True)
raise SystemExit(p.returncode)
PY
fi

echo "===== 3. VERIFY DEV COLUMN ====="
column_meta "$DEV_HOST" "$WORK/dev-after.json"
cat "$WORK/dev-after.json"
python3 - "$WORK/prod.json" "$WORK/dev-after.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1])); d=json.load(open(sys.argv[2]))
keys=['data_type','not_null','default_expr','identity_kind','generated_kind']
assert d.get('exists'), d
for k in keys: assert p.get(k)==d.get(k), (k,p.get(k),d.get(k))
assert p.get('user_count') is not None and d.get('user_count') is not None
print('DEV_COUNTRY_CODE_CONTRACT=MATCH_PROD')
PY

echo "===== 4. LAUNCH PATCHED V3 SYNC ====="
APPLY="/tmp/desifaces-next2-apply-prod-dev-v3-${APPLY_SHA}.sh"
curl -fsSL "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${APPLY_SHA}/scripts/ops/apply-prod-dev-v3-runtime-sync-20260919.sh" -o "$APPLY"
chmod 700 "$APPLY"
bash -n "$APPLY"
exec "$APPLY"
