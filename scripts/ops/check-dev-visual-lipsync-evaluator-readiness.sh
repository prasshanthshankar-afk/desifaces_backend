#!/usr/bin/env bash
# DEV-only evaluator readiness. No installs, credentials, downloads, or generation.
set -Eeuo pipefail
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 1; }
echo "=== DEV VISUAL LIPSYNC EVALUATOR READINESS ==="
for c in df-svc-fusion-extension df-svc-fusion-extension-worker df-svc-fusion-extension-stitch-worker; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "$c=ABSENT"; continue; }
  echo "=== $c ==="
  docker exec -i "$c" python - <<'PY'
import importlib.util, pathlib, shutil
for m in ("cv2","mediapipe","torch","torchaudio","onnxruntime","librosa","numpy"):
    try:
        present=importlib.util.find_spec(m) is not None
    except (ImportError,ValueError):
        present=False
    print(f"MODULE_{m.upper()}={'AVAILABLE' if present else 'ABSENT'}")
for name in ("ffmpeg","ffprobe"):
    print(f"TOOL_{name.upper()}={'AVAILABLE' if shutil.which(name) else 'ABSENT'}")
paths=("/app/models","/models","/opt/models","/app/checkpoints")
for p in paths:
    d=pathlib.Path(p)
    print(f"CHECKPOINT_DIRECTORY_{p}={'PRESENT' if d.exists() else 'ABSENT'}")
print("VALIDATED_AV_EVALUATOR=NOT_ESTABLISHED")
PY
done
echo "NO_DEPLOYMENT=YES"
echo "NO_GENERATION=YES"
