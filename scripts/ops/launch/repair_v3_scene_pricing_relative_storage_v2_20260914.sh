#!/usr/bin/env bash
set -Eeuo pipefail

EXT="df-svc-fusion-extension"
TARGET="/app/app/api/routes/v3_scene_pricing.py"
WF="120fa276-2796-4a83-a6b7-b29fa7c0f99c"
SCENE="9f2d02b9-59ab-524d-bbe7-11bf87579a9d"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$HOME/df-v3-scene-pricing-before-relative-storage-v2-${STAMP}.py"
TMP="$(mktemp -d)"
MUTATED=0

fail(){ echo "FAIL: $*" >&2; exit 1; }

cleanup(){
  rc=$?
  trap - EXIT
  if [[ "$rc" -ne 0 && "$MUTATED" -eq 1 ]]; then
    echo "ROLLBACK_TRIGGERED=YES"
    docker cp "$BACKUP" "$EXT:$TARGET" >/dev/null 2>&1 || true
    docker restart "$EXT" >/dev/null 2>&1 || true
    echo "ROLLBACK_COMPLETE=YES"
  fi
  rm -rf "$TMP"
  exit "$rc"
}
trap cleanup EXIT

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "wrong host"
docker inspect "$EXT" >/dev/null 2>&1 || fail "$EXT missing"
[[ "$(docker inspect -f '{{.State.Status}}' "$EXT")" == "running" ]] || fail "$EXT not running"

echo "============================================================"
echo " desifaces — V3 RELATIVE AUDIO STORAGE LINEAGE REPAIR V2"
echo " scope=FUSION_EXTENSION_SCENE_PRICING_ONLY"
echo " strategy=PARSE_CONTAINER_FROM_CANONICAL_SOURCE_AUDIO_URL"
echo " heredoc=QUOTED_SQL_PLACEHOLDERS_PRESERVED"
echo "============================================================"

docker cp "$EXT:$TARGET" "$BACKUP"
cp "$BACKUP" "$TMP/original.py"
echo "BACKUP=$BACKUP"

python3 - "$TMP/original.py" "$TMP/patched.py" <<'PY'
from pathlib import Path
import sys

src=Path(sys.argv[1]).read_text()

old='''    if storage_uri:
        parsed = urlparse(storage_uri)
        host = _clean(parsed.hostname).lower()
        if parsed.scheme in {"http", "https"} and host.endswith(".blob.core.windows.net"):
            remainder = unquote(parsed.path).lstrip("/")
            container, sep, blob_name = remainder.partition("/")
            if sep and container and blob_name:
                return container, blob_name.lstrip("/")

    meta = _as_dict(row["meta_json"])
'''

new='''    if storage_uri:
        parsed = urlparse(storage_uri)
        host = _clean(parsed.hostname).lower()
        if parsed.scheme in {"http", "https"} and host.endswith(".blob.core.windows.net"):
            remainder = unquote(parsed.path).lstrip("/")
            container, sep, blob_name = remainder.partition("/")
            if sep and container and blob_name:
                return container, blob_name.lstrip("/")

    # Canonical V3 audio assets store storage_uri as a relative blob path.
    # Recover only the container from producer metadata's source_audio_url,
    # require the URL blob path to match storage_uri exactly, then sign a new
    # read URL using the current storage connection. The old SAS is never used.
    if storage_uri and "://" not in storage_uri:
        v3_meta = _as_dict(row["v3_metadata"])
        source_audio_url = _clean(v3_meta.get("source_audio_url"))
        if source_audio_url:
            parsed = urlparse(source_audio_url)
            host = _clean(parsed.hostname).lower()
            if parsed.scheme in {"http", "https"} and host.endswith(".blob.core.windows.net"):
                remainder = unquote(parsed.path).lstrip("/")
                container, sep, source_blob = remainder.partition("/")
                canonical_blob = storage_uri.lstrip("/")
                if sep and container and source_blob == canonical_blob:
                    return container, canonical_blob

    meta = _as_dict(row["meta_json"])
'''

if old not in src:
    raise SystemExit("PATCH_ABORT: relative storage insertion baseline mismatch")
src=src.replace(old,new,1)

old_sql='''        select dt.turn_id,dt.sequence_no,ao.media_id,
               ma.meta_json,ma.storage_ref,vma.storage_uri
'''
new_sql='''        select dt.turn_id,dt.sequence_no,ao.media_id,
               ma.meta_json,ma.storage_ref,vma.storage_uri,
               vma.metadata as v3_metadata
'''
if old_sql not in src:
    raise SystemExit("PATCH_ABORT: V3 media SELECT baseline mismatch")
src=src.replace(old_sql,new_sql,1)

Path(sys.argv[2]).write_text(src)
print("RELATIVE_STORAGE_PATCH_BUILD=PASS")
PY

python3 -m py_compile "$TMP/patched.py"
echo "RELATIVE_STORAGE_PATCH_COMPILE=PASS"

docker cp "$TMP/patched.py" "$EXT:$TARGET"
MUTATED=1

docker exec "$EXT" python -m py_compile "$TARGET"
echo "CONTAINER_COMPILE=PASS"

echo "===== PRE-RESTART 8/8 STORAGE + FFPROBE GATE ====="
timeout 90s docker exec -i \
  -e DF_TARGET_WF="$WF" \
  -e DF_TARGET_SCENE="$SCENE" \
  "$EXT" python - <<'PY'
import asyncio
import asyncpg
import os
from uuid import UUID

from app.api.routes.v3_scene_pricing import (
    _approved_audio_rows,
    _measure_audio_rows,
    _storage_location,
)

WF=UUID(os.environ["DF_TARGET_WF"])
SCENE=UUID(os.environ["DF_TARGET_SCENE"])

async def main():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        wf=await conn.fetchrow(
            "select account_id,project_id from public.v3_studio_workflows where workflow_id=$1",
            WF,
        )
        assert wf, "workflow missing"
        rows=await _approved_audio_rows(
            conn,
            scene_id=SCENE,
            workflow_id=WF,
            account_id=wf["account_id"],
            project_id=wf["project_id"],
        )
        assert len(rows)==8, len(rows)
        containers=[]
        for row in rows:
            container,blob=_storage_location(row)
            assert container, row["media_id"]
            assert blob, row["media_id"]
            containers.append(container)
            print(f"SEQ={row['sequence_no']} CONTAINER={container} RELATIVE_STORAGE=PASS")
        assert len(set(containers))==1, containers
        measured,total,lineage=await _measure_audio_rows(rows)
        assert len(measured)==8, len(measured)
        assert total>0, total
        assert lineage, "lineage hash missing"
        print(f"CANONICAL_AUDIO_CONTAINER={containers[0]}")
        print("APPROVED_AUDIO_STORAGE_ROWS=8/8")
        print("FFPROBE_AUDIO_ROWS=8/8")
        print(f"TOTAL_AUDIO_DURATION_SEC={total}")
        print("FRESH_SIGNED_AUDIO_READ=PASS")
        print("SCENE_PRICING_AUDIO_MEASUREMENT=PASS")
    finally:
        await conn.close()

asyncio.run(main())
PY

echo "===== RESTART FUSION EXTENSION ONLY ====="
docker restart "$EXT" >/dev/null

STATE=""; HEALTH=""
for _ in $(seq 1 30); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$EXT" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT" 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    break
  fi
  sleep 2
done
[[ "$STATE" == "running" ]] || fail "Fusion Extension not running after restart"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension unhealthy: $HEALTH"

docker exec -i "$EXT" python - <<'PY'
from app.main import app
paths={getattr(r,'path','') for r in app.routes}
assert '/api/longform/v3/scene-pricing/preview' in paths
print('LIVE_SCENE_PRICING_ROUTE=PASS')
PY

trap - EXIT
rm -rf "$TMP"

echo "============================================================"
echo "ROOT_CAUSE=V3_STORAGE_URI_IS_RELATIVE_BLOB_PATH"
echo "CONTAINER_SOURCE=V3_METADATA_SOURCE_AUDIO_URL_PATH_ONLY"
echo "EXPIRED_SOURCE_SAS_REUSED=NO"
echo "FRESH_SAS_GENERATION=YES"
echo "AUDIO_TOUCH=NONE"
echo "AUDIO_APPROVAL_TOUCH=NONE"
echo "DIRECTOR_TOUCH=NONE"
echo "DB_TOUCH=NONE"
echo "PRICING_SERVICE_TOUCH=NONE"
echo "V3_RELATIVE_AUDIO_STORAGE_LINEAGE_REPAIR=PASS"
echo "SAFE_TO_RETRY_CHECK_PRICE=YES"
echo "============================================================"
