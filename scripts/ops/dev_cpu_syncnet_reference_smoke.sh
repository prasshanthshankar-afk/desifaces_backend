#!/usr/bin/env bash
# DEV ONLY. Isolated reference SyncNet CPU demo smoke test, not desifaces certification.
set -Eeuo pipefail
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "DEV_HOST_REQUIRED" >&2; exit 2; }
for bin in docker git curl; do command -v "$bin" >/dev/null; done
FREE_KIB="$(df -Pk /var/lib/docker | awk 'NR==2 {print $4}')"
if (( FREE_KIB < 5*1024*1024 )); then
  echo "BLOCKED_INSUFFICIENT_DOCKER_DISK_FREE_KIB=$FREE_KIB"; exit 2
fi
BASE="$HOME/desifaces-qc-evidence/syncnet-reference-demo"
mkdir -p "$BASE"
chmod 700 "$BASE"
TMP="$(mktemp -d "$BASE/isolated.XXXXXXXX")"
chmod 700 "$TMP"
echo "TEST_LOCATION=$TMP"
echo "SYNCNET_COMMERCIAL_RIGHTS_NOT_ESTABLISHED=YES"
echo "STAGE_1_CLONE_REFERENCE_SOURCE"
git clone -q --depth 1 https://github.com/joonson/syncnet_python.git "$TMP/src"
git -C "$TMP/src" rev-parse HEAD | tee "$TMP/reference_git_sha.txt"
echo "STAGE_2_DOWNLOAD_REFERENCE_WEIGHTS_AND_EXAMPLE"
mkdir -p "$TMP/src/data"
for pair in "syncnet_v2.model" "example.avi"; do
  curl -fL --retry 2 --connect-timeout 12 --max-time 180 \
    "https://www.robots.ox.ac.uk/~vgg/software/lipsync/data/$pair" \
    -o "$TMP/src/data/$pair"
  test -s "$TMP/src/data/$pair"
  sha256sum "$TMP/src/data/$pair"
done
cat > "$TMP/Dockerfile" <<'DOCKER'
FROM python:3.10-slim
ENV PIP_NO_CACHE_DIR=1 OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 CUDA_VISIBLE_DEVICES=""
RUN apt-get update -qq && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --disable-pip-version-check \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.5.1
RUN pip install --disable-pip-version-check \
    numpy==1.26.4 scipy==1.11.4 opencv-python-headless==4.10.0.84 \
    python_speech_features==0.6
WORKDIR /eval
ENTRYPOINT ["python"]
DOCKER
echo "STAGE_3_BUILD_TEMPORARY_CPU_IMAGE"
IMAGE="desifaces-syncnet-cpu-smoke:dev-20261016"
# Container build changes only the local DEV Docker image cache, never live services.
docker build --pull --tag "$IMAGE" "$TMP"
echo "STAGE_4_RUN_REFERENCE_DEMO"
set +e
docker run --rm --network none --cpus 2 --memory 3g --pids-limit 160 \
  --security-opt no-new-privileges --cap-drop ALL \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$TMP/src,dst=/eval,readonly" \
  --mount "type=bind,src=$TMP,dst=/evidence" \
  --entrypoint python "$IMAGE" demo_syncnet.py \
   --initial_model data/syncnet_v2.model --videofile data/example.avi \
   --tmp_dir /evidence/work --batch_size 4 --vshift 15 \
   >"$TMP/syncnet-demo.stdout.log" 2>"$TMP/syncnet-demo.stderr.log"
RC=$?
set -e
echo "DEMO_EXIT_CODE=$RC"
echo "===== REFERENCE MODEL OUTPUT (non-sensitive) ====="
grep -E 'AV offset:|Confidence:|Min dist:|Model .* loaded' \
  "$TMP/syncnet-demo.stderr.log" | tail -n 8 || true
if [[ "$RC" -ne 0 ]]; then
  echo "REFERENCE_DEMO=FAIL"
  echo "ERROR_TAIL:"
  tail -n 16 "$TMP/syncnet-demo.stderr.log"
  exit 1
fi
if ! grep -q 'AV offset:' "$TMP/syncnet-demo.stderr.log"; then
  echo "REFERENCE_DEMO=NO_OFFSET_OUTPUT"
  exit 1
fi
echo "REFERENCE_DEMO=EXECUTED"
echo "KNOWN_SHIFT_CALIBRATION=NOT_DONE"
echo "REAL_DESIFACES_LIPSYNC_MEASURED=NO"
echo "PRODUCTION_CHANGES=NONE"
echo "LOG_DIR=$TMP"
