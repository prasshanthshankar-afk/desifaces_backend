#!/usr/bin/env bash
set -Eeuo pipefail

HOST="desifaces-gpu"
WEB="https://web.desifaces.ai"
API="https://api.desifaces.ai"
HTTP_WEB="http://web.desifaces.ai"

need(){ command -v "$1" >/dev/null 2>&1 || { echo "FAIL: missing required command: $1" >&2; exit 2; }; }
for x in ssh curl openssl; do need "$x"; done

echo "============================================================"
echo " desifaces.ai — PRODUCTION WEB SECURITY AUDIT"
echo "============================================================"
echo "MODE=READ_ONLY"
echo "PRODUCTION_MUTATION=NONE"

echo
echo "===== 1. PUBLIC HTTP -> HTTPS ====="
HTTP_HEADERS="$(curl -sS -I --max-time 15 "$HTTP_WEB" || true)"
printf '%s\n' "$HTTP_HEADERS"
HTTP_STATUS="$(printf '%s\n' "$HTTP_HEADERS" | awk 'toupper($0) ~ /^HTTP\// {code=$2} END{print code}')"
HTTP_LOC="$(printf '%s\n' "$HTTP_HEADERS" | awk 'BEGIN{IGNORECASE=1} /^Location:/ {gsub("\r",""); sub(/^[^:]+:[[:space:]]*/,""); print; exit}')"
if [[ "$HTTP_STATUS" =~ ^30[12378]$ ]] && [[ "$HTTP_LOC" == https://web.desifaces.ai* ]]; then
  echo "HTTP_TO_HTTPS_REDIRECT=PASS"
else
  echo "HTTP_TO_HTTPS_REDIRECT=FAIL"
fi

echo
echo "===== 2. HTTPS + SECURITY HEADERS ====="
HTTPS_HEADERS="$(curl -sS -I --max-time 15 "$WEB" || true)"
printf '%s\n' "$HTTPS_HEADERS"
for h in strict-transport-security content-security-policy x-content-type-options x-frame-options referrer-policy permissions-policy; do
  if printf '%s\n' "$HTTPS_HEADERS" | grep -qi "^${h}:"; then
    echo "HEADER_${h^^}=PASS"
  else
    echo "HEADER_${h^^}=MISSING"
  fi
done

echo
echo "===== 3. TLS CERTIFICATE / PROTOCOL ====="
CERT="$(echo | openssl s_client -connect web.desifaces.ai:443 -servername web.desifaces.ai 2>/dev/null | openssl x509 -noout -subject -issuer -dates 2>/dev/null || true)"
printf '%s\n' "$CERT"
for proto in tls1 tls1_1 tls1_2 tls1_3; do
  case "$proto" in
    tls1) flag=-tls1 ;;
    tls1_1) flag=-tls1_1 ;;
    tls1_2) flag=-tls1_2 ;;
    tls1_3) flag=-tls1_3 ;;
  esac
  if echo | openssl s_client "$flag" -connect web.desifaces.ai:443 -servername web.desifaces.ai >/tmp/df-tls.out 2>&1; then
    if grep -q 'Protocol.*TLS' /tmp/df-tls.out; then
      echo "TLS_${proto}=ACCEPTED"
    else
      echo "TLS_${proto}=REJECTED"
    fi
  else
    echo "TLS_${proto}=REJECTED"
  fi
done
rm -f /tmp/df-tls.out

echo
echo "===== 4. PUBLIC API / CORS QUICK CHECK ====="
API_HEADERS="$(curl -sS -I --max-time 15 "$API/director/api/health" || true)"
printf '%s\n' "$API_HEADERS"
if printf '%s\n' "$API_HEADERS" | grep -qi '^access-control-allow-origin:[[:space:]]*\*$'; then
  echo "CORS_WILDCARD=FAIL"
else
  echo "CORS_WILDCARD=NOT_DETECTED"
fi

echo
echo "===== 5. VM LISTENING PORTS / NGINX / DOCKER ====="
ssh -o BatchMode=yes "$HOST" 'bash -s' <<'REMOTE'
set -Eeuo pipefail

echo "--- LISTENERS ---"
sudo ss -lntp || ss -lntp || true

echo "--- DOCKER PUBLISHED PORTS ---"
docker ps --format 'table {{.Names}}\t{{.Ports}}' | sort

echo "--- WEB CONTAINER ---"
docker inspect df-v3-web-prod --format 'NAME={{.Name}} IMAGE={{.Config.Image}}{{println}}{{range $p,$v := .NetworkSettings.Ports}}PORT={{$p}} => {{$v}}{{println}}{{end}}' || true

echo "--- NGINX CONFIG RELEVANT LINES ---"
sudo nginx -T 2>/dev/null | grep -nEi 'server_name (web|api)\.desifaces\.ai|listen 80|listen 443|return 30[18]|proxy_pass|ssl_protocols|add_header (Strict-Transport-Security|Content-Security-Policy|X-Content-Type-Options|X-Frame-Options|Referrer-Policy|Permissions-Policy)' || true

echo "--- UFW ---"
sudo ufw status verbose 2>/dev/null || true
REMOTE

echo
echo "===== 6. EXTERNAL PORT EXPOSURE QUICK TEST ====="
for port in 80 443 13001 5432 6379; do
  if nc -z -w 3 web.desifaces.ai "$port" >/dev/null 2>&1; then
    echo "PUBLIC_PORT_${port}=OPEN"
  else
    echo "PUBLIC_PORT_${port}=CLOSED_OR_FILTERED"
  fi
done

echo
echo "============================================================"
echo "PRODUCTION_WEB_SECURITY_AUDIT=COMPLETE"
echo "MUTATION=NONE"
echo "============================================================"
