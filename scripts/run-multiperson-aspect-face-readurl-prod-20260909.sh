#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${ROOT:-/home/azureuser/workspace/desifaces}"
RELEASE_REF="release/v3-production-backend-closeout-20260904"
SOURCE_COMMIT="6aaef39c524ff4d5472c432936046a73fe4bbb1f"
WEB_COMMIT="0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"
PROMOTION_PATH="scripts/promote-certify-multiperson-aspect-face-readurl-prod-20260909.sh"
REPO="prasshanthshankar-afk/desifaces_backend"

fail(){ echo "FAIL: $*" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
[[ -d "$ROOT" ]] || fail "production package missing: $ROOT"

# Important: /home/azureuser/workspace/desifaces is a canonical production
# package, not a Git checkout. Never require .git or mutate it with git commands.
echo "SOURCE_TRANSPORT=IMMUTABLE_GITHUB_BLOBS"
echo "PRODUCTION_PACKAGE=$ROOT"

# The branch raw endpoint is used only to obtain the small deployment script.
# All application source injected by that script is replaced below with immutable
# Git blob reads, so no per-path raw/ref lookup is used for hotfix source files.
payload="$(curl -fsSL \
  "https://raw.githubusercontent.com/${REPO}/${RELEASE_REF}/${PROMOTION_PATH}")"

patched="$(printf '%s\n' "$payload" | python3 -c '
import sys
s = sys.stdin.read()
source = "6aaef39c524ff4d5472c432936046a73fe4bbb1f"
web = "0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc"
s = s.replace(
    "BACKEND_COMMIT=\"f459ae128d0bf5e2b0d6b63dfaf476df11c4731b\"",
    f"BACKEND_COMMIT=\"{source}\"",
)
s = s.replace(
    "WEB_COMMIT=\"0e832f2d14e8f7cfe1d74975ac1277c8f476bfdc\"",
    f"WEB_COMMIT=\"{web}\"",
)
start_marker = "echo\necho \"===== 1. SYNC IMMUTABLE SOURCE =====\""
end_marker = "echo\necho \"===== 2. STATIC CONTRACT GATES =====\""
start = s.find(start_marker)
end = s.find(end_marker, start + 1)
if start < 0 or end < 0:
    raise SystemExit("FAIL: could not locate source-sync block in promotion script")
replacement = r'''echo
echo "===== 1. SYNC IMMUTABLE SOURCE ====="

fetch_blob_to_file(){
  local repo="$1" blob_sha="$2" dest="$3" tmp
  tmp="${dest}.hotfix.$$"
  mkdir -p "$(dirname "$dest")"
  curl -fsSL \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${repo}/git/blobs/${blob_sha}" \
  | python3 -c '\''
import base64, json, sys
doc = json.load(sys.stdin)
if doc.get("encoding") != "base64" or not doc.get("content"):
    raise SystemExit("invalid GitHub blob response")
sys.stdout.buffer.write(base64.b64decode(doc["content"]))
'\'' > "$tmp"
  [[ -s "$tmp" ]] || { rm -f "$tmp"; fail "empty immutable blob: $repo@$blob_sha"; }
  mv "$tmp" "$dest"
}

# Backend source: immutable blobs from backend commit 6aaef39c...
fetch_blob_to_file "$BACKEND_REPO" "59462638a9464b3800ae4330c9983a58a87a4992" "$ROOT/services/svc-director/app/app/face_execution_runtime.py"
fetch_blob_to_file "$BACKEND_REPO" "531327d60d1b7f4cddeabd614e3d5887b7297acb" "$ROOT/services/svc-director/app/app/fusion_input_performance.py"
fetch_blob_to_file "$BACKEND_REPO" "4841800302c78b43e20964ad68a974df444b5bae" "$ROOT/services/svc-director/app/app/studio_aspect_routes.py"
fetch_blob_to_file "$BACKEND_REPO" "44da7f2299d241becd42a4a280efef217cdffb5e" "$ROOT/services/svc-director/app/app/studio_routes_runtime.py"
fetch_blob_to_file "$BACKEND_REPO" "59115de9687becd2cf9ab835130b439166d4e1f0" "$ROOT/services/svc-face/app/app/api/routes/face_media.py"
fetch_blob_to_file "$BACKEND_REPO" "870d249af1e54b95d633ad036d703bd43165dc4d" "$ROOT/services/svc-face/app/app/api/__init__.py"

# Web source: immutable blobs from web commit 0e832f2...
fetch_blob_to_file "$WEB_REPO" "8d4dddfe5bea0f4456a89f29e253c3ff1065fa03" "$WEB_SRC/lib/multiperson-aspect.ts"
fetch_blob_to_file "$WEB_REPO" "cd2f27766fb85e2325ba83104c61968924b1d76b" "$WEB_SRC/lib/client.ts"
fetch_blob_to_file "$WEB_REPO" "3838ce47bed39538681a070f011e57a3f5a01b92" "$WEB_SRC/components/MultiPersonAspectControls.tsx"
fetch_blob_to_file "$WEB_REPO" "f0e5437685cda96bca341ce021a9d0cecb632d1e" "$WEB_SRC/app/app/multi-person/multi-person-aspect.css"
fetch_blob_to_file "$WEB_REPO" "2699b085b75a68ed850169c560722d6ad5809fc9" "$WEB_SRC/app/app/multi-person/layout.tsx"

echo "SOURCE_SYNC=PASS"
'''
s = s[:start] + replacement + s[end:]
if "raw.githubusercontent.com/$BACKEND_REPO/$BACKEND_COMMIT/$f" in s:
    raise SystemExit("FAIL: backend raw source loop remained after patch")
if "raw.githubusercontent.com/$WEB_REPO/$WEB_COMMIT/web/$f" in s:
    raise SystemExit("FAIL: web raw source loop remained after patch")
print(s, end="")
')"

printf '%s\n' "$patched" | bash
