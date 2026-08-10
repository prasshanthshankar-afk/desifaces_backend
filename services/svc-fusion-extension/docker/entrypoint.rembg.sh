#!/usr/bin/env bash
set -euo pipefail

U2NET_HOME="${U2NET_HOME:-/opt/rembg-models}"
REMBG_MODEL="${REMBG_MODEL:-u2net}"
MODEL_PATH="${U2NET_HOME}/${REMBG_MODEL}.onnx"

mkdir -p "${U2NET_HOME}"

if [ ! -f "${MODEL_PATH}" ]; then
  echo "[entrypoint.rembg] warming rembg model ${REMBG_MODEL} into ${U2NET_HOME}"
  python /app/app/scripts/prewarm_rembg.py
else
  echo "[entrypoint.rembg] rembg model already present at ${MODEL_PATH}"
fi

exec "$@"