#!/usr/bin/env bash
set -euo pipefail

: "${STUDIO_JOB_ID:?Set STUDIO_JOB_ID}"
: "${TOKEN:?Set TOKEN}"
BASE="${BASE:-http://localhost:8008}"

for i in $(seq 1 20); do
  # keep output tiny + safe
  body="$(curl -sS --max-time 10 --connect-timeout 5 \
    -H "Authorization: Bearer $TOKEN" \
    "$BASE/api/commerce/jobs/$STUDIO_JOB_ID/status" || true)"

  status="$(echo "$body" | jq -r '.status // .detail // "unknown"' 2>/dev/null || echo "unknown")"
  echo "poll[$i] status=$status"

  # stop early on terminal states
  if [[ "$status" == "succeeded" || "$status" == "failed" ]]; then
    break
  fi
  sleep 2
done