#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
DIRECTOR="df-v3-svc-director"
FUSION_WORKER="df-v3-svc-fusion-worker"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
docker inspect "$DIRECTOR" >/dev/null 2>&1 || fail "$DIRECTOR missing"
docker inspect "$FUSION_WORKER" >/dev/null 2>&1 || fail "$FUSION_WORKER missing"

echo "============================================================"
echo " desifaces DEV — SHARED-SCENE PARALLELISM CERTIFICATION"
echo " production_touch=NONE"
echo "============================================================"

DIRECTOR_PARALLEL="$(docker exec "$DIRECTOR" sh -lc 'printf "%s" "${DF_DIRECTOR_FUSION_DISPATCH_CONCURRENCY:-32}"')"
WORKER_PARALLEL="$(docker exec "$FUSION_WORKER" sh -lc 'printf "%s" "${DF_FUSION_WORKER_CONCURRENCY:-4}"')"
SYNC_PARALLEL="$(docker exec "$FUSION_WORKER" sh -lc 'printf "%s" "${DF_SYNC3_PROVIDER_CONCURRENCY:-1}"')"

echo "DIRECTOR_DISPATCH_CONCURRENCY=$DIRECTOR_PARALLEL"
echo "FUSION_WORKER_CONCURRENCY=$WORKER_PARALLEL"
echo "SYNC3_PROVIDER_CONCURRENCY=$SYNC_PARALLEL"

[[ "$DIRECTOR_PARALLEL" =~ ^[0-9]+$ ]] || fail "invalid Director concurrency"
[[ "$WORKER_PARALLEL" =~ ^[0-9]+$ ]] || fail "invalid Fusion worker concurrency"
[[ "$SYNC_PARALLEL" =~ ^[0-9]+$ ]] || fail "invalid Sync3 concurrency"

(( DIRECTOR_PARALLEL >= 2 )) || fail "Director is serial"
(( WORKER_PARALLEL >= 2 )) || fail "Fusion worker is serial"
(( SYNC_PARALLEL >= 2 )) || fail "Sync3 provider path is serial; account/runtime concurrency must be increased before launch"

EFFECTIVE="$DIRECTOR_PARALLEL"
(( WORKER_PARALLEL < EFFECTIVE )) && EFFECTIVE="$WORKER_PARALLEL"
(( SYNC_PARALLEL < EFFECTIVE )) && EFFECTIVE="$SYNC_PARALLEL"

echo "EFFECTIVE_END_TO_END_PARALLELISM=$EFFECTIVE"
echo "SHARED_SCENE_PARALLELISM_CERTIFICATION=PASS"
echo "PRODUCTION_TOUCH=NONE"
