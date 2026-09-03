#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${HOME}/.local/state/desifaces-v3/fusion-single-presenter-repair"
LOG_FILE="${STATE_DIR}/repair.log"
PID_FILE="${STATE_DIR}/repair.pid"

printf '%s\n' '============================================================'
printf '%s\n' ' V3 FUSION SINGLE-PRESENTER REPAIR — SAFE STATUS'
printf '%s\n' '============================================================'

pid=""
if [[ -f "$PID_FILE" ]]; then
  pid="$(tr -cd '0-9' < "$PID_FILE" | head -c 20 || true)"
fi

if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
  echo "VISUAL_REPAIR_PROCESS=RUNNING"
  echo "REPAIR_PID=$pid"
else
  echo "VISUAL_REPAIR_PROCESS=NOT_RUNNING"
fi

echo "LOG_FILE=$LOG_FILE"

if [[ ! -f "$LOG_FILE" ]]; then
  echo "STATUS_LOG_PRESENT=NO"
  exit 0
fi

echo "STATUS_LOG_PRESENT=YES"
echo "LOG_BYTES=$(wc -c < "$LOG_FILE" | tr -d ' ')"

echo '---------------- KEY MILESTONES ----------------'
# Whitelist only compact markers; do not dump raw ffmpeg/python output or SAS URLs.
grep -E '^(PRE_REPAIR_DURABLE_STATE_GATE|ATTEMPT_NO|PROVIDER_CHILDREN=|PROVIDER_CHILDREN_SUCCEEDED=|PARENT_CONSUME_EVENTS=|PARENT_CREDIT_DELTA=|AZURE_CONTAINER=|AZURE_CONTAINER_RESOLVE=|SOURCE_FACE_AUDIT |SOURCE_FACE_STACKED_PARTICIPANTS=|PRESERVED_CHILDREN_DOWNLOADED=|SEGMENT_AUDIT |DUPLICATE_SEGMENT_COUNT=|DUPLICATE_SEQUENCE_NOS=|REPAIRED_SEGMENTS_READY=|PROVIDER_RERENDER=|NO_FUSION_RETRY_CREATED=|NO_PROVIDER_RERENDER=|NO_PRICING_CHANGE=|POST_REPAIR_DURABLE_STATE_GATE=|VISUAL_REPAIR_CANDIDATE=|CANDIDATE_CONTAINER=|CANDIDATE_BLOB=|ERROR:)' "$LOG_FILE" \
  | tail -n 80 \
  | cut -c 1-320 \
  || true

echo '---------------- LAST SAFE EVENT ----------------'
# Last bounded non-secret event only. REVIEW_URL is deliberately excluded.
grep -Ev '^(REVIEW_URL=|https://.*[?&](sig|se|sp|sv)=)' "$LOG_FILE" \
  | tail -n 1 \
  | cut -c 1-320 \
  || true
