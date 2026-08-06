#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
AUDIO_APP="$ROOT/services/svc-audio/app"
TEST_DIR="$ROOT/services/svc-audio/tests"

export PYTHONPATH="$AUDIO_APP"

echo "root=$ROOT"
echo "audio_app=$AUDIO_APP"
echo "python=$(command -v python3)"
python3 --version

echo
echo "===== SOURCE PROVENANCE ====="
python3 "$ROOT/deploy/audio-global/validate_source_provenance.py"

echo
echo "===== COMPILE ====="
python3 -m compileall -q \
  "$ROOT/services/svc-audio/app/app/repos/locale_catalog_repo.py" \
  "$ROOT/services/svc-audio/app/app/repos/locale_context_repo.py" \
  "$ROOT/services/svc-audio/app/app/repos/tts_catalog_repo.py" \
  "$ROOT/services/svc-audio/app/app/services/locale_resolver.py" \
  "$ROOT/services/svc-audio/app/app/services/locale_context_resolver.py" \
  "$ROOT/services/svc-audio/app/app/services/tts_model_resolver.py" \
  "$ROOT/services/svc-audio/app/app/services/tts_provider_adapter.py" \
  "$ROOT/services/svc-audio/app/app/services/azure_tts_adapter.py" \
  "$ROOT/services/svc-audio/app/app/services/tts_voice_resolver.py" \
  "$ROOT/services/svc-audio/app/app/services/tts_resolution_planner.py" \
  "$TEST_DIR"

echo "compile=PASS"

echo
echo "===== COMPLETE UNIT SUITE ====="
python3 -m unittest discover \
  -s "$TEST_DIR" \
  -p 'test_*.py' \
  -v
