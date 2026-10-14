#!/usr/bin/env bash
set -Eeuo pipefail
# Read-only certification evidence. Do not print secrets or environment values.
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: desifaces-dev required"; exit 1; }
echo "=== DEV SARVAM / VIDEO QC PREFLIGHT (READ ONLY) ==="
printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'host=%s\n' "$(hostname -s)"
if [[ -d "$HOME/workspace/desifaces-runtime/.git" ]]; then
  git -C "$HOME/workspace/desifaces-runtime" rev-parse HEAD | sed 's/^/runtime_source_sha=/'
fi
for c in df-v3-svc-audio df-svc-audio df-v3-svc-audio-worker df-v3-svc-fusion-extension df-svc-fusion-extension df-v3-svc-fusion-extension-worker df-svc-fusion-extension-worker; do
  if docker inspect "$c" >/dev/null 2>&1; then
    docker inspect --format 'container={{.Name}} status={{.State.Status}} image_id={{.Image}} image_name={{.Config.Image}}' "$c"
    case "$c" in
      *svc-audio*)
        docker exec "$c" python -c 'import os; print("sarvam_credential_present=" + str(bool(os.getenv("SARVAM_API_KEY")))); print("dev_sarvam_flag=" + ("configured" if "DF_DEV_SARVAM_INDIAN_SPEECH_ENABLED" in os.environ else "unset"))' 2>/dev/null || echo "audio_environment_check=unavailable"
        ;;
    esac
  else
    echo "container_absent=$c"
  fi
done
echo "NOT_CERTIFIED: this report verifies runtime identity only; no DB routing, synthesis or video quality measured."
