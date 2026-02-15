#!/usr/bin/env bash
set -euo pipefail

MUSIC_URL="${MUSIC_URL:-http://localhost:8007}"
TOKEN="${TOKEN:-}"

if [[ -z "$TOKEN" ]]; then
  echo "ERROR: export TOKEN first" >&2
  exit 1
fi

RUN_DIR="${RUN_DIR:-/tmp/df_e2e_music_video_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$RUN_DIR"
echo "RUN_DIR=$RUN_DIR"

HDR="$RUN_DIR/hdr.txt"
OUT="$RUN_DIR/out.json"
OPENAPI="$RUN_DIR/openapi.json"

curl_json() {
  local method="$1"; shift
  local url="$1"; shift
  local body_file="${1:-}"

  : >"$HDR"
  : >"$OUT"

  local code
  if [[ -n "$body_file" ]]; then
    code="$(curl -q -sS --max-time 60 --connect-timeout 10 \
      -D "$HDR" -o "$OUT" -w "%{http_code}" \
      -X "$method" "$url" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      --data "@$body_file")"
  else
    code="$(curl -q -sS --max-time 60 --connect-timeout 10 \
      -D "$HDR" -o "$OUT" -w "%{http_code}" \
      -X "$method" "$url" \
      -H "Authorization: Bearer $TOKEN")"
  fi
  echo "$code"
}

echo "[1/8] Fetch OpenAPI..."
code="$(curl_json GET "$MUSIC_URL/openapi.json")"
if [[ "$code" != "200" ]]; then
  echo "openapi fetch failed code=$code" >&2
  sed -n '1,40p' "$HDR" >&2
  head -c 400 "$OUT" >&2; echo >&2
  exit 1
fi
cp "$OUT" "$OPENAPI"

echo "[2/8] Confirm endpoints exist (create_project + generate + status + publish)..."
python3 - <<'PY' "$OPENAPI"
import json,sys
j=json.load(open(sys.argv[1],"r",encoding="utf-8"))
paths=j.get("paths") or {}
need=[
  ("POST","/api/music/projects"),
  ("POST","/api/music/projects/{project_id}/generate"),
  ("GET" ,"/api/music/jobs/{job_id}/status"),
  ("POST","/api/music/jobs/{job_id}/publish"),
]
missing=[]
for m,p in need:
  ops=paths.get(p) or {}
  if m.lower() not in ops:
    missing.append((m,p))
if missing:
  raise SystemExit("Missing endpoints in OpenAPI: "+", ".join([f"{m} {p}" for m,p in missing]))
print("OK: required endpoints present")
PY

echo "[3/8] Build request payloads from OpenAPI schemas (filtered, deterministic)..."
python3 - <<'PY' "$OPENAPI" "$RUN_DIR/create_project.json" "$RUN_DIR/generate.json" "$RUN_DIR/publish.json"
import json,sys

openapi=json.load(open(sys.argv[1],"r",encoding="utf-8"))
schemas=(openapi.get("components") or {}).get("schemas") or {}

def ref_name(ref: str) -> str:
    # "#/components/schemas/Foo" -> "Foo"
    return ref.rsplit("/",1)[-1].strip()

def resolve(schema):
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        return resolve(schemas.get(ref_name(schema["$ref"]), {}))
    if "allOf" in schema and isinstance(schema["allOf"], list):
        merged={"type":"object","properties":{}, "required":[]}
        for part in schema["allOf"]:
            r=resolve(part)
            if (r.get("type")=="object") and isinstance(r.get("properties"),dict):
                merged["properties"].update(r["properties"])
                merged["required"]=sorted(set((merged.get("required") or []) + (r.get("required") or [])))
            else:
                # best effort: keep whatever
                pass
        return merged
    if "oneOf" in schema and isinstance(schema["oneOf"], list) and schema["oneOf"]:
        return resolve(schema["oneOf"][0])
    if "anyOf" in schema and isinstance(schema["anyOf"], list) and schema["anyOf"]:
        return resolve(schema["anyOf"][0])
    return schema

def pick_enum(prop_schema, preferred=None):
    enum=prop_schema.get("enum") if isinstance(prop_schema,dict) else None
    if not enum or not isinstance(enum,list):
        return None
    enum=[str(x) for x in enum if str(x).strip()]
    if not enum: return None
    preferred = preferred or []
    for p in preferred:
        if p in enum:
            return p
    return enum[0]

def skeleton(schema_name: str, preferred_values: dict) -> dict:
    s=resolve(schemas.get(schema_name, {}))
    props=s.get("properties") or {}
    req=set(s.get("required") or [])
    out={}
    for k,ps in props.items():
        if k in preferred_values:
            out[k]=preferred_values[k]
        else:
            # fill required with safe placeholders
            if k in req:
                t=(ps.get("type") if isinstance(ps,dict) else None)
                if "enum" in (ps or {}):
                    out[k]=pick_enum(ps, preferred=[])
                elif t=="string":
                    out[k]=""
                elif t=="integer":
                    out[k]=0
                elif t=="number":
                    out[k]=0
                elif t=="boolean":
                    out[k]=False
                elif t=="array":
                    out[k]=[]
                elif t=="object" or isinstance(ps.get("properties"),dict):
                    out[k]={}
                else:
                    out[k]=None
    # now filter preferred_values to keys that exist in schema
    for k,v in preferred_values.items():
        if k in props:
            out[k]=v
    # remove nulls if not required
    out2={}
    for k,v in out.items():
        if v is None and k not in req:
            continue
        out2[k]=v
    return out2

# Preferred deterministic values
create_project_pref={
  "title":"E2E Smoke Music Video",
  "mode":"autopilot",
  "duet_layout":"split_screen",
  "language_hint":"en-IN",
  "camera_edit":"beat_cut",
  "band_pack":[]
}
generate_pref={
  # common fields in your experiments; script will keep only those present in schema
  "outputs":["full_mix","timed_lyrics_json"],
  "provider_hints":{
    "title":"E2E Smoke Music Video",
    "genre":"pop",
    "mood":"uplifting",
    "tempo":"mid",
    "lyrics_source":"autopilot",
    "deterministic_seed": 1337,
    "request_nonce": "e2e_1337"
  }
}
publish_pref={
  "target":"fusion",
  "consent":{"accepted":True}
}

cp = skeleton("CreateMusicProjectIn", create_project_pref)
gj = skeleton("GenerateMusicIn", generate_pref)
pj = skeleton("PublishMusicIn", publish_pref)

json.dump(cp, open(sys.argv[2],"w",encoding="utf-8"), indent=2)
json.dump(gj, open(sys.argv[3],"w",encoding="utf-8"), indent=2)
json.dump(pj, open(sys.argv[4],"w",encoding="utf-8"), indent=2)

print("Wrote:")
print(" -", sys.argv[2])
print(" -", sys.argv[3])
print(" -", sys.argv[4])
PY

echo "[4/8] Create project..."
code="$(curl_json POST "$MUSIC_URL/api/music/projects" "$RUN_DIR/create_project.json")"
if [[ "$code" != "200" && "$code" != "201" ]]; then
  echo "create project failed code=$code" >&2
  head -c 1200 "$OUT" >&2; echo >&2
  exit 1
fi
cp "$OUT" "$RUN_DIR/project_out.json"

PROJECT_ID="$(python3 - <<'PY' "$RUN_DIR/project_out.json"
import json,sys
j=json.load(open(sys.argv[1]))
print(j.get("id") or j.get("project_id") or "")
PY
)"
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: could not extract project id. See $RUN_DIR/project_out.json" >&2
  exit 1
fi
echo "PROJECT_ID=$PROJECT_ID"

echo "[5/8] Generate (creates job) via /projects/{project_id}/generate ..."
code="$(curl_json POST "$MUSIC_URL/api/music/projects/$PROJECT_ID/generate" "$RUN_DIR/generate.json")"
if [[ "$code" != "200" && "$code" != "201" ]]; then
  echo "generate failed code=$code" >&2
  head -c 2000 "$OUT" >&2; echo >&2
  echo "payload: $RUN_DIR/generate.json" >&2
  exit 1
fi
cp "$OUT" "$RUN_DIR/generate_out.json"

JID="$(python3 - <<'PY' "$RUN_DIR/generate_out.json"
import json,sys
j=json.load(open(sys.argv[1]))
# tolerate different response shapes
print(j.get("job_id") or j.get("id") or (j.get("job") or {}).get("job_id") or "")
PY
)"
if [[ -z "$JID" ]]; then
  echo "ERROR: could not extract job_id. See $RUN_DIR/generate_out.json" >&2
  exit 1
fi
echo "JID=$JID"

echo "[6/8] Poll status..."
STATUS_URL="$MUSIC_URL/api/music/jobs/$JID/status"
for i in $(seq 1 240); do
  code="$(curl_json GET "$STATUS_URL")"
  if [[ "$code" != "200" ]]; then
    echo "status http $code (retrying)..." >&2
    sleep 2
    continue
  fi
  cp "$OUT" "$RUN_DIR/status.json"
  st="$(python3 -c 'import json; j=json.load(open("'"$RUN_DIR/status.json"'")); print((j.get("status") or "").lower())')"
  stage="$(python3 -c 'import json; j=json.load(open("'"$RUN_DIR/status.json"'")); print(j.get("stage"))')"
  prog="$(python3 -c 'import json; j=json.load(open("'"$RUN_DIR/status.json"'")); print(j.get("progress"))')"
  echo "status=$st stage=$stage progress=$prog"

  if [[ "$st" == *"succeeded"* ]]; then
    echo "JOB SUCCEEDED"
    break
  fi
  if [[ "$st" == *"failed"* ]]; then
    echo "JOB FAILED. See $RUN_DIR/status.json" >&2
    python3 - <<'PY' "$RUN_DIR/status.json"
import json,sys
j=json.load(open(sys.argv[1]))
print("error:", j.get("error"))
PY
    exit 1
  fi
  sleep 2
done

echo "[7/8] Extract any URLs + HEAD check..."
python3 - <<'PY' "$RUN_DIR/status.json" "$RUN_DIR/urls.txt"
import json,sys
j=json.load(open(sys.argv[1]))
urls=set()

def walk(x):
    if isinstance(x, dict):
        for v in x.values(): walk(v)
    elif isinstance(x, list):
        for v in x: walk(v)
    elif isinstance(x, str) and x.startswith("http"):
        urls.add(x)

walk(j)
urls=sorted(urls)
open(sys.argv[2],"w").write("\n".join(urls))
print("\n".join(urls))
PY

while read -r u; do
  [[ -z "$u" ]] && continue
  echo "HEAD $u"
  curl -q -sS -I -L --max-time 25 "$u" | head -n 5
  echo "----"
done <"$RUN_DIR/urls.txt"

echo "[8/8] Publish (consent accepted)..."
code="$(curl_json POST "$MUSIC_URL/api/music/jobs/$JID/publish" "$RUN_DIR/publish.json")"
if [[ "$code" != "200" ]]; then
  echo "publish failed code=$code" >&2
  head -c 2000 "$OUT" >&2; echo >&2
  exit 1
fi
cp "$OUT" "$RUN_DIR/publish_out.json"

python3 - <<'PY' "$RUN_DIR/publish_out.json"
import json,sys
j=json.load(open(sys.argv[1]))
print("publish_status:", j.get("status"))
fp=j.get("fusion_payload") or {}
audio=((fp.get("audio") or {}).get("url") or "")
print("fusion_payload.audio.url:", audio[:220])
PY

echo "DONE. Artifacts in: $RUN_DIR"