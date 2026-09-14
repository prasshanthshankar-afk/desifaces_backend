#!/usr/bin/env bash
set -Eeuo pipefail

# SSH-native, repo-free production repair for V3 scene parent pricing.
# Root cause: svc-audio canonical media intentionally stores project_id=NULL and
# records the requested project in meta_json.requested_project_id, while
# v3_scene_pricing required media_assets.project_id=$4.  This script derives a
# candidate from the exact running Fusion Extension image and changes only that
# compatibility predicate.  Account ownership, audio kind, active lifecycle,
# approved-stage/output/review, and exact requested-project checks remain strict.

EXPECTED_HOST="desifaces-gpu"
TARGET_WORKFLOW="120fa276-2796-4a83-a6b7-b29fa7c0f99c"
TARGET_SCENE="9f2d02b9-59ab-524d-bbe7-11bf87579a9d"
TARGET_STAGE="68bfd034-e232-440f-b036-7f71b0d95472"
EXPECTED_TURNS=8
TARGET_FILE="/app/app/api/routes/v3_scene_pricing.py"
PREFLIGHT="df-fusion-extension-audio-lineage-preflight"
TMP="$(mktemp -d /tmp/desifaces-scene-pricing-audio.XXXXXX)"
BASE_FILE="$TMP/v3_scene_pricing.base.py"
PATCH_FILE="$TMP/v3_scene_pricing.py"
ENVFILE="$TMP/live.env"
MUTATED=0

cleanup() {
  docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
  rm -rf "$TMP" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail(){ echo "FAIL: $*" >&2; return 1; }
pass(){ echo "$1=PASS"; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "production host guard failed"
command -v docker >/dev/null 2>&1 || fail "docker missing"
command -v python3 >/dev/null 2>&1 || fail "python3 missing"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"

EXT="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-fusion-extension$|^df-svc-fusion-extension$|svc-fusion-extension$' | head -1 || true)"
[[ -n "$EXT" ]] || fail "Fusion Extension API container not found"
IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$EXT")"
IMAGE_ID="$(docker inspect -f '{{.Image}}' "$EXT")"
NETWORK="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$EXT")"
[[ -n "$IMAGE_REF" && -n "$IMAGE_ID" && -n "$NETWORK" ]] || fail "Fusion Extension runtime metadata incomplete"
[[ "$IMAGE_REF" != sha256:* ]] || fail "Fusion Extension image reference is not safely retaggable"

docker cp "$EXT:$TARGET_FILE" "$BASE_FILE"
cp "$BASE_FILE" "$PATCH_FILE"
BASE_SHA="$(sha256sum "$BASE_FILE" | awk '{print $1}')"

WORKDIR="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$EXT" 2>/dev/null || true)"
CONFIG_FILES="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$EXT" 2>/dev/null || true)"
SERVICE="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$EXT" 2>/dev/null || true)"
PROJECT="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$EXT" 2>/dev/null || true)"
[[ -n "$WORKDIR" && -d "$WORKDIR" && -n "$CONFIG_FILES" && -n "$SERVICE" && -n "$PROJECT" ]] || fail "Compose ownership metadata incomplete"
CANON_ENV="$WORKDIR/infra/.env"
[[ -s "$CANON_ENV" ]] || fail "canonical production env missing: $CANON_ENV"

COMPOSE=(docker compose --project-directory "$WORKDIR" --env-file "$CANON_ENV" -p "$PROJECT")
IFS=',' read -r -a CFG_ARR <<< "$CONFIG_FILES"
for f in "${CFG_ARR[@]}"; do
  [[ -f "$f" ]] || fail "Compose file missing: $f"
  COMPOSE+=( -f "$f" )
done
"${COMPOSE[@]}" config -q </dev/null

# Capture current API env without printing secrets.
docker inspect "$EXT" > "$TMP/ext.inspect.json"
python3 - "$TMP/ext.inspect.json" "$ENVFILE" <<'PY'
import json,sys
obj=json.load(open(sys.argv[1]))[0]
with open(sys.argv[2],"w") as out:
    for raw in obj.get("Config",{}).get("Env",[]):
        if "=" not in raw: continue
        k,v=raw.split("=",1)
        if "\n" in v or "\r" in v:
            raise SystemExit(f"unsupported newline env: {k}")
        out.write(f"{k}={v}\n")
PY
chmod 600 "$ENVFILE"

cat <<EOF
============================================================
 desifaces — V3 SCENE PRICING CANONICAL AUDIO RECOVERY
 target_workflow=$TARGET_WORKFLOW
 target_scene=$TARGET_SCENE
 target_stage=$TARGET_STAGE
 fusion_extension=$EXT
 base_image=$IMAGE_REF
 base_image_id=$IMAGE_ID
 base_file_sha256=$BASE_SHA
 mutation_scope=ONE_FUSION_EXTENSION_API_FILE
 database_mutation=NONE
 face_audio_fusion_worker_touch=NONE
============================================================
EOF
pass PRODUCTION_BASELINE_CAPTURE

# Prove the exact live data shape before changing code.  This is read-only.
docker exec -i "$EXT" python - "$TARGET_WORKFLOW" "$TARGET_SCENE" "$EXPECTED_TURNS" <<'PY'
import asyncio,os,sys,asyncpg
from uuid import UUID

workflow_id=UUID(sys.argv[1]); scene_id=UUID(sys.argv[2]); expected=int(sys.argv[3])

async def main():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        wf=await conn.fetchrow("select account_id,project_id from public.v3_studio_workflows where workflow_id=$1",workflow_id)
        assert wf, "workflow missing"
        account_id=wf["account_id"]; project_id=wf["project_id"]
        rows=await conn.fetch("""
            select dt.turn_id,dt.sequence_no,ao.media_id,
                   ma.id as asset_id,ma.account_id,ma.project_id,ma.kind,ma.lifecycle_state,
                   ma.meta_json->>'requested_project_id' as requested_project_id
            from public.v3_dialogue_turns dt
            join public.v3_studio_stage_runs a
              on a.workflow_id=$1 and a.stage_type='audio' and a.scope_type='dialogue_turn'
             and a.dialogue_turn_id=dt.turn_id and a.state='approved'
            join public.v3_studio_stage_outputs ao
              on ao.stage_run_id=a.stage_run_id and ao.is_active=true
            join public.v3_studio_review_items ar
              on ar.stage_run_id=a.stage_run_id and ar.media_id=ao.media_id and ar.decision='approved'
            left join public.media_assets ma on ma.id=ao.media_id
            where dt.scene_id=$2 and dt.turn_kind='speech'
            order by dt.sequence_no,dt.turn_id
        """,workflow_id,scene_id)
        assert len(rows)==expected, f"approved stage/output/review rows {len(rows)}/{expected}"
        canonical=0
        for r in rows:
            assert r["asset_id"] is not None, f"media missing turn={r['turn_id']}"
            assert r["account_id"]==account_id, f"account mismatch turn={r['turn_id']}"
            assert str(r["kind"] or "")=="audio", f"kind mismatch turn={r['turn_id']}"
            assert str(r["lifecycle_state"] or "")=="active", f"lifecycle mismatch turn={r['turn_id']}"
            direct=(r["project_id"]==project_id)
            fallback=(r["project_id"] is None and str(r["requested_project_id"] or "")==str(project_id))
            assert direct or fallback, f"project lineage mismatch turn={r['turn_id']}"
            canonical += int(fallback)
        assert canonical>0, "target data does not exercise canonical NULL-project compatibility path"
        print(f"APPROVED_AUDIO_ROWS={len(rows)}")
        print(f"CANONICAL_NULL_PROJECT_ROWS={canonical}")
        print("LIVE_CANONICAL_AUDIO_LINEAGE_PROOF=PASS")
    finally:
        await conn.close()
asyncio.run(main())
PY

# Patch exactly one known SQL predicate, once.
python3 - "$PATCH_FILE" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
s=p.read_text()
old="""        join public.media_assets ma
          on ma.id=ao.media_id and ma.account_id=$3 and ma.project_id=$4
         and ma.kind='audio' and ma.lifecycle_state='active'
"""
new="""        join public.media_assets ma
          on ma.id=ao.media_id and ma.account_id=$3
         and (
               ma.project_id=$4
               or (
                    ma.project_id is null
                    and ma.meta_json->>'requested_project_id'=$4::text
               )
         )
         and ma.kind='audio' and ma.lifecycle_state='active'
"""
count=s.count(old)
if count != 1:
    raise SystemExit(f"expected exact pricing media predicate once, found {count}")
s=s.replace(old,new,1)
p.write_text(s)
PY

PATCH_SHA="$(sha256sum "$PATCH_FILE" | awk '{print $1}')"
[[ "$PATCH_SHA" != "$BASE_SHA" ]] || fail "patch did not change target file"
grep -Fq "ma.account_id=\$3" "$PATCH_FILE" || fail "account isolation predicate missing"
grep -Fq "ma.meta_json->>'requested_project_id'=\$4::text" "$PATCH_FILE" || fail "canonical requested-project predicate missing"
grep -Fq "ma.kind='audio' and ma.lifecycle_state='active'" "$PATCH_FILE" || fail "audio active lifecycle predicate missing"
pass ONE_PREDICATE_PATCH_GATE

# Preserve the exact running image before any tag mutation.
ROLLBACK_TAG="desifaces-svc-fusion-extension:rollback-audio-lineage-$(date -u +%Y%m%dT%H%M%SZ)"
docker tag "$IMAGE_ID" "$ROLLBACK_TAG"
docker image inspect "$ROLLBACK_TAG" >/dev/null
pass ROLLBACK_IMAGE_CAPTURE

# Candidate derives from exact current production image; one file is replaced.
CANDIDATE="desifaces-svc-fusion-extension:audio-lineage-fix-$(date -u +%Y%m%dT%H%M%SZ)"
cat > "$TMP/Dockerfile" <<EOF
FROM $ROLLBACK_TAG
COPY v3_scene_pricing.py $TARGET_FILE
EOF
cp "$PATCH_FILE" "$TMP/v3_scene_pricing.py"
docker build --pull=false -t "$CANDIDATE" "$TMP" > "$TMP/build.log" 2>&1 || {
  tail -n 120 "$TMP/build.log" >&2 || true
  fail "candidate build failed"
}
CANDIDATE_ID="$(docker image inspect -f '{{.Id}}' "$CANDIDATE")"
CANDIDATE_FILE_SHA="$(docker run --rm --entrypoint sha256sum "$CANDIDATE" "$TARGET_FILE" | awk '{print $1}')"
[[ "$CANDIDATE_FILE_SHA" == "$PATCH_SHA" ]] || fail "candidate target-file hash mismatch"
pass ONE_FILE_CANDIDATE_BUILD

# Real target-story proof against the candidate helper.  No writes and no pricing call.
docker run --rm -i --env-file "$ENVFILE" --entrypoint python "$CANDIDATE" \
  - "$TARGET_WORKFLOW" "$TARGET_SCENE" "$EXPECTED_TURNS" <<'PY'
import asyncio,os,sys,asyncpg
from uuid import UUID,uuid4
from fastapi import HTTPException
from app.api.routes.v3_scene_pricing import _approved_audio_rows

workflow_id=UUID(sys.argv[1]); scene_id=UUID(sys.argv[2]); expected=int(sys.argv[3])

async def must_409(conn, **kw):
    try:
        await _approved_audio_rows(conn,**kw)
    except HTTPException as e:
        assert e.status_code==409, e
        return
    raise AssertionError("isolation negative test unexpectedly passed")

async def main():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        wf=await conn.fetchrow("select account_id,project_id from public.v3_studio_workflows where workflow_id=$1",workflow_id)
        assert wf
        rows=await _approved_audio_rows(conn,scene_id=scene_id,workflow_id=workflow_id,account_id=wf["account_id"],project_id=wf["project_id"])
        assert len(rows)==expected,(len(rows),expected)
        print(f"TARGET_STORY_APPROVED_AUDIO_ROWS={len(rows)}")
        print("TARGET_STORY_CANONICAL_AUDIO_ACCEPTED=PASS")
        await must_409(conn,scene_id=scene_id,workflow_id=workflow_id,account_id=uuid4(),project_id=wf["project_id"])
        print("CROSS_ACCOUNT_ISOLATION=PASS")
        await must_409(conn,scene_id=scene_id,workflow_id=workflow_id,account_id=wf["account_id"],project_id=uuid4())
        print("CROSS_PROJECT_ISOLATION=PASS")
    finally:
        await conn.close()
asyncio.run(main())
PY
pass CANDIDATE_DATA_CONTRACT_TESTS

# Full app import / route surface must remain unchanged.
for img in "$ROLLBACK_TAG" "$CANDIDATE"; do
  out="$TMP/routes.$(echo "$img" | tr '/:' '__')"
  docker run --rm --env-file "$ENVFILE" --entrypoint python "$img" -c \
    'from app.main import app; import json; print(json.dumps(sorted({(getattr(r,"path","") or "")+"|"+",".join(sorted(getattr(r,"methods",set()) or set())) for r in app.routes})))' > "$out"
done
BASE_ROUTES="$TMP/routes.$(echo "$ROLLBACK_TAG" | tr '/:' '__')"
CAND_ROUTES="$TMP/routes.$(echo "$CANDIDATE" | tr '/:' '__')"
cmp -s "$BASE_ROUTES" "$CAND_ROUTES" || fail "Fusion Extension route surface changed"
pass ROUTE_SURFACE_UNCHANGED

# Isolated candidate on production network, no host port and no production alias.
docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
docker run -d --name "$PREFLIGHT" --network "$NETWORK" --env-file "$ENVFILE" \
  -e PORT=8006 -e WORKER_ENABLED=false -e STITCH_WORKER_ENABLED=false "$CANDIDATE" >/dev/null
READY=0
for _ in $(seq 1 45); do
  if docker exec "$PREFLIGHT" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8006/api/health >/dev/null 2>&1; then
    READY=1; break
  fi
  sleep 2
done
(( READY == 1 )) || { docker logs --tail 160 "$PREFLIGHT" >&2 || true; fail "isolated candidate health failed"; }
pass ISOLATED_CANDIDATE_HEALTH

docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
pass PRE_MUTATION_CERTIFICATION

rollback(){
  rc=${1:-1}
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  docker tag "$ROLLBACK_TAG" "$IMAGE_REF" >/dev/null 2>&1 || true
  "${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" </dev/null >/dev/null 2>&1 || true
  REC="$(docker ps -a --format '{{.Names}}' | grep -E '^df-v3-svc-fusion-extension$|^df-svc-fusion-extension$|svc-fusion-extension$' | head -1 || true)"
  if [[ -n "$REC" ]]; then
    for _ in $(seq 1 45); do
      s="$(docker inspect -f '{{.State.Status}}' "$REC" 2>/dev/null || true)"
      h="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$REC" 2>/dev/null || true)"
      [[ "$s" == "running" && ( "$h" == "healthy" || "$h" == "no-healthcheck" ) ]] && break
      sleep 2
    done
    echo "ROLLBACK_STATE=${s:-unknown}"
    echo "ROLLBACK_HEALTH=${h:-unknown}"
  fi
  echo "ROLLBACK_COMPLETE=YES"
  exit "$rc"
}

# First production mutation: activate preflighted API image, API service only.
docker tag "$CANDIDATE" "$IMAGE_REF"
MUTATED=1
trap 'rc=$?; (( MUTATED == 1 )) && rollback "$rc" || exit "$rc"' ERR
"${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" </dev/null
pass FUSION_EXTENSION_API_ONLY_RECREATE

EXT2="$(docker ps -a --format '{{.Names}}' | grep -E '^df-v3-svc-fusion-extension$|^df-svc-fusion-extension$|svc-fusion-extension$' | head -1 || true)"
[[ -n "$EXT2" ]] || fail "Fusion Extension API missing after recreate"
STATE=""; HEALTH=""; LIVE_READY=0
for _ in $(seq 1 60); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$EXT2" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT2" 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    if docker exec "$EXT2" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8006/api/health >/dev/null 2>&1; then LIVE_READY=1; break; fi
  fi
  sleep 2
done
[[ "$STATE" == "running" ]] || fail "Fusion Extension API not running after recreate"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension API health=$HEALTH"
(( LIVE_READY == 1 )) || fail "Fusion Extension live health endpoint not ready"
[[ "$(docker inspect -f '{{.Image}}' "$EXT2")" == "$CANDIDATE_ID" ]] || fail "live API is not candidate image"
LIVE_FILE_SHA="$(docker exec "$EXT2" sha256sum "$TARGET_FILE" | awk '{print $1}')"
[[ "$LIVE_FILE_SHA" == "$PATCH_SHA" ]] || fail "live target-file fingerprint mismatch"
pass PRODUCTION_CANDIDATE_ACTIVE

# Re-run exact target-story helper in the live API image.
docker exec -i "$EXT2" python - "$TARGET_WORKFLOW" "$TARGET_SCENE" "$EXPECTED_TURNS" <<'PY'
import asyncio,os,sys,asyncpg
from uuid import UUID
from app.api.routes.v3_scene_pricing import _approved_audio_rows
workflow_id=UUID(sys.argv[1]); scene_id=UUID(sys.argv[2]); expected=int(sys.argv[3])
async def main():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        wf=await conn.fetchrow("select account_id,project_id from public.v3_studio_workflows where workflow_id=$1",workflow_id)
        assert wf
        rows=await _approved_audio_rows(conn,scene_id=scene_id,workflow_id=workflow_id,account_id=wf["account_id"],project_id=wf["project_id"])
        assert len(rows)==expected,(len(rows),expected)
        print(f"LIVE_TARGET_APPROVED_AUDIO_ROWS={len(rows)}")
        print("LIVE_TARGET_SCENE_PRICING_AUDIO_GATE=PASS")
    finally:
        await conn.close()
asyncio.run(main())
PY

trap - ERR
MUTATED=0

echo "============================================================"
echo "ROOT_CAUSE=SCENE_PRICING_REJECTED_CANONICAL_AUDIO_NULL_PROJECT_ID"
echo "FIX=ACCEPT_NULL_PROJECT_ONLY_WHEN_REQUESTED_PROJECT_META_MATCHES"
echo "ACCOUNT_ISOLATION=PRESERVED"
echo "PROJECT_ISOLATION=PRESERVED"
echo "APPROVAL_REVIEW_GATE=PRESERVED"
echo "AUDIO_KIND_ACTIVE_GATE=PRESERVED"
echo "PRICING_FORMULA=UNCHANGED"
echo "DATABASE_MUTATION=NONE"
echo "FUSION_EXTENSION_API_ONLY=YES"
echo "ROLLBACK_IMAGE=$ROLLBACK_TAG"
echo "SAFE_TO_RETRY_SAME_STORY_CHECK_PRICE=YES"
echo "PRODUCTION_SCENE_PRICING_CANONICAL_AUDIO_FIX=PASS"
echo "============================================================"
