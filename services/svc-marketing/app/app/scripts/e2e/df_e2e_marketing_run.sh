#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# svc-marketing E2E (macOS Bash 3.2 compatible)
#
# - health
# - login via svc-core /api/auth/login
# - fetch svc-marketing OpenAPI
# - discover POST /api/marketing/runs request schema
# - build valid payload:
#     - mode: enum (defaults to "stage" if allowed else first enum)
#     - recipe: required (defaults to first enum/object per schema)
# - create run
# - poll status (best GET endpoint discovered from OpenAPI)
# - optional publish (PUBLISH=1)
# =============================================================================

# -----------------------
# Config (override via env)
# -----------------------
CORE_URL="${CORE_URL:-http://svc-core:8000}"
MARKETING_URL="${MARKETING_URL:-http://svc-marketing:8009}"

DF_EMAIL="${DF_EMAIL:-}"
DF_PASSWORD="${DF_PASSWORD:-}"

# REQUIRED by svc-marketing (per your 422):
MODE="${MODE:-stage}"        # stage|publish
RECIPE="${RECIPE:-}"         # optional override; if empty, auto-picks from OpenAPI enum

# Optional
USE_CASE_ID="${USE_CASE_ID:-}"
PUBLISH="${PUBLISH:-0}"
POLL_SECS="${POLL_SECS:-3}"
TIMEOUT_SECS="${TIMEOUT_SECS:-900}"
DEBUG="${DEBUG:-0}"          # 1 = save headers and print more

if [[ -z "$DF_EMAIL" || -z "$DF_PASSWORD" ]]; then
  echo "ERROR: set DF_EMAIL and DF_PASSWORD"
  exit 2
fi

ts="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="/tmp/df_e2e_marketing_${ts}"
mkdir -p "$RUN_DIR"

echo "RUN_DIR=$RUN_DIR"
echo "CORE_URL=$CORE_URL"
echo "MARKETING_URL=$MARKETING_URL"
echo "MODE=$MODE"
echo "RECIPE=${RECIPE:-<auto>}"

py() { python3 - "$@"; }

AUTH_HEADER=""
XUID_HEADER=""
ACCESS_TOKEN=""
USER_ID=""

is_2xx() { [[ "${1:-}" =~ ^2 ]]; }

require_nonempty_file() {
  local p="$1"
  [[ -s "$p" ]] || { echo "ERROR: expected non-empty file: $p" >&2; return 1; }
}

require_json_file() {
  local p="$1"
  require_nonempty_file "$p" || return 1
  py "$p" <<'PY' >/dev/null
import json,sys
p=sys.argv[1]
s=open(p,"r",encoding="utf-8").read().strip()
if not s:
    raise SystemExit(f"empty: {p}")
json.loads(s)
PY
}

curl_json() {
  # usage: curl_json METHOD URL JSON_BODY OUTFILE [HDRFILE]
  local method="$1"; shift
  local url="$1"; shift
  local body="$1"; shift
  local outfile="$1"; shift
  local hdrfile="${1:-}"

  local tmp
  tmp="$(mktemp "${RUN_DIR}/_body.XXXXXX.json")"
  printf "%s" "$body" > "$tmp"

  local curl_args=(
    -sS -L
    -X "$method" "$url"
    -H "Content-Type: application/json"
    --data-binary @"$tmp"
    -o "$outfile"
    -w "%{http_code}"
  )

  [[ -n "$AUTH_HEADER" ]] && curl_args+=(-H "$AUTH_HEADER")
  [[ -n "$XUID_HEADER" ]] && curl_args+=(-H "$XUID_HEADER")
  [[ -n "$hdrfile" ]] && curl_args+=(-D "$hdrfile")

  local code
  code="$(curl "${curl_args[@]}" || true)"
  rm -f "$tmp" || true
  printf "%s\n" "$code"
}

curl_get() {
  # usage: curl_get URL OUTFILE [HDRFILE]
  local url="$1"; shift
  local outfile="$1"; shift
  local hdrfile="${1:-}"

  local curl_args=( -sS -L "$url" -o "$outfile" -w "%{http_code}" )
  [[ -n "$AUTH_HEADER" ]] && curl_args+=(-H "$AUTH_HEADER")
  [[ -n "$XUID_HEADER" ]] && curl_args+=(-H "$XUID_HEADER")
  [[ -n "$hdrfile" ]] && curl_args+=(-D "$hdrfile")

  local code
  code="$(curl "${curl_args[@]}" || true)"
  printf "%s\n" "$code"
}

extract_token_and_user() {
  local login_json="$1"
  require_json_file "$login_json" || { echo "ERROR: login response not JSON"; cat "$login_json"; exit 3; }

  ACCESS_TOKEN="$(py "$login_json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],"r",encoding="utf-8"))
print(d.get("access_token") or d.get("token") or d.get("jwt") or "")
PY
)"
  [[ -n "$ACCESS_TOKEN" ]] || { echo "ERROR: access_token missing"; cat "$login_json"; exit 3; }

  USER_ID="$(py "$login_json" <<'PY'
import json,sys,base64
d=json.load(open(sys.argv[1],"r",encoding="utf-8"))
for k in ("user_id","userId","uid","id"):
    v=d.get(k)
    if isinstance(v,str) and v:
        print(v); raise SystemExit(0)
tok=d.get("access_token") or d.get("token") or ""
parts=tok.split(".")
if len(parts)>=2:
    payload=parts[1]
    pad="="*((4-len(payload)%4)%4)
    payload=payload.replace("-","+").replace("_","/")+pad
    try:
        p=json.loads(base64.b64decode(payload).decode("utf-8"))
        sub=p.get("sub") or ""
        if isinstance(sub,str) and sub:
            print(sub); raise SystemExit(0)
    except Exception:
        pass
print("")
PY
)"

  ACCESS_TOKEN="${ACCESS_TOKEN#Bearer }"
  AUTH_HEADER="Authorization: Bearer ${ACCESS_TOKEN}"
  XUID_HEADER=""
  [[ -n "$USER_ID" ]] && XUID_HEADER="X-User-Id: ${USER_ID}"
}

fetch_openapi() {
  local out="$1"
  local hdr="${2:-}"
  local base="${MARKETING_URL%/}"
  local ok="0"
  for p in "/openapi.json" "/api/openapi.json"; do
    local code
    code="$(curl_get "${base}${p}" "$out" "$hdr")"
    if is_2xx "$code" && require_json_file "$out" >/dev/null 2>&1; then
      ok="1"
      echo "$p" > "${RUN_DIR}/00_openapi_path.txt"
      break
    fi
  done
  [[ "$ok" == "1" ]] || return 1
}

# Build a valid create payload from OpenAPI schema for POST /api/marketing/runs
build_create_plan() {
  local spec="$1"
  local out="$2"
  py "$spec" "$out" "$MODE" "$RECIPE" "$USE_CASE_ID" "$ts" "$USER_ID" <<'PY'
import json, sys, re
spec_path, out_path = sys.argv[1], sys.argv[2]
MODE, RECIPE, USE_CASE_ID, TS, USER_ID = sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7]

spec = json.load(open(spec_path, "r", encoding="utf-8"))
paths = spec.get("paths", {}) or {}
schemas = (spec.get("components", {}) or {}).get("schemas", {}) or {}

def resolve_ref(s):
    if not isinstance(s, dict): return s
    ref = s.get("$ref")
    if ref and ref.startswith("#/components/schemas/"):
        name = ref.split("/")[-1]
        return resolve_ref(schemas.get(name, {}))
    return s

def merge_allof(s):
    s = resolve_ref(s)
    if not isinstance(s, dict): return {}
    if "allOf" not in s: return s
    out = {"type":"object","properties":{}, "required":[]}
    for part in s.get("allOf", []):
        part = merge_allof(part)
        if not isinstance(part, dict): continue
        out["properties"].update(part.get("properties") or {})
        out["required"] = sorted(set(out["required"] + (part.get("required") or [])))
    # keep top-level hints
    for k in ("type","enum","format","default"):
        if k in s and k not in out:
            out[k] = s[k]
    return out

def schema_for_request(op):
    rb = op.get("requestBody") if isinstance(op, dict) else None
    if not isinstance(rb, dict): return None
    content = rb.get("content") or {}
    if "application/json" in content:
        sch = content["application/json"].get("schema")
        return merge_allof(sch) if sch else None
    for v in content.values():
        if isinstance(v, dict) and v.get("schema"):
            return merge_allof(v["schema"])
    return None

def pick_enum(schema, want=None):
    schema = resolve_ref(schema)
    enum = schema.get("enum") if isinstance(schema, dict) else None
    if isinstance(enum, list) and enum:
        if want and want in enum:
            return want
        return enum[0]
    return want

def default_for(schema):
    schema = resolve_ref(schema)
    if not isinstance(schema, dict): return None
    if "default" in schema: return schema["default"]
    t = schema.get("type")
    if schema.get("enum"):
        return pick_enum(schema)
    if t == "string":
        return ""
    if t == "boolean":
        return False
    if t == "integer":
        return 0
    if t == "number":
        return 0
    if t == "array":
        return []
    if t == "object":
        return {}
    return None

def build_object(schema, depth=0):
    schema = merge_allof(schema)
    if not isinstance(schema, dict): return {}
    props = schema.get("properties") or {}
    req = schema.get("required") or []
    out = {}

    # MODE + RECIPE special handling
    if "mode" in props:
        out["mode"] = pick_enum(props["mode"], MODE)
    if "recipe" in props:
        rsch = merge_allof(props["recipe"])
        if isinstance(rsch, dict) and rsch.get("enum"):
            out["recipe"] = pick_enum(rsch, RECIPE or None)
        elif isinstance(rsch, dict) and (rsch.get("type") == "object" or rsch.get("properties")):
            # recipe is an object; fill required fields
            out["recipe"] = build_object(rsch, depth+1)
            # if recipe object has "kind" enum, try to set it from RECIPE
            if isinstance(out["recipe"], dict) and "kind" in (rsch.get("properties") or {}):
                ksch = (rsch.get("properties") or {})["kind"]
                out["recipe"]["kind"] = pick_enum(ksch, RECIPE or None)
        else:
            out["recipe"] = RECIPE or default_for(rsch)

    # helpful extras
    if USE_CASE_ID and "use_case_id" in props:
        out["use_case_id"] = USE_CASE_ID
    if USER_ID and "user_id" in props:
        out["user_id"] = USER_ID

    # inputs blob (whichever exists)
    inputs_obj = {"e2e": True, "ts": TS}
    if "inputs" in props:
        out["inputs"] = inputs_obj
    if "inputs_json" in props:
        out["inputs_json"] = inputs_obj

    # fill required fields not set
    for k in req:
        if k in out:
            continue
        sch = props.get(k, {})
        sch = merge_allof(sch)
        if k == "mode":
            out[k] = pick_enum(sch, MODE)
        elif k == "recipe":
            # already handled above, but keep safe
            out[k] = pick_enum(sch, RECIPE or None) if isinstance(sch, dict) and sch.get("enum") else (RECIPE or default_for(sch))
        else:
            # nested object handling (limited depth)
            if isinstance(sch, dict) and (sch.get("type") == "object" or sch.get("properties")) and depth < 2:
                out[k] = build_object(sch, depth+1)
            else:
                out[k] = default_for(sch)

    # remove None values (but keep required empties)
    out = {k:v for k,v in out.items() if v is not None}
    return out

# Find best create endpoint: prefer exact /api/marketing/runs POST, and avoid /admin/
create_path = None
create_op = None
if "/api/marketing/runs" in paths and isinstance(paths["/api/marketing/runs"], dict) and isinstance(paths["/api/marketing/runs"].get("post"), dict):
    create_path = "/api/marketing/runs"
    create_op = paths[create_path]["post"]
else:
    best = (-999, None, None)
    for p, ops in paths.items():
        if "/admin/" in p.lower(): continue
        if not isinstance(ops, dict): continue
        op = ops.get("post")
        if not isinstance(op, dict): continue
        lp = p.lower()
        sc = 0
        if "marketing" in lp: sc += 5
        if "run" in lp: sc += 6
        if "publish" in lp: sc -= 10
        if "schedule" in lp: sc -= 10
        if "toggle" in lp: sc -= 10
        if lp.startswith("/api/"): sc += 1
        if op.get("requestBody"): sc += 1
        if sc > best[0]:
            best = (sc, p, op)
    create_path, create_op = best[1], best[2]

if not create_path or not create_op:
    raise SystemExit("Could not find a suitable create-run POST endpoint in OpenAPI")

req_schema = schema_for_request(create_op) or {"type":"object","properties":{}}
payload = build_object(req_schema)

# Collect helpful enum info for printing
mode_enum = None
recipe_enum = None
props = merge_allof(req_schema).get("properties") or {}
if "mode" in props:
    mode_enum = resolve_ref(props["mode"]).get("enum")
if "recipe" in props:
    r = resolve_ref(props["recipe"])
    recipe_enum = r.get("enum")
    if recipe_enum is None and isinstance(r, dict) and (r.get("type")=="object" or r.get("properties")):
        k = (r.get("properties") or {}).get("kind")
        if isinstance(k, dict):
            recipe_enum = k.get("enum")

# Find best status GET endpoint: prefer paths containing /runs/{...}/status
status_tpl = None
best = (-999, None)
for p, ops in paths.items():
    if not isinstance(ops, dict): continue
    if not isinstance(ops.get("get"), dict): continue
    lp = p.lower()
    if "/admin/" in lp: continue
    if "marketing" not in lp: continue
    sc = 0
    if "runs" in lp: sc += 4
    if "run" in lp: sc += 4
    if "status" in lp: sc += 6
    if "{" in p and "}" in p: sc += 2
    if sc > best[0]:
        best = (sc, p)
status_tpl = best[1]

# Find publish POST endpoint (optional)
publish_tpl = None
bestp = (-999, None)
for p, ops in paths.items():
    if not isinstance(ops, dict): continue
    if not isinstance(ops.get("post"), dict): continue
    lp = p.lower()
    if "/admin/" in lp: continue
    if "publish" not in lp: continue
    sc = 0
    if "runs" in lp: sc += 3
    if "run" in lp: sc += 3
    if "{" in p and "}" in p: sc += 2
    if lp.startswith("/api/"): sc += 1
    if sc > bestp[0]:
        bestp = (sc, p)
publish_tpl = bestp[1]

out = {
    "create_path": create_path,
    "payload": payload,
    "mode_enum": mode_enum,
    "recipe_enum": recipe_enum,
    "status_tpl": status_tpl,
    "publish_tpl": publish_tpl,
}
json.dump(out, open(out_path, "w", encoding="utf-8"), indent=2, sort_keys=True)
PY
}

substitute_path_param() {
  local tpl="$1"
  local rid="$2"
  py "$tpl" "$rid" <<'PY'
import sys,re
tpl=sys.argv[1]; rid=sys.argv[2]
print(re.sub(r"\{[^}]+\}", rid, tpl))
PY
}

extract_run_id() {
  local resp_json="$1"
  py "$resp_json" <<'PY'
import json,sys,re
d=json.load(open(sys.argv[1],"r",encoding="utf-8"))

for k in ("run_id","id","marketing_run_id","job_id","uuid"):
    v=d.get(k)
    if isinstance(v,str) and v:
        print(v); raise SystemExit(0)

def walk(x):
    if isinstance(x,dict):
        for v in x.values(): yield from walk(v)
    elif isinstance(x,list):
        for v in x: yield from walk(v)
    elif isinstance(x,str):
        yield x

uuid_re=re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
for s in walk(d):
    if uuid_re.match(s):
        print(s); raise SystemExit(0)

print("")
PY
}

extract_status() {
  local resp_json="$1"
  py "$resp_json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],"r",encoding="utf-8"))
for k in ("status","stage","state"):
    v=d.get(k)
    if isinstance(v,str) and v:
        print(v.lower()); raise SystemExit(0)
computed=d.get("computed") if isinstance(d,dict) else None
if isinstance(computed,dict):
    for k in ("status","stage","state"):
        v=computed.get(k)
        if isinstance(v,str) and v:
            print(v.lower()); raise SystemExit(0)
print("")
PY
}

# -----------------------
# 0) Health
# -----------------------
echo "Health checks..."
curl -sS "${CORE_URL%/}/api/health" >/dev/null
curl -sS "${MARKETING_URL%/}/api/health" >/dev/null
echo "  core health HTTP=200"
echo "  marketing health HTTP=200"

# -----------------------
# 1) Login
# -----------------------
LOGIN_OUT="${RUN_DIR}/01_login.json"
LOGIN_HDR="${RUN_DIR}/01_login.hdr"

echo "Logging in via svc-core..."
login_body="$(py <<PY
import json
print(json.dumps({"email":"${DF_EMAIL}","password":"${DF_PASSWORD}"}))
PY
)"
code="$(curl_json POST "${CORE_URL%/}/api/auth/login" "$login_body" "$LOGIN_OUT" $([[ "$DEBUG" == "1" ]] && echo "$LOGIN_HDR" || true))"
if ! is_2xx "$code"; then
  echo "ERROR: login failed HTTP=$code" >&2
  head -c 1200 "$LOGIN_OUT" >&2 || true
  exit 3
fi
extract_token_and_user "$LOGIN_OUT"
echo "Auth OK. user_id=${USER_ID:-<empty>}"

# -----------------------
# 2) OpenAPI + plan
# -----------------------
OPENAPI_FILE="${RUN_DIR}/00_openapi.json"
OPENAPI_HDR="${RUN_DIR}/00_openapi.hdr"
PLAN_FILE="${RUN_DIR}/00_plan.json"

fetch_openapi "$OPENAPI_FILE" "$OPENAPI_HDR" || { echo "ERROR: failed to fetch svc-marketing OpenAPI"; exit 4; }
echo "OpenAPI loaded: $OPENAPI_FILE (path=$(cat "${RUN_DIR}/00_openapi_path.txt"))"

build_create_plan "$OPENAPI_FILE" "$PLAN_FILE"

echo "Plan:"
cat "$PLAN_FILE"

CREATE_PATH="$(py "$PLAN_FILE" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],"r",encoding="utf-8"))
print(d["create_path"])
PY
)"
STATUS_TPL="$(py "$PLAN_FILE" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],"r",encoding="utf-8"))
print(d.get("status_tpl") or "")
PY
)"
PUBLISH_TPL="$(py "$PLAN_FILE" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],"r",encoding="utf-8"))
print(d.get("publish_tpl") or "")
PY
)"
CREATE_BODY="$(py "$PLAN_FILE" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],"r",encoding="utf-8"))
print(json.dumps(d["payload"]))
PY
)"

# -----------------------
# 3) Create run
# -----------------------
echo "Creating marketing run..."
CREATE_RESP="${RUN_DIR}/02_create_run.json"
CREATE_HDR="${RUN_DIR}/02_create_run.hdr"

url="${MARKETING_URL%/}${CREATE_PATH}"
code="$(curl_json POST "$url" "$CREATE_BODY" "$CREATE_RESP" $([[ "$DEBUG" == "1" ]] && echo "$CREATE_HDR" || true))"
echo "  -> POST $url HTTP=$code"
if ! is_2xx "$code"; then
  echo "ERROR: create failed. Body:" >&2
  cat "$CREATE_RESP" >&2 || true
  exit 5
fi

RUN_ID=""
if require_json_file "$CREATE_RESP" >/dev/null 2>&1; then
  RUN_ID="$(extract_run_id "$CREATE_RESP")"
fi

if [[ -z "$RUN_ID" ]]; then
  echo "WARN: create returned 2xx but run_id not found. Response saved: $CREATE_RESP"
  echo "DONE. Artifacts in: $RUN_DIR"
  exit 0
fi
echo "Create OK. run_id=$RUN_ID"

# -----------------------
# 4) Poll status
# -----------------------
if [[ -z "$STATUS_TPL" ]]; then
  echo "WARN: OpenAPI did not provide a status GET path. Exiting after create."
  echo "DONE. Artifacts in: $RUN_DIR"
  exit 0
fi

echo "Polling status..."
deadline=$(( $(date +%s) + TIMEOUT_SECS ))
STATUS_RESP="${RUN_DIR}/03_status.json"
FINAL_STATUS=""

while true; do
  now="$(date +%s)"
  if (( now > deadline )); then
    echo "ERROR: timeout after ${TIMEOUT_SECS}s"
    [[ -f "$STATUS_RESP" ]] && cat "$STATUS_RESP" || true
    exit 6
  fi

  ep="$(substitute_path_param "$STATUS_TPL" "$RUN_ID")"
  surl="${MARKETING_URL%/}${ep}"
  code="$(curl_get "$surl" "$STATUS_RESP" $([[ "$DEBUG" == "1" ]] && echo "${RUN_DIR}/03_status.hdr" || true))"
  if is_2xx "$code" && require_json_file "$STATUS_RESP" >/dev/null 2>&1; then
    FINAL_STATUS="$(extract_status "$STATUS_RESP")"
    echo "  status=${FINAL_STATUS:-<unknown>}"
    case "${FINAL_STATUS}" in
      succeeded|success|done|completed)
        echo "Run SUCCEEDED."
        break
        ;;
      failed|error|aborted|cancelled|canceled)
        echo "Run FAILED."
        cat "$STATUS_RESP"
        exit 7
        ;;
      *)
        sleep "$POLL_SECS"
        ;;
    esac
  else
    echo "  WARN: status HTTP=$code (retry)"
    sleep "$POLL_SECS"
  fi
done

# -----------------------
# 5) Optional publish
# -----------------------
if [[ "$PUBLISH" == "1" ]]; then
  if [[ -z "$PUBLISH_TPL" ]]; then
    echo "WARN: OpenAPI did not provide a publish endpoint."
  else
    echo "Publishing..."
    pub_ep="$(substitute_path_param "$PUBLISH_TPL" "$RUN_ID")"
    pub_url="${MARKETING_URL%/}${pub_ep}"
    pub_body='{"consent": true, "e2e": true}'
    PUB_RESP="${RUN_DIR}/04_publish.json"
    code="$(curl_json POST "$pub_url" "$pub_body" "$PUB_RESP" $([[ "$DEBUG" == "1" ]] && echo "${RUN_DIR}/04_publish.hdr" || true))"
    echo "  -> POST $pub_url HTTP=$code"
    if is_2xx "$code"; then
      echo "Publish OK."
    else
      echo "WARN: publish failed body:"
      cat "$PUB_RESP" || true
    fi
  fi
fi

echo
echo "DONE. Artifacts in: $RUN_DIR"
ls -la "$RUN_DIR" || true