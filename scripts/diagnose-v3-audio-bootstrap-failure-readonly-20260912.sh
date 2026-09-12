#!/usr/bin/env bash
set -Eeuo pipefail

AUDIO_C="df-v3-svc-audio"
EXPECTED_HOST="desifaces-dev"

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || { echo "FAIL wrong_host=$(hostname -s)"; exit 2; }
docker inspect "$AUDIO_C" >/dev/null 2>&1 || { echo "FAIL missing_container=$AUDIO_C"; exit 3; }

echo "============================================================"
echo " desifaces DEV — AUDIO BOOTSTRAP FAILURE DIAGNOSTIC (READ ONLY)"
echo "============================================================"
echo "mutation=NONE"
echo "production_touch=NONE"
echo "container=$AUDIO_C"
echo "state=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' "$AUDIO_C")"
echo "restart_count=$(docker inspect -f '{{.RestartCount}}' "$AUDIO_C")"
echo "image=$(docker inspect -f '{{.Image}}' "$AUDIO_C")"
echo "revision=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$(docker inspect -f '{{.Image}}' "$AUDIO_C")" 2>/dev/null || true)"

echo
echo "===== RUNTIME COMPATIBILITY ====="
docker exec -i "$AUDIO_C" python - <<'PY'
import importlib
checks = [
    ("app", None),
    ("app.db", "get_pool"),
    ("app.api.deps", "get_current_user_id"),
    ("app.services.azure_storage_service", "AzureStorageService"),
    ("asyncpg", None),
    ("azure.storage.blob", "generate_blob_sas"),
]
for mod, attr in checks:
    try:
        m=importlib.import_module(mod)
        ok = True if attr is None else hasattr(m, attr)
        print(f"CHECK {mod}{'.'+attr if attr else ''}={'PASS' if ok else 'MISSING_ATTR'}")
    except Exception as e:
        print(f"CHECK {mod}{'.'+attr if attr else ''}=IMPORT_FAIL:{type(e).__name__}:{e}")

try:
    import app.api as api
    import inspect
    src=inspect.getsource(api.build_router)
    print("BUILD_ROUTER_SOURCE_BEGIN")
    for line in src.splitlines():
        if "import" in line or "include_router" in line:
            print(line)
    print("BUILD_ROUTER_SOURCE_END")
except Exception as e:
    print(f"BUILD_ROUTER_INSPECT=FAIL:{type(e).__name__}:{e}")
PY

echo
echo "===== FAILED-BOOT TRACEBACK ====="
# The failed bootstrap occurred around 18:18Z. Docker retains logs across restarts,
# including the failed start followed by rollback. Show only the recent tail.
docker logs --since 30m "$AUDIO_C" 2>&1 | tail -n 260

echo
echo "============================================================"
echo " AUDIO BOOTSTRAP DIAGNOSTIC=COMPLETE"
echo " READ_ONLY=PASS"
echo "============================================================"
