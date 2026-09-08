#!/usr/bin/env bash
set -Eeuo pipefail

SUB="e70463a7-4016-4cf5-9291-b640b0f0f5cb"
EDGE_RG="rg-desifaces-prod-edge"
PROFILE="desifaces-prod-frontdoor"
ORIGIN_GROUP="default-origin-group"
PUBLIC_IP="52.252.188.211"
ORIGIN_HOST="origin-web.desifaces.ai"
PUBLIC_HOST="web.desifaces.ai"
AFD_ENDPOINT="desifaces-prod-b0enavbgachwc6e8.z01.azurefd.net"
VM_RG="desifaces_rg"
VM_NAME=""

need(){ command -v "$1" >/dev/null 2>&1 || { echo "FAIL: missing required command: $1" >&2; exit 2; }; }
for x in az curl; do need "$x"; done

echo "============================================================"
echo " desifaces.ai — FRONT DOOR ORIGIN-WEB TLS TRANSITION"
echo "============================================================"
echo "mode=GUARDED"
echo "public_dns_change=NONE"
echo "nsg_change=NONE"
echo "customer_data_change=NONE"

echo
echo "===== 1. VERIFY ORIGIN DNS ====="
ORIGIN_IP="$(getent ahostsv4 "$ORIGIN_HOST" 2>/dev/null | awk 'NR==1{print $1}' || true)"
if [[ -z "$ORIGIN_IP" ]]; then
  ORIGIN_IP="$(python3 - <<PY 2>/dev/null || true
import socket
try: print(socket.gethostbyname('$ORIGIN_HOST'))
except Exception: pass
PY
)"
fi
printf 'origin_dns=%s\n' "${ORIGIN_IP:-unresolved}"
[[ "$ORIGIN_IP" == "$PUBLIC_IP" ]] || { echo "FAIL: $ORIGIN_HOST must resolve directly to $PUBLIC_IP before continuing" >&2; exit 10; }

echo
echo "===== 2. RESOLVE PRODUCTION VM ====="
VM_NAME="$(az vm list -d --subscription "$SUB" --resource-group "$VM_RG" --query "[?publicIps=='$PUBLIC_IP'].name | [0]" -o tsv)"
[[ -n "$VM_NAME" ]] || { echo "FAIL: no VM in $VM_RG owns public IP $PUBLIC_IP" >&2; exit 11; }
echo "vm=$VM_NAME"

echo
echo "===== 3. PRECHECK CURRENT FRONT DOOR ORIGIN ====="
ORIGIN_NAME="$(az afd origin list --subscription "$SUB" --resource-group "$EDGE_RG" --profile-name "$PROFILE" --origin-group-name "$ORIGIN_GROUP" --query '[0].name' -o tsv)"
[[ -n "$ORIGIN_NAME" ]] || { echo "FAIL: Front Door origin not found" >&2; exit 12; }
az afd origin show --subscription "$SUB" --resource-group "$EDGE_RG" --profile-name "$PROFILE" --origin-group-name "$ORIGIN_GROUP" --origin-name "$ORIGIN_NAME" --query '{name:name,hostName:hostName,originHostHeader:originHostHeader,httpsPort:httpsPort,enforceCertificateNameCheck:enforceCertificateNameCheck,enabledState:enabledState}' -o jsonc

echo
echo "===== 4. CONFIGURE ORIGIN TLS ON VM ====="
read -r -d '' REMOTE_SCRIPT <<'REMOTE' || true
set -Eeuo pipefail
ORIGIN_HOST="origin-web.desifaces.ai"
PUBLIC_HOST="web.desifaces.ai"
PUBLIC_IP="52.252.188.211"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/home/azureuser/backups/frontdoor-origin-web-${TS}"
sudo mkdir -p "$BACKUP"
sudo cp -a /etc/nginx "$BACKUP/nginx-before"

command -v nginx >/dev/null || { echo 'FAIL: nginx missing'; exit 20; }
command -v certbot >/dev/null || { echo 'FAIL: certbot missing; refusing package mutation during launch hardening'; exit 21; }

LIVE_IP="$(getent ahostsv4 "$ORIGIN_HOST" | awk 'NR==1{print $1}')"
[[ "$LIVE_IP" == "$PUBLIC_IP" ]] || { echo "FAIL: VM sees $ORIGIN_HOST -> $LIVE_IP, expected $PUBLIC_IP"; exit 22; }

CURRENT_CODE="$(curl -k -sS -o /dev/null -w '%{http_code}' --resolve "$PUBLIC_HOST:443:127.0.0.1" "https://$PUBLIC_HOST/auth/login" || true)"
echo "existing_web_local_https=$CURRENT_CODE"
[[ "$CURRENT_CODE" == "200" ]] || { echo 'FAIL: existing production Nginx web path is not healthy'; exit 23; }

sudo mkdir -p /var/www/letsencrypt/.well-known/acme-challenge
ACME_CONF=/etc/nginx/conf.d/desifaces-origin-web-acme.conf
sudo tee "$ACME_CONF" >/dev/null <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name origin-web.desifaces.ai;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type text/plain;
        try_files $uri =404;
    }

    location / {
        return 301 https://web.desifaces.ai$request_uri;
    }
}
NGINX
sudo nginx -t
sudo systemctl reload nginx

echo 'acme-probe' | sudo tee /var/www/letsencrypt/.well-known/acme-challenge/df-origin-probe >/dev/null
PROBE_CODE="$(curl -sS -o /tmp/df-origin-probe.out -w '%{http_code}' "http://$ORIGIN_HOST/.well-known/acme-challenge/df-origin-probe" || true)"
echo "acme_http_probe=$PROBE_CODE"
[[ "$PROBE_CODE" == "200" ]] || { echo 'FAIL: origin HTTP-01 path is not publicly reachable'; exit 24; }

sudo certbot certonly --webroot \
  --webroot-path /var/www/letsencrypt \
  -d "$ORIGIN_HOST" \
  --non-interactive --agree-tos \
  --email support@desifaces.ai \
  --keep-until-expiring

CERT_DIR="/etc/letsencrypt/live/$ORIGIN_HOST"
[[ -s "$CERT_DIR/fullchain.pem" && -s "$CERT_DIR/privkey.pem" ]] || { echo 'FAIL: origin certificate files missing'; exit 25; }

ORIGIN_CONF=/etc/nginx/conf.d/desifaces-frontdoor-origin.conf
sudo tee "$ORIGIN_CONF" >/dev/null <<'NGINX'
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name origin-web.desifaces.ai;

    ssl_certificate     /etc/letsencrypt/live/origin-web.desifaces.ai/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/origin-web.desifaces.ai/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:13001;
        proxy_http_version 1.1;
        proxy_set_header Host web.desifaces.ai;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host web.desifaces.ai;
        proxy_set_header X-Forwarded-Port 443;
    }
}
NGINX
sudo nginx -t
sudo systemctl reload nginx

LOCAL_ORIGIN_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --resolve "$ORIGIN_HOST:443:127.0.0.1" "https://$ORIGIN_HOST/auth/login" || true)"
echo "origin_local_https=$LOCAL_ORIGIN_CODE"
[[ "$LOCAL_ORIGIN_CODE" == "200" ]] || { echo 'FAIL: new origin HTTPS endpoint does not serve web app'; exit 26; }

CERT_OK="$(echo | openssl s_client -connect 127.0.0.1:443 -servername "$ORIGIN_HOST" 2>/dev/null | openssl x509 -noout -ext subjectAltName 2>/dev/null | grep -F "$ORIGIN_HOST" || true)"
[[ -n "$CERT_OK" ]] || { echo 'FAIL: certificate SAN does not contain origin hostname'; exit 27; }

PUBLIC_WEB_CODE="$(curl -sS -o /dev/null -w '%{http_code}' "https://$PUBLIC_HOST/auth/login" || true)"
echo "public_web_before_afd_origin_switch=$PUBLIC_WEB_CODE"
[[ "$PUBLIC_WEB_CODE" == "200" ]] || { echo 'FAIL: public web regressed during origin TLS setup'; exit 28; }

echo "ORIGIN_TLS_VM_PREP=PASS"
echo "backup=$BACKUP"
REMOTE

RUN_RESULT="$(az vm run-command invoke --subscription "$SUB" --resource-group "$VM_RG" --name "$VM_NAME" --command-id RunShellScript --scripts "$REMOTE_SCRIPT" --query 'value[0].message' -o tsv)"
printf '%s\n' "$RUN_RESULT"
grep -q 'ORIGIN_TLS_VM_PREP=PASS' <<<"$RUN_RESULT" || { echo 'FAIL: VM origin TLS preparation did not certify'; exit 13; }

echo
echo "===== 5. SWITCH FRONT DOOR ORIGIN HOSTNAME ====="
OLD_HOST="$(az afd origin show --subscription "$SUB" --resource-group "$EDGE_RG" --profile-name "$PROFILE" --origin-group-name "$ORIGIN_GROUP" --origin-name "$ORIGIN_NAME" --query hostName -o tsv)"
OLD_HEADER="$(az afd origin show --subscription "$SUB" --resource-group "$EDGE_RG" --profile-name "$PROFILE" --origin-group-name "$ORIGIN_GROUP" --origin-name "$ORIGIN_NAME" --query originHostHeader -o tsv)"
echo "old_host=$OLD_HOST"
echo "old_header=$OLD_HEADER"

az afd origin update \
  --subscription "$SUB" \
  --resource-group "$EDGE_RG" \
  --profile-name "$PROFILE" \
  --origin-group-name "$ORIGIN_GROUP" \
  --origin-name "$ORIGIN_NAME" \
  --host-name "$ORIGIN_HOST" \
  --origin-host-header "$PUBLIC_HOST" \
  --https-port 443 \
  --http-port 80 \
  --enforce-certificate-name-check true \
  --enabled-state Enabled \
  --output none

echo
echo "===== 6. VERIFY FRONT DOOR ORIGIN CONFIG ====="
az afd origin show --subscription "$SUB" --resource-group "$EDGE_RG" --profile-name "$PROFILE" --origin-group-name "$ORIGIN_GROUP" --origin-name "$ORIGIN_NAME" --query '{hostName:hostName,originHostHeader:originHostHeader,httpsPort:httpsPort,enforceCertificateNameCheck:enforceCertificateNameCheck,enabledState:enabledState,provisioningState:provisioningState,deploymentStatus:deploymentStatus}' -o jsonc

echo
echo "===== 7. VERIFY GENERATED FRONT DOOR ENDPOINT ====="
AFD_OK=0
for i in $(seq 1 20); do
  CODE="$(curl -sS -o /tmp/afd-origin-test.html -w '%{http_code}' "https://$AFD_ENDPOINT/auth/login" || true)"
  echo "attempt=$i afd_http=$CODE"
  if [[ "$CODE" == "200" ]]; then AFD_OK=1; break; fi
  sleep 15
done

if [[ "$AFD_OK" != "1" ]]; then
  echo 'WARN: AFD verification failed; rolling origin hostname back to prior value.' >&2
  az afd origin update \
    --subscription "$SUB" \
    --resource-group "$EDGE_RG" \
    --profile-name "$PROFILE" \
    --origin-group-name "$ORIGIN_GROUP" \
    --origin-name "$ORIGIN_NAME" \
    --host-name "$OLD_HOST" \
    --origin-host-header "$OLD_HEADER" \
    --https-port 443 \
    --enforce-certificate-name-check true \
    --enabled-state Enabled \
    --output none
  echo "AFD_ORIGIN_ROLLBACK=PASS restored_host=$OLD_HOST"
  exit 14
fi

echo "============================================================"
echo " FRONT DOOR ORIGIN-WEB TLS TRANSITION PASS"
echo "============================================================"
echo "ORIGIN_DNS=PASS"
echo "ORIGIN_TLS_CERT=PASS"
echo "ORIGIN_NGINX=PASS"
echo "AFD_ORIGIN_HOST=$ORIGIN_HOST"
echo "AFD_ORIGIN_HOST_HEADER=$PUBLIC_HOST"
echo "AFD_CERT_NAME_CHECK=ENABLED"
echo "AFD_GENERATED_ENDPOINT=PASS"
echo "PUBLIC_DNS_CHANGE=NONE"
echo "NSG_CHANGE=NONE"
echo "NEXT_ACTION=CUT_WEB_DNS_TO_FRONT_DOOR_THEN_LOCK_ORIGIN"
