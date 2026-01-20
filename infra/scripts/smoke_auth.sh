#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
EMAIL="${EMAIL:-smoke_$(date +%s)@desifaces.ai}"
PASS="${PASS:-TestPassw0rd!}"
NAME="${NAME:-Smoke Test}"

echo "BASE=$BASE"
echo "EMAIL=$EMAIL"

json_get () {
  local key="$1"
  python3 -c 'import json,sys; k=sys.argv[1]; s=sys.stdin.read().strip(); print(json.loads(s).get(k,""))' "$key"
}

# Return: "<status>|||<body>"
http_post () {
  local path="$1"
  local data="$2"
  local resp status body
  resp="$(curl -sS -X POST "$BASE$path" \
    -H "Content-Type: application/json" \
    -d "$data" \
    -w "\n%{http_code}")"
  status="$(echo "$resp" | tail -n 1 | tr -d '\r')"
  body="$(echo "$resp" | sed '$d')"
  printf "%s|||%s" "$status" "$body"
}

split_resp () {
  local resp="$1"
  status="${resp%%|||*}"
  body="${resp#*|||}"
}

echo
echo "1) Register"
resp="$(http_post /api/auth/register "{\"email\":\"$EMAIL\",\"password\":\"$PASS\",\"full_name\":\"$NAME\"}")"
split_resp "$resp"
echo "$body"
[[ "$status" == "200" || "$status" == "201" ]] || { echo "❌ register failed (status $status)"; exit 1; }
echo "✅ register ok"

echo
echo "2) Login"
resp="$(http_post /api/auth/login "{\"email\":\"$EMAIL\",\"password\":\"$PASS\",\"client_type\":\"web\"}")"
split_resp "$resp"
echo "$body"
[[ "$status" == "200" ]] || { echo "❌ login failed (status $status)"; exit 1; }

access="$(printf "%s" "$body" | json_get access_token)"
refresh="$(printf "%s" "$body" | json_get refresh_token)"
if [[ -z "$access" || -z "$refresh" ]]; then
  echo "❌ missing tokens from login"
  echo "DEBUG: access_len=${#access} refresh_len=${#refresh}"
  exit 1
fi
echo "✅ login ok"

echo
echo "3) Refresh (should succeed, rotated refresh token)"
resp="$(http_post /api/auth/refresh "{\"refresh_token\":\"$refresh\"}")"
split_resp "$resp"
echo "$body"
[[ "$status" == "200" ]] || { echo "❌ refresh failed (status $status)"; exit 1; }

new_refresh="$(printf "%s" "$body" | json_get refresh_token)"
[[ -n "$new_refresh" ]] || { echo "❌ missing new refresh token"; exit 1; }
echo "✅ refresh ok (token rotated)"

echo
echo "4) Logout (revoke refresh token)"
resp="$(http_post /api/auth/logout "{\"refresh_token\":\"$new_refresh\"}")"
split_resp "$resp"
echo "$body"
[[ "$status" == "200" ]] || { echo "❌ logout failed (status $status)"; exit 1; }
echo "✅ logout ok"

echo
echo "5) Refresh after logout (MUST fail with 401)"
resp="$(http_post /api/auth/refresh "{\"refresh_token\":\"$new_refresh\"}")"
split_resp "$resp"
echo "$body"
if [[ "$status" == "401" ]]; then
  echo "✅ refresh correctly rejected after logout (401)"
else
  echo "❌ expected 401 after logout, got $status"
  exit 1
fi

echo
echo "🎉 SMOKE TEST PASSED"
