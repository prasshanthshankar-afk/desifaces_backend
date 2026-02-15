#!/usr/bin/env bash
set -euo pipefail

MUSIC_URL="${MUSIC_URL:-http://localhost:8007}"
TOKEN="${TOKEN:-}"

if [[ -z "$TOKEN" ]]; then
  echo "ERROR: TOKEN env var not set. Export TOKEN first." >&2
  exit 1
fi

RUN_DIR="${RUN_DIR:-/tmp/df_e2e_music_video_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$RUN_DIR"
echo "RUN_DIR=$RUN_DIR"

hdr="$RUN_DIR/hdr.txt"
out="$RUN_DIR/out.json"
openapi="$RUN_DIR/openapi.json"

curl_json() {
  local method="$1"; shift
  local url="$1"; shift
  local body_file="${1:-}"

  : >"$hdr"
  : >"$out"

  local code
  if [[ -n "$body_file" ]]; then
    code="$(curl -q -sS --max-time 60 --connect-timeout 10 \
      -D "$hdr" -o "$out" -w "%{http_code}" \
      -X "$method" "$url" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      --data "@$body_file")"
  else
    code="$(curl -q -sS --max-time 60 --connect-timeout 10 \
      -D "$hdr" -o "$out" -w "%{http_code}" \
      -X "$method" "$url" \
      -H "Authorization: Bearer $TOKEN")"
  fi

  echo "$code"
}

echo "[1/7] Fetch openapi..."
code="$(curl_json GET "$MUSIC_URL/openapi.json")"
if [[ "$code" != "200" ]]; then
  echo "openapi fetch failed code=$code"; sed -n '1,40p' "$hdr"; head -c 400 "$out"; echo
  exit 1
fi
cp "$out" "$openapi"

echo "[2/7] Discover endpoints..."
python3 - <<'PY' "$openapi" "$RUN_DIR/endpoints.json"
import json,sys,re
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
paths=j.get("paths") or {}
def has_post(p): return isinstance(paths.get(p),dict) and "post" in paths[p]
def has_get(p): return isinstance(paths.get(p),dict) and "get" in paths[p]

create_project=None
create_job=None
status=None
publish=None

# most likely shapes (but we’ll discover rather than assume)
for p in paths:
  if create_project is None and has_post(p) and re.fullmatch(r".*/api/music/projects/?", p):
    create_project=p
  if status is None and has_get(p) and re.search(r"/api/music/jobs/\{[^}]+\}/status/?$", p):
    status=p
  if publish is None and has_post(p) and re.search(r"/api/music/jobs/\{[^}]+\}/publish/?$", p):
    publish=p

# create_job: post under projects/{project_id}/...jobs...
cands=[]
for p in paths:
  if has_post(p) and "/api/music/projects/" in p and "{" in p and "}" in p and "/jobs" in p:
    cands.append(p)
# prefer something like /api/music/projects/{project_id}/jobs or /video_jobs
def score(p):
  s=0
  if p.endswith("/jobs"): s+=5
  if "video" in p: s+=2
  if p.count("{")==1: s+=2
  return s
cands=sorted(cands, key=score, reverse=True)
create_job=cands[0] if cands else None

ep={"create_project":create_project,"create_job":create_job,"status":status,"publish":publish,"create_job_candidates":cands[:10]}
json.dump(ep, open(sys.argv[2],"w",encoding="utf-8"), indent=2)
print(json.dumps(ep, indent=2))
PY

EP="$RUN_DIR/endpoints.json"
create_project="$(python3 -c 'import json; j=json.load(open("'"$EP"'")); print(j.get("create_project") or "")')"
create_job="$(python3 -c 'import json; j=json.load(open("'"$EP"'")); print(j.get("create_job") or "")')"
status_ep="$(python3 -c 'import json; j=json.load(open("'"$EP"'")); print(j.get("status") or "")')"
publish_ep="$(python3 -c 'import json; j=json.load(open("'"$EP"'")); print(j.get("publish") or "")')"

if [[ -z "$create_project" || -z "$create_job" || -z "$status_ep" || -z "$publish_ep" ]]; then
  echo "ERROR: Could not discover required endpoints. See $EP" >&2
  exit 1
fi

echo "[3/7] Create project..."
cat >"$RUN_DIR/create_project.json" <<'JSON'
{
  "title": "E2E Smoke Music Video",
  "mode": "autopilot",
  "duet_layout": "split_screen",
  "language_hint": "en-IN"
}
JSON

code="$(curl_json POST "$MUSIC_URL$create_project" "$RUN_DIR/create_project.json")"
if [[ "$code" != "200" && "$code" != "201" ]]; then
  echo "create project failed code=$code"; head -c 600 "$out"; echo; exit 1
fi
cp "$out" "$RUN_DIR/project_out.json"

PROJECT_ID="$(python3 - <<'PY' "$RUN_DIR/project_out.json"
import json,sys
j=json.load(open(sys.argv[1]))
pid=j.get("id") or j.get("project_id")
print(pid or "")
PY
)"
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: project_id not found in response: $RUN_DIR/project_out.json" >&2
  exit 1
fi
echo "PROJECT_ID=$PROJECT_ID"

echo "[4/7] Create video job..."
cat >"$RUN_DIR/create_job.json" <<'JSON'
{
  "outputs": ["full_mix", "timed_lyrics_json"],
  "provider_hints": {
    "title": "E2E Smoke Music Video",
    "genre": "pop",
    "mood": "uplifting",
    "tempo": "mid",
    "lyrics_source": "autopilot"
  }
}
JSON

# Substitute project_id into create_job path
job_path="${create_job/\{project_id\}/$PROJECT_ID}"
job_path="${job_path/\{id\}/$PROJECT_ID}"

code="$(curl_json POST "$MUSIC_URL$job_path" "$RUN_DIR/create_job.json")"
if [[ "$code" != "200" && "$code" != "201" ]]; then
  echo "create job failed code=$code"; head -c 900 "$out"; echo; exit 1
fi
cp "$out" "$RUN_DIR/job_out.json"

JID="$(python3 - <<'PY' "$RUN_DIR/job_out.json"
import json,sys
j=json.load(open(sys.argv[1]))
jid=j.get("job_id") or j.get("id")
print(jid or "")
PY
)"
if [[ -z "$JID" ]]; then
  echo "ERROR: job_id not found: $RUN_DIR/job_out.json" >&2
  exit 1
fi
echo "JID=$JID"

echo "[5/7] Poll job status..."
status_path="${status_ep/\{job_id\}/$JID}"
status_path="${status_path/\{id\}/$JID}"

for i in $(seq 1 240); do
  code="$(curl_json GET "$MUSIC_URL$status_path")"
  if [[ "$code" != "200" ]]; then
    echo "status failed code=$code"; head -c 400 "$out"; echo; sleep 2; continue
  fi
  cp "$out" "$RUN_DIR/status.json"
  python3 - <<'PY' "$RUN_DIR/status.json"
import json,sys
j=json.load(open(sys.argv[1]))
print("status:", j.get("status"), "stage:", j.get("stage"), "progress:", j.get("progress"))
PY

  st="$(python3 -c 'import json; j=json.load(open("'"$RUN_DIR/status.json"'")); print((j.get("status") or "").lower())')"
  if [[ "$st" == *"succeeded"* ]]; then
    echo "JOB SUCCEEDED"
    break
  fi
  if [[ "$st" == *"failed"* ]]; then
    echo "JOB FAILED. See $RUN_DIR/status.json" >&2
    exit 1
  fi
  sleep 2
done

echo "[6/7] Validate artifacts (audio + performer images)..."
python3 - <<'PY' "$RUN_DIR/status.json" "$RUN_DIR/artifacts.txt"
import json,sys
j=json.load(open(sys.argv[1]))
urls=[]
# look in tracks
for t in (j.get("tracks") or []):
  u=(t.get("url") or "").strip()
  if u.startswith("http"): urls.append(u)
# look in computed performer images
c=j.get("computed") or {}
for k in ("performer_a_image_url","performer_b_image_url"):
  u=str(c.get(k) or "").strip()
  if u.startswith("http"): urls.append(u)
for u in (c.get("performer_images") or []):
  u=str(u or "").strip()
  if u.startswith("http"): urls.append(u)
urls=sorted(set(urls))
open(sys.argv[2],"w").write("\n".join(urls))
print("\n".join(urls))
PY

while read -r u; do
  [[ -z "$u" ]] && continue
  # HEAD check (don’t download)
  curl -q -sS -I -L --max-time 25 "$u" | head -n 5
  echo "----"
done <"$RUN_DIR/artifacts.txt"

echo "[7/7] Publish (consent required)..."
publish_path="${publish_ep/\{job_id\}/$JID}"
publish_path="${publish_path/\{id\}/$JID}"

cat >"$RUN_DIR/publish.json" <<'JSON'
{
  "target": "fusion",
  "consent": { "accepted": true }
}
JSON

code="$(curl_json POST "$MUSIC_URL$publish_path" "$RUN_DIR/publish.json")"
if [[ "$code" != "200" ]]; then
  echo "publish failed code=$code"; head -c 800 "$out"; echo; exit 1
fi
cp "$out" "$RUN_DIR/publish_out.json"

python3 - <<'PY' "$RUN_DIR/publish_out.json"
import json,sys
j=json.load(open(sys.argv[1]))
print("publish_status:", j.get("status"))
fp=j.get("fusion_payload") or {}
audio=((fp.get("audio") or {}).get("url") or "")
print("fusion_payload.audio.url:", audio[:180])
PY

echo "DONE. Artifacts + responses in: $RUN_DIR"