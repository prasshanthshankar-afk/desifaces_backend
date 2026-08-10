#!/usr/bin/env bash
set -Eeuo pipefail

# Usage:
#   MODE=helper ./test_audio_gender_translation.sh
#   MODE=e2e RUN_CREATE=1 TOKEN='...' USER_ID='...' ./test_audio_gender_translation.sh
#
# helper mode:
#   Calls the validated shared gender translator directly. No desifaces credits.
#
# e2e mode:
#   Runs pricing preview, creates female and male Hindi TTS jobs, polls status,
#   and verifies grammatical gender plus persisted metadata. This creates two
#   billable Audio jobs.

MODE="${MODE:-helper}"
TEXT="${TEXT:-I am testing desifaces.ai}"
AUDIO_API_ROOT="${AUDIO_API_ROOT:-http://127.0.0.1:8004}"
WORKER_CONTAINER="${WORKER_CONTAINER:-df-svc-audio-worker}"
POLL_SECONDS="${POLL_SECONDS:-2}"
MAX_POLLS="${MAX_POLLS:-60}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

run_helper_test() {
  echo "Running non-billable shared translator test in ${WORKER_CONTAINER}..."

  docker exec -i \
    -e DF_TEST_TEXT="$TEXT" \
    "$WORKER_CONTAINER" python - <<'PY'
import asyncio
import os

from gender_translation import translate_with_gender


async def main() -> None:
    text = os.environ.get("DF_TEST_TEXT", "I am testing desifaces.ai")
    cases = (
        ("female", "रही"),
        ("male", "रहा"),
    )

    for gender, expected_fragment in cases:
        result = await translate_with_gender(
            text=text,
            source_language="en",
            target_language="hi",
            speaker_gender=gender,
            tone="neutral",
        )

        print(f"{gender}: {result.text}")
        print(f"provider={result.provider} model={result.model}")

        if expected_fragment not in result.text:
            raise SystemExit(
                f"FAIL: expected {expected_fragment!r} in {gender} translation"
            )
        if "desifaces.ai" not in result.text:
            raise SystemExit("FAIL: desifaces.ai was not preserved")

    print("PASS: helper returned correct female and male Hindi forms.")


asyncio.run(main())
PY
}

build_headers() {
  REQUEST_HEADERS=(
    -H "Authorization: Bearer ${TOKEN}"
    -H "Content-Type: application/json"
    -H "Accept: application/json"
  )
  if [[ -n "${USER_ID:-}" ]]; then
    REQUEST_HEADERS+=( -H "X-User-Id: ${USER_ID}" )
  fi
}

run_one_e2e_case() {
  local gender="$1"
  local voice="$2"
  local expected_fragment="$3"

  local payload preview quote_id fingerprint create_payload created job_id status_json status final_text

  payload="$(jq -n \
    --arg text "$TEXT" \
    --arg gender "$gender" \
    --arg voice "$voice" \
    '{
      text: $text,
      source_language: "en",
      target_locale: "hi-IN",
      translate: true,
      voice: $voice,
      voice_id: $voice,
      voice_locale: "hi-IN",
      speaker_gender: $gender,
      voice_gender: $gender,
      translation_tone: "neutral",
      output_format: "mp3"
    }')"

  echo
  echo "[$gender] Pricing preview..."
  preview="$(curl -fsS \
    "${REQUEST_HEADERS[@]}" \
    -X POST "${AUDIO_API_ROOT%/}/api/audio/tts/pricing/preview" \
    -d "$payload")"

  quote_id="$(jq -r '.quote_id // .pricing.quote_id // empty' <<<"$preview")"
  fingerprint="$(jq -r '.preview_fingerprint // .pricing.preview_fingerprint // empty' <<<"$preview")"

  [[ -n "$quote_id" ]] || fail "$gender preview did not return quote_id: $preview"
  echo "[$gender] Preview accepted. quote_id=${quote_id}"

  create_payload="$(jq \
    --arg quote_id "$quote_id" \
    --arg fingerprint "$fingerprint" \
    '. + {
      pricing_confirmation: (
        {quote_id: $quote_id}
        + (if $fingerprint == "" then {} else {preview_fingerprint: $fingerprint} end)
      )
    }' <<<"$payload")"

  echo "[$gender] Creating billable TTS job..."
  created="$(curl -fsS \
    "${REQUEST_HEADERS[@]}" \
    -X POST "${AUDIO_API_ROOT%/}/api/audio/tts" \
    -d "$create_payload")"

  job_id="$(jq -r '.job_id // empty' <<<"$created")"
  [[ -n "$job_id" ]] || fail "$gender create did not return job_id: $created"
  echo "[$gender] job_id=${job_id}"

  status=""
  for ((i=1; i<=MAX_POLLS; i++)); do
    status_json="$(curl -fsS \
      "${REQUEST_HEADERS[@]}" \
      "${AUDIO_API_ROOT%/}/api/audio/jobs/${job_id}/status")"

    status="$(jq -r '.status // empty' <<<"$status_json")"
    case "$status" in
      succeeded)
        break
        ;;
      failed|blocked|cancelled|canceled)
        jq . <<<"$status_json"
        fail "$gender job ended with status=$status"
        ;;
    esac

    sleep "$POLL_SECONDS"
  done

  [[ "$status" == "succeeded" ]] || fail "$gender job did not finish after ${MAX_POLLS} polls"

  final_text="$(jq -r '.payload.final_synthesis_text // empty' <<<"$status_json")"
  [[ -n "$final_text" ]] || fail "$gender job has no final_synthesis_text"

  echo "[$gender] final_synthesis_text=${final_text}"
  echo "[$gender] resolved metadata:"
  jq '{
    speaker_gender: .payload.speaker_gender,
    voice_gender: .payload.voice_gender,
    voice_locale: .payload.voice_locale,
    translation_provider: .payload.translation_provider,
    translation_model: .payload.translation_model,
    artifact_count: (.variants | length)
  }' <<<"$status_json"

  grep -Fq "$expected_fragment" <<<"$final_text" \
    || fail "$gender output did not contain expected Hindi form ${expected_fragment}"
  grep -Fq "desifaces.ai" <<<"$final_text" \
    || fail "$gender output did not preserve desifaces.ai"

  [[ "$(jq -r '.payload.speaker_gender // empty' <<<"$status_json")" == "$gender" ]] \
    || fail "$gender speaker_gender metadata mismatch"
  [[ "$(jq -r '.payload.voice_gender // empty' <<<"$status_json")" == "$gender" ]] \
    || fail "$gender voice_gender metadata mismatch"
  [[ "$(jq -r '.variants | length' <<<"$status_json")" -ge 1 ]] \
    || fail "$gender job produced no audio artifact"

  echo "[$gender] PASS"
}

run_e2e_test() {
  command -v curl >/dev/null || fail "curl is required"
  command -v jq >/dev/null || fail "jq is required"
  [[ "${RUN_CREATE:-0}" == "1" ]] \
    || fail "e2e mode creates two billable jobs; rerun with RUN_CREATE=1"
  [[ -n "${TOKEN:-}" ]] || fail "TOKEN is required for e2e mode"

  build_headers

  echo "WARNING: e2e mode creates two billable Audio jobs."
  run_one_e2e_case "female" "hi-IN-SwaraNeural" "रही"
  run_one_e2e_case "male" "hi-IN-MadhurNeural" "रहा"
  echo
  echo "PASS: female and male Audio jobs preserved grammatical gender end to end."
}

case "$MODE" in
  helper)
    run_helper_test
    ;;
  e2e)
    run_e2e_test
    ;;
  *)
    fail "MODE must be helper or e2e"
    ;;
esac
