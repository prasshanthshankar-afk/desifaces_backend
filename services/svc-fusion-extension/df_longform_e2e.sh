#!/usr/bin/env bash
set -euo pipefail

# ===== YOU MUST SET THESE TWO =====
: "${USER_JWT:?Set USER_JWT='Bearer <user_access_token>'}"
: "${FACE_ARTIFACT_ID:?Set FACE_ARTIFACT_ID='<face_artifact_uuid>'}"

# ===== OPTIONAL (defaults are correct for your setup) =====
FUSION_EXT_BASE="${FUSION_EXT_BASE:-http://localhost:8006}"

echo ""
echo "=============================="
echo "DesiFaces Longform E2E Test"
echo "=============================="
echo "Fusion Extension Base: $FUSION_EXT_BASE"
echo ""

echo "1) Health check..."
curl -sS "$FUSION_EXT_BASE/api/health" | jq || true
echo ""

echo "2) Create longform job (this returns the JOB_ID)..."
CREATE_RES="$(curl -sS -H "Authorization: $USER_JWT" -H "Content-Type: application/json" \
  -d "{
    \"face_artifact_id\": \"${FACE_ARTIFACT_ID}\",
    \"aspect_ratio\": \"9:16\",
    \"voice_cfg\": {\"target_locale\":\"en-US\"},
    \"segment_seconds\": 12,
    \"max_segment_seconds\": 30,
    \"script_text\": \"Hello from segment one. Hello from segment two. Hello from segment three.\",
    \"tags\": {\"source\":\"e2e\"}
  }" \
  "$FUSION_EXT_BASE/api/longform/jobs")"

echo "$CREATE_RES" | jq

JOB_ID="$(echo "$CREATE_RES" | jq -r '.job_id // empty')"
if [[ -z "$JOB_ID" || "$JOB_ID" == "null" ]]; then
  echo ""
  echo "❌ Could not read job_id from response."
  echo "Response was:"
  echo "$CREATE_RES"
  exit 1
fi

echo ""
echo "✅ JOB_ID=$JOB_ID"
echo ""

echo "3) Poll job status until it finishes (or fails)..."
DEADLINE=$(( $(date +%s) + 1200 ))  # 20 minutes max
while true; do
  STATUS_RES="$(curl -sS -H "Authorization: $USER_JWT" "$FUSION_EXT_BASE/api/longform/jobs/$JOB_ID")"
  STATUS="$(echo "$STATUS_RES" | jq -r '.status // empty' | tr '[:upper:]' '[:lower:]')"

  echo "  status=$STATUS"
  if [[ "$STATUS" == "succeeded" ]]; then
    echo ""
    echo "✅ Longform job succeeded."
    echo "$STATUS_RES" | jq
    break
  fi

  if [[ "$STATUS" == "failed" ]]; then
    echo ""
    echo "❌ Longform job failed."
    echo "$STATUS_RES" | jq
    echo ""
    echo "Tip: run 'docker logs -n 200 df-svc-fusion-extension-worker' to see why."
    exit 2
  fi

  if (( $(date +%s) > DEADLINE )); then
    echo ""
    echo "❌ Timed out waiting for job completion."
    echo "$STATUS_RES" | jq
    exit 3
  fi

  sleep 5
done

echo ""
echo "4) Fetch segments list (videos per segment)..."
curl -sS -H "Authorization: $USER_JWT" "$FUSION_EXT_BASE/api/longform/jobs/$JOB_ID/segments" | jq

echo ""
echo "✅ E2E test done."
