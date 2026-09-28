#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || {
  echo "FAIL: DEV host required"
  exit 1
}

LIVE="df-svc-director"
EXPECTED_BASE="sha256:a49aa79494f932b85c0547d52852c4a9dc63f1ad90edb13061d15d7b3263ed6e"
RUNTIME_ROOT="$HOME/workspace/desifaces-runtime"
ENV_FILE="$HOME/workspace/desifaces-v3/infra/.env"
COMPOSE_FILE="$RUNTIME_ROOT/docker-compose.yml"
NETWORK="df-net"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PATCHED="/tmp/next3-shared-scene-routes-${STAMP}.py"
LIVE_SRC="/tmp/next3-shared-scene-routes-live-${STAMP}.py"
CANDIDATE="desifaces-svc-director-next3-lineage:${STAMP}"
CANARY="df-svc-director-next3-lineage-canary"
ENV_SNAPSHOT="/tmp/df-svc-director-lineage-${STAMP}.env"
LOG="/tmp/next3-lineage-surgical-${STAMP}.log"

fail(){ echo "FAIL: $*" >&2; exit 1; }

exec > >(tee "$LOG") 2>&1

echo "============================================================"
echo " NEXT3 — SURGICAL GROUP-PHOTO LINEAGE FIX"
echo "============================================================"
echo "log=$LOG"

[[ -f "$COMPOSE_FILE" ]] || fail "canonical compose missing: $COMPOSE_FILE"
[[ -f "$ENV_FILE" ]] || fail "runtime env missing: $ENV_FILE"

CURRENT="$(docker inspect "$LIVE" --format '{{.Image}}')"
[[ "$CURRENT" == "$EXPECTED_BASE" ]] || fail "Director image changed: $CURRENT"
echo "BASE_IMAGE_VERIFIED=$CURRENT"

docker cp "$LIVE:/app/app/shared_scene_routes.py" "$LIVE_SRC"
cp "$LIVE_SRC" "$PATCHED"

python3 - "$PATCHED" <<'PY'
from pathlib import Path
import sys

p=Path(sys.argv[1])
s=p.read_text()

old="""            media = await conn.fetchrow(
                \"\"\"
                select id,user_id,account_id,project_id,kind,lifecycle_state,meta_json
                from public.media_assets
                where id=$1 and user_id=$2 and account_id=$3
                  and (project_id is null or project_id=$4)
                  and kind in ('image','source_image','face_image','face_source_image')
                  and lifecycle_state='active'
                \"\"\",
                body.shared_scene_media_id,
                auth.user_id,
                auth.account_id,
                stage[\"project_id\"],
            )"""

new="""            media = await conn.fetchrow(
                \"\"\"
                select id,user_id,account_id,project_id,kind,lifecycle_state,meta_json
                from public.media_assets
                where id=$1 and user_id=$2
                  and (account_id is null or account_id=$3)
                  and (project_id is null or project_id=$4)
                  and kind in ('image','source_image','face_image','face_source_image')
                  and lifecycle_state='active'
                for update
                \"\"\",
                body.shared_scene_media_id,
                auth.user_id,
                auth.account_id,
                stage[\"project_id\"],
            )"""

if s.count(old) != 1:
    raise SystemExit(f"expected exactly one media ownership predicate, found {s.count(old)}")
s=s.replace(old,new,1)

anchor="""            if int(validation.get(\"expected_speakers\") or 0) != len(speaking_ids):
                raise HTTPException(
                    status_code=422,
                    detail={
                        \"code\": \"shared_scene_media_speaker_count_validation_mismatch\",
                        \"message\": \"The photo validation no longer matches the number of speakers in this conversation. Validate the photo again.\",
                        \"recoverable\": True,
                        \"action\": \"validate_group_photo\",
                    },
                )

            member_rows = await conn.fetch("""

insert="""            if int(validation.get(\"expected_speakers\") or 0) != len(speaking_ids):
                raise HTTPException(
                    status_code=422,
                    detail={
                        \"code\": \"shared_scene_media_speaker_count_validation_mismatch\",
                        \"message\": \"The photo validation no longer matches the number of speakers in this conversation. Validate the photo again.\",
                        \"recoverable\": True,
                        \"action\": \"validate_group_photo\",
                    },
                )

            # Adopt only missing account/project lineage for a user-owned,
            # active, validated group-photo asset. Existing non-NULL lineage
            # cannot be overwritten because the SELECT predicate above rejects
            # account/project mismatches.
            await conn.execute(
                \"\"\"
                update public.media_assets
                set account_id=coalesce(account_id,$2),
                    project_id=coalesce(project_id,$3),
                    updated_at=now()
                where id=$1
                \"\"\",
                body.shared_scene_media_id,
                auth.account_id,
                stage[\"project_id\"],
            )

            member_rows = await conn.fetch("""

if s.count(anchor) != 1:
    raise SystemExit(f"expected exactly one validation anchor, found {s.count(anchor)}")
s=s.replace(anchor,insert,1)

p.write_text(s)
print("PATCH_APPLIED=PASS")
PY

python3 -m py_compile "$PATCHED"

python3 - "$PATCHED" <<'PY'
from pathlib import Path
import sys
s=Path(sys.argv[1]).read_text()
assert "where id=$1 and user_id=$2\n                  and (account_id is null or account_id=$3)" in s
assert "set account_id=coalesce(account_id,$2)" in s
assert "project_id=coalesce(project_id,$3)" in s
assert "where id=$1 and user_id=$2 and account_id=$3" not in s
print("SOURCE_CONTRACT=PASS")
PY

echo
echo "===== TARGET DIFF ====="
diff -u "$LIVE_SRC" "$PATCHED" || true

echo
echo "===== BUILD SURGICAL IMAGE ====="
PREP="df-director-lineage-prep-${STAMP}"
docker create --name "$PREP" "$EXPECTED_BASE" >/dev/null
docker cp "$PATCHED" "$PREP:/app/app/shared_scene_routes.py"
docker commit "$PREP" "$CANDIDATE" >/dev/null
docker rm "$PREP" >/dev/null
CANDIDATE_ID="$(docker image inspect "$CANDIDATE" --format '{{.Id}}')"
echo "CANDIDATE_IMAGE=$CANDIDATE_ID"

echo
echo "===== BYTE-IDENTICAL NON-TARGET CODE ====="
docker run --rm --entrypoint sh "$EXPECTED_BASE" -c   'find /app/app -type f -name "*.py" ! -path "/app/app/shared_scene_routes.py" -exec sha256sum {} \; | sort'   > /tmp/next3-lineage-base-files.txt

docker run --rm --entrypoint sh "$CANDIDATE" -c   'find /app/app -type f -name "*.py" ! -path "/app/app/shared_scene_routes.py" -exec sha256sum {} \; | sort'   > /tmp/next3-lineage-candidate-files.txt

diff -u /tmp/next3-lineage-base-files.txt /tmp/next3-lineage-candidate-files.txt
echo "NON_TARGET_DIRECTOR_CODE=BYTE_IDENTICAL"

echo
echo "===== CANARY ====="
umask 077
docker inspect "$LIVE" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$ENV_SNAPSHOT"
docker rm -f "$CANARY" >/dev/null 2>&1 || true

docker run -d   --name "$CANARY"   --network "$NETWORK"   --env-file "$ENV_SNAPSHOT"   --restart no   "$CANDIDATE" >/dev/null

CANARY_OK=0
for _ in $(seq 1 45); do
  if docker exec "$CANARY" sh -lc     'curl -fsS http://127.0.0.1:${PORT:-8011}/api/health >/dev/null' 2>/dev/null
  then
    CANARY_OK=1
    break
  fi
  sleep 2
done
(( CANARY_OK == 1 )) || {
  docker logs --tail 80 "$CANARY" 2>&1 || true
  fail "canary health failed"
}

docker exec "$CANARY" python -c '
from pathlib import Path
from app.main import app
s=Path("/app/app/shared_scene_routes.py").read_text()
assert "and (account_id is null or account_id=$3)" in s
assert "set account_id=coalesce(account_id,$2)" in s
assert any(getattr(r,"path","").endswith("/shared-scene") for r in app.routes)
print("CANARY_CONTRACT=PASS")
'
echo "CANARY_HEALTH=PASS"
docker rm -f "$CANARY" >/dev/null

echo
echo "===== PROMOTE THROUGH CANONICAL COMPOSE ====="
docker tag "$EXPECTED_BASE" "desifaces-svc-director-pre-lineage:${STAMP}"
docker tag "$CANDIDATE_ID" desifaces-svc-director:latest

rollback(){
  rc=$?
  set +e
  echo "===== AUTOMATIC DIRECTOR ROLLBACK ====="
  docker tag "$EXPECTED_BASE" desifaces-svc-director:latest
  DESIFACES_RUNTIME_ENV_FILE="$ENV_FILE"     docker compose       --project-directory "$RUNTIME_ROOT"       --env-file "$ENV_FILE"       -f "$COMPOSE_FILE"       up -d --no-build --no-deps --force-recreate svc-director >/dev/null 2>&1
  rm -f "$ENV_SNAPSHOT"
  exit "$rc"
}
trap rollback ERR

DESIFACES_RUNTIME_ENV_FILE="$ENV_FILE" docker compose   --project-directory "$RUNTIME_ROOT"   --env-file "$ENV_FILE"   -f "$COMPOSE_FILE"   up -d --no-build --no-deps --force-recreate svc-director >/dev/null

LIVE_OK=0
for _ in $(seq 1 45); do
  CODE="$(curl -sS -o /dev/null -w '%{http_code}'     --connect-timeout 2 --max-time 4     http://127.0.0.1:18011/api/health 2>/dev/null || true)"
  if [[ "$CODE" == "200" ]]; then LIVE_OK=1; break; fi
  sleep 2
done
(( LIVE_OK == 1 )) || fail "live Director health failed: HTTP_${CODE:-NO_RESPONSE}"

RUNNING="$(docker inspect "$LIVE" --format '{{.Image}}')"
[[ "$RUNNING" == "$CANDIDATE_ID" ]] || fail "live image mismatch: $RUNNING"

docker exec "$LIVE" python -c '
from pathlib import Path
s=Path("/app/app/shared_scene_routes.py").read_text()
assert "and (account_id is null or account_id=$3)" in s
assert "set account_id=coalesce(account_id,$2)" in s
print("LIVE_LINEAGE_FIX=PASS")
'

BAD="$(docker ps -a --format '{{.Names}}' | grep -i v3 || true)"
[[ -z "$BAD" ]] || { echo "$BAD"; fail "v3 container returned"; }

BADNET="$(docker network ls --format '{{.Name}}' | grep -i v3 || true)"
[[ -z "$BADNET" ]] || { echo "$BADNET"; fail "v3 network returned"; }

RESTARTING="$(docker ps --format '{{.Names}} {{.Status}}' | grep -E '^(df-svc-|df-web-dev ).*Restarting' || true)"
[[ -z "$RESTARTING" ]] || { echo "$RESTARTING"; fail "restart loop detected"; }

rm -f "$ENV_SNAPSHOT"
trap - ERR

echo
echo "============================================================"
echo " NEXT3_GROUP_PHOTO_LINEAGE_DEPLOY=PASS"
echo " previous_image=$EXPECTED_BASE"
echo " running_image=$RUNNING"
echo " only_code_changed=shared_scene_routes.py"
echo " canonical_network=df-net"
echo " zero_v3_runtime=PASS"
echo " production=UNTOUCHED"
echo "============================================================"
