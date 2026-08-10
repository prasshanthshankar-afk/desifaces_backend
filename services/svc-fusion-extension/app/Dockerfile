FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# rembg / u2net cache configuration
ENV U2NET_HOME=/opt/rembg-models
ENV REMBG_MODEL=u2net

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    ca-certificates \
    ffmpeg \
    findutils \
    && rm -rf /var/lib/apt/lists/*

# With repo-root docker build context, requirements lives under app/app
COPY services/svc-fusion-extension/app/app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy only what this service needs plus repo content used to locate desifaces_shared
COPY services/svc-fusion-extension/app/app /app/app
COPY . /repo

# Make desifaces_shared importable even if it is not at repo root.
RUN set -eux; \
    shared_dir="$(find /repo -type d -name desifaces_shared | head -n 1)"; \
    if [ -z "$shared_dir" ]; then \
      echo "desifaces_shared not found under build context" >&2; \
      find /repo -maxdepth 4 -type d | sort | head -200 >&2; \
      exit 1; \
    fi; \
    ln -s "$shared_dir" /app/desifaces_shared

ENV PYTHONPATH=/app:/repo

# Prepare rembg model cache directory.
RUN mkdir -p "${U2NET_HOME}"

# Prewarm rembg model at build time so Premium matte composition does not pay
# the first-run download penalty during a live job.
RUN python - <<'PY'
import os
from pathlib import Path

from rembg import new_session

model_name = os.getenv("REMBG_MODEL", "u2net").strip() or "u2net"
model_home = Path(os.getenv("U2NET_HOME", "/opt/rembg-models"))
model_home.mkdir(parents=True, exist_ok=True)

print(f"[docker-prewarm] U2NET_HOME={model_home}")
print(f"[docker-prewarm] model={model_name}")

_ = new_session(model_name)

expected = model_home / f"{model_name}.onnx"
if expected.exists():
    print(f"[docker-prewarm] ready: {expected}")
else:
    print(f"[docker-prewarm] model session initialized; expected file not found exactly at {expected}")
PY

# Startup wrapper:
# - if U2NET_HOME is mounted as an empty volume, repopulate the model cache once
# - then continue with the original service CMD unchanged
RUN cat > /usr/local/bin/df-rembg-entrypoint.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail

U2NET_HOME="${U2NET_HOME:-/opt/rembg-models}"
REMBG_MODEL="${REMBG_MODEL:-u2net}"
MODEL_PATH="${U2NET_HOME}/${REMBG_MODEL}.onnx"

mkdir -p "${U2NET_HOME}"

if [ ! -f "${MODEL_PATH}" ]; then
  echo "[df-rembg-entrypoint] warming rembg model ${REMBG_MODEL} into ${U2NET_HOME}"
  python - <<'PY'
import os
from pathlib import Path

from rembg import new_session

model_name = os.getenv("REMBG_MODEL", "u2net").strip() or "u2net"
model_home = Path(os.getenv("U2NET_HOME", "/opt/rembg-models"))
model_home.mkdir(parents=True, exist_ok=True)

print(f"[entrypoint-prewarm] U2NET_HOME={model_home}")
print(f"[entrypoint-prewarm] model={model_name}")

_ = new_session(model_name)

expected = model_home / f"{model_name}.onnx"
if expected.exists():
    print(f"[entrypoint-prewarm] ready: {expected}")
else:
    print(f"[entrypoint-prewarm] model session initialized; expected file not found exactly at {expected}")
PY
else
  echo "[df-rembg-entrypoint] rembg model already present at ${MODEL_PATH}"
fi

exec "$@"
SH

RUN chmod +x /usr/local/bin/df-rembg-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/df-rembg-entrypoint.sh"]
CMD ["bash", "-lc", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8006}"]
