set -euo pipefail

BASE="http://localhost:8004"
AUTH="http://localhost:8000"

echo "=== sanity: svc-audio openapi has /api/audio/tts ==="
curl -sS "$BASE/openapi.json" | jq -e '.paths["/api/audio/tts"]' >/dev/null
echo "OK: /api/audio/tts exists on $BASE"

echo
echo "=== sanity: locales (should be India + en-US/en-GB) ==="
curl -sS "$BASE/api/audio/catalog/locales" | jq -r '.items[].locale' | head -n 20

echo
echo "=== login: get JWT from svc-core (8000) ==="
: "${AUTH_EMAIL:?Set AUTH_EMAIL env var}"
: "${AUTH_PASSWORD:?Set AUTH_PASSWORD env var}"

TOKEN=$(
  curl -sS -X POST "$AUTH/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$AUTH_EMAIL\",\"password\":\"$AUTH_PASSWORD\"}" \
  | jq -r '.access_token // .token // .data.access_token // empty'
)

if [ -z "$TOKEN" ]; then
  echo "Login did not return a token. Printing response:"
  curl -sS -X POST "$AUTH/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$AUTH_EMAIL\",\"password\":\"$AUTH_PASSWORD\"}" | jq
  exit 1
fi

echo "OK: TOKEN=${TOKEN:0:20}..."

echo
echo "=== create tts job ==="
JOB_ID=$(
  curl -sS -X POST "$BASE/api/audio/tts" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "text": "Vanakkam! This is a DesiFaces Audio Studio API test.",
      "target_locale": "ta-IN",
      "source_language": "en",
      "translate": true,
      "voice": null,
      "style": null,
      "style_degree": null,
      "rate": 1.0,
      "pitch": 0.0,
      "volume": 1.0,
      "context": null,
      "output_format": "mp3"
    }' | jq -r '.job_id // empty'
)

if [ -z "$JOB_ID" ]; then
  echo "Create job did not return job_id. Full response:"
  curl -sS -X POST "$BASE/api/audio/tts" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"text":"test","target_locale":"ta-IN"}' | jq
  exit 1
fi

echo "OK: JOB_ID=$JOB_ID"

echo
echo "=== poll job status ==="
FINAL=""
for i in $(seq 1 60); do
  R=$(curl -sS "$BASE/api/audio/jobs/$JOB_ID/status" -H "Authorization: Bearer $TOKEN")
  ST=$(echo "$R" | jq -r '.status // ""' | tr '[:upper:]' '[:lower:]')
  echo "[$i] status=$ST"

  if [ "$ST" = "succeeded" ] || [ "$ST" = "failed" ]; then
    FINAL="$R"
    break
  fi
  sleep 1
done

echo
echo "=== final status json ==="
echo "$FINAL" | jq

echo
echo "=== extract audio_url ==="
AUDIO_URL=$(echo "$FINAL" | jq -r '.variants[0].audio_url // empty')
if [ -z "$AUDIO_URL" ]; then
  echo "❌ variants[0].audio_url is missing."
  echo "This means: job says succeeded but no audio artifact was attached."
  exit 2
fi

echo "OK: AUDIO_URL=$AUDIO_URL"

echo
echo "=== HEAD audio_url ==="
curl -sS -I "$AUDIO_URL" | sed -n '1,25p'