#!/usr/bin/env bash
set -Eeuo pipefail

SUBSCRIPTION="e70463a7-4016-4cf5-9291-b640b0f0f5cb"
RG="DESIFACES_RG"
VM="desifaces-gpu-non-prod"
EXPECTED_VM_ID="35396818-8b9f-4a80-bb41-86213457484b"
EXPECTED_PUBLIC_IP="52.252.188.211"
PROD_DISK_NAME="desifaces-non-prod-rg_OsDisk_1_ca10d5598c9842d9a9cd420e4629ca70"
WRONG_OS_DISK_NAME="desifaces-gpu_OsDisk_1_b7f54a09f09e439eab77000e5a11f837"
EXPECTED_PROD_HOSTNAME="desifaces-gpu"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
PROD_SNAPSHOT="desifaces-prod-os-pre-recovery-${TS}"
WRONG_SNAPSHOT="desifaces-wrong-os-pre-recovery-${TS}"

fail() { echo "FAIL: $*" >&2; exit 1; }

cleanup() {
  unset CONFIRM || true
}
trap cleanup EXIT

command -v az >/dev/null 2>&1 || fail "Azure CLI is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"

# Azure CLI output schemas vary across versions (diskSizeGb vs diskSizeGB).
# Resolve the disk size from JSON instead of relying on one JMESPath casing.
disk_size_gb() {
  az disk show --ids "$1" -o json | python3 -c '
import json, sys
d = json.load(sys.stdin)
v = d.get("diskSizeGb")
if v is None:
    v = d.get("diskSizeGB")
if v is None:
    v = d.get("disk_size_gb")
if isinstance(v, float) and v.is_integer():
    v = int(v)
print("" if v is None else v)
'
}

az account set --subscription "$SUBSCRIPTION" >/dev/null

echo "============================================================"
echo " desifaces — PRODUCTION OS DISK RECOVERY V2"
echo " source=existing_persistent_production_os_disk"
echo " db_restore=NOT_PLANNED"
echo " dev_data_import=FORBIDDEN"
echo "============================================================"

echo
echo "===== 1. FAIL-CLOSED AZURE IDENTITY GATE ====="
VM_ID="$(az vm show -g "$RG" -n "$VM" --query vmId -o tsv)"
[[ "$VM_ID" == "$EXPECTED_VM_ID" ]] || fail "unexpected VM id: $VM_ID"
PUBLIC_IP="$(az vm list-ip-addresses -g "$RG" -n "$VM" --query '[0].virtualMachine.network.publicIpAddresses[0].ipAddress' -o tsv)"
[[ "$PUBLIC_IP" == "$EXPECTED_PUBLIC_IP" ]] || fail "unexpected public IP: $PUBLIC_IP"

PROD_DISK_ID="$(az disk show -g "$RG" -n "$PROD_DISK_NAME" --query id -o tsv)"
WRONG_OS_DISK_ID="$(az disk show -g "$RG" -n "$WRONG_OS_DISK_NAME" --query id -o tsv)"
CURRENT_OS_ID="$(az vm show -g "$RG" -n "$VM" --query storageProfile.osDisk.managedDisk.id -o tsv)"
[[ "${CURRENT_OS_ID,,}" == "${WRONG_OS_DISK_ID,,}" ]] || fail "current OS disk is not the expected recovery/non-prod disk"

DATA_DISKS="$(az vm show -g "$RG" -n "$VM" --query 'storageProfile.dataDisks[].managedDisk.id' -o tsv)"
grep -Fqi "$PROD_DISK_ID" <<<"$DATA_DISKS" || fail "production OS disk is not currently attached as a data disk"

PROD_OS_TYPE="$(az disk show --ids "$PROD_DISK_ID" --query osType -o tsv)"
[[ "$PROD_OS_TYPE" == "Linux" ]] || fail "production disk osType is not Linux: $PROD_OS_TYPE"

PROD_SIZE="$(disk_size_gb "$PROD_DISK_ID")"
CURRENT_SIZE="$(disk_size_gb "$WRONG_OS_DISK_ID")"
[[ "$PROD_SIZE" =~ ^[0-9]+$ ]] || fail "invalid production disk size: ${PROD_SIZE:-EMPTY}"
[[ "$CURRENT_SIZE" =~ ^[0-9]+$ ]] || fail "invalid current OS disk size: ${CURRENT_SIZE:-EMPTY}"

PROD_HV="$(az disk show --ids "$PROD_DISK_ID" --query hyperVGeneration -o tsv)"
CURRENT_HV="$(az disk show --ids "$WRONG_OS_DISK_ID" --query hyperVGeneration -o tsv)"
if [[ -n "$PROD_HV" && -n "$CURRENT_HV" && "$PROD_HV" != "$CURRENT_HV" ]]; then
  fail "Hyper-V generation mismatch: prod=$PROD_HV current=$CURRENT_HV"
fi

PROD_DES="$(az disk show --ids "$PROD_DISK_ID" --query 'encryption.diskEncryptionSetId' -o tsv)"
CURRENT_DES="$(az disk show --ids "$WRONG_OS_DISK_ID" --query 'encryption.diskEncryptionSetId' -o tsv)"
if [[ -n "$PROD_DES" || -n "$CURRENT_DES" ]]; then
  [[ "$PROD_DES" == "$CURRENT_DES" ]] || fail "disk encryption set mismatch"
fi

echo "vm_id=$VM_ID"
echo "public_ip=$PUBLIC_IP"
echo "current_os_disk=$WRONG_OS_DISK_NAME size_gb=$CURRENT_SIZE"
echo "production_os_disk=$PROD_DISK_NAME size_gb=$PROD_SIZE"
echo "production_os_type=$PROD_OS_TYPE"
echo "PRE_MUTATION_IDENTITY_GATE=PASS"

echo
echo "===== 2. EXPLICIT RECOVERY CONFIRMATION ====="
echo "This will deallocate the VM, snapshot both disks, detach the production disk from the data-disk slot,"
echo "make the two OS disks the same Azure-managed size if needed, swap the VM back to the persistent production OS disk,"
echo "start the VM, and certify the recovered stack."
echo "No PostgreSQL dump restore is performed."
read -r -p "Type RECOVER-DESIFACES-PROD-OS to continue: " CONFIRM
[[ "$CONFIRM" == "RECOVER-DESIFACES-PROD-OS" ]] || { echo "RECOVERY=ABORTED"; exit 0; }

echo
echo "===== 3. DEALLOCATE VM ====="
az vm deallocate -g "$RG" -n "$VM" --only-show-errors >/dev/null
echo "VM_DEALLOCATE=PASS"

echo
echo "===== 4. SNAPSHOT BOTH DISKS BEFORE MUTATION ====="
az snapshot create -g "$RG" -n "$PROD_SNAPSHOT" --source "$PROD_DISK_ID" --sku Standard_LRS --only-show-errors >/dev/null
az snapshot create -g "$RG" -n "$WRONG_SNAPSHOT" --source "$WRONG_OS_DISK_ID" --sku Standard_LRS --only-show-errors >/dev/null
[[ "$(az snapshot show -g "$RG" -n "$PROD_SNAPSHOT" --query provisioningState -o tsv)" == "Succeeded" ]] || fail "production disk snapshot failed"
[[ "$(az snapshot show -g "$RG" -n "$WRONG_SNAPSHOT" --query provisioningState -o tsv)" == "Succeeded" ]] || fail "current OS disk snapshot failed"
echo "production_snapshot=$PROD_SNAPSHOT"
echo "rollback_snapshot=$WRONG_SNAPSHOT"
echo "SNAPSHOT_GATE=PASS"

echo
echo "===== 5. DETACH PRODUCTION DISK FROM DATA-DISK SLOT ====="
az vm disk detach -g "$RG" --vm-name "$VM" --name "$PROD_DISK_NAME" --only-show-errors >/dev/null
MANAGED_BY="$(az disk show --ids "$PROD_DISK_ID" --query managedBy -o tsv)"
[[ -z "$MANAGED_BY" ]] || fail "production disk still attached after detach: $MANAGED_BY"
echo "PRODUCTION_DISK_DETACH=PASS"

echo
echo "===== 6. SIZE-COMPATIBILITY GATE ====="
if (( CURRENT_SIZE < PROD_SIZE )); then
  echo "resizing_current_wrong_os_disk_from=${CURRENT_SIZE}GB_to=${PROD_SIZE}GB"
  az disk update --ids "$WRONG_OS_DISK_ID" --size-gb "$PROD_SIZE" --only-show-errors >/dev/null
  CURRENT_SIZE="$(disk_size_gb "$WRONG_OS_DISK_ID")"
elif (( CURRENT_SIZE > PROD_SIZE )); then
  fail "current OS disk is larger than production disk; automatic shrink is forbidden"
fi
[[ "$CURRENT_SIZE" == "$PROD_SIZE" ]] || fail "OS disk sizes do not match after compatibility step: current=$CURRENT_SIZE prod=$PROD_SIZE"
echo "OS_DISK_SIZE_COMPATIBILITY=PASS size_gb=$PROD_SIZE"

echo
echo "===== 7. SWAP VM BACK TO PERSISTENT PRODUCTION OS DISK ====="
az vm update -g "$RG" -n "$VM" --os-disk "$PROD_DISK_ID" --only-show-errors >/dev/null
NEW_OS_ID="$(az vm show -g "$RG" -n "$VM" --query storageProfile.osDisk.managedDisk.id -o tsv)"
[[ "${NEW_OS_ID,,}" == "${PROD_DISK_ID,,}" ]] || fail "VM model did not switch to production OS disk"
echo "OS_DISK_SWAP=PASS"

echo
echo "===== 8. START VM ====="
az vm start -g "$RG" -n "$VM" --only-show-errors >/dev/null
POWER=""
for i in $(seq 1 30); do
  POWER="$(az vm get-instance-view -g "$RG" -n "$VM" --query "instanceView.statuses[?starts_with(code,'PowerState/')].code | [0]" -o tsv 2>/dev/null || true)"
  [[ "$POWER" == "PowerState/running" ]] && break
  sleep 5
done
[[ "$POWER" == "PowerState/running" ]] || fail "VM did not reach running state"
echo "VM_START=PASS"

echo
echo "===== 9. WAIT FOR AZURE AGENT ====="
sleep 35

POST_SCRIPT=$(cat <<'POST'
set -Eeuo pipefail

echo "============================================================"
echo " desifaces — RECOVERED PRODUCTION RUNTIME CERTIFICATION"
echo "============================================================"

HOST="$(hostname -s)"
echo "hostname=$HOST"
[[ "$HOST" == "desifaces-gpu" ]] || { echo "FAIL: unexpected recovered hostname" >&2; exit 20; }

sudo systemctl start docker
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}')"
echo "docker_root=$DOCKER_ROOT"
[[ "$DOCKER_ROOT" == "/var/lib/docker" ]] || { echo "FAIL: recovered Docker root is not persistent /var/lib/docker" >&2; exit 21; }

[[ -f /home/azureuser/workspace/desifaces/infra/.env ]] || { echo "FAIL: production infra/.env missing" >&2; exit 22; }
[[ -d /var/lib/docker/volumes/desifaces_df_pgdata/_data ]] || { echo "FAIL: production PostgreSQL volume missing" >&2; exit 23; }

echo "prod_env=FOUND"
echo "postgres_volume=FOUND"

sudo systemctl start nginx

for T in desifaces-notification-dispatch.timer desifaces-safe-docker-cleanup.timer; do
  if systemctl list-unit-files "$T" --no-legend 2>/dev/null | grep -q "$T"; then
    sudo systemctl enable --now "$T" >/dev/null
  fi
done

REQUIRED=(
  desifaces-db
  desifaces-redis
  df-svc-audio
  df-svc-audio-worker
  df-v3-svc-director
  df-v3-svc-director-worker
  df-v3-svc-assistant
  df-v3-web-prod
)

for C in "${REQUIRED[@]}"; do
  docker inspect "$C" >/dev/null 2>&1 || { echo "FAIL: required container metadata missing: $C" >&2; exit 24; }
  STATE="$(docker inspect -f '{{.State.Status}}' "$C")"
  if [[ "$STATE" != "running" ]]; then
    echo "starting_container=$C previous_state=$STATE"
    docker start "$C" >/dev/null
  fi
done

for i in $(seq 1 36); do
  DB_STATE="$(docker inspect -f '{{.State.Status}}' desifaces-db 2>/dev/null || true)"
  WEB_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:13000/auth/login 2>/dev/null || true)"
  DIR_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:18011/api/health 2>/dev/null || true)"
  AST_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:18012/api/health 2>/dev/null || true)"
  if [[ "$DB_STATE" == "running" && "$WEB_CODE" == "200" && "$DIR_CODE" == "200" && "$AST_CODE" == "200" ]]; then
    break
  fi
  sleep 5
done

DB_HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' desifaces-db)"
echo "db_state=$DB_STATE health=$DB_HEALTH"
echo "local_web=$WEB_CODE"
echo "local_director=$DIR_CODE"
echo "local_assistant=$AST_CODE"
[[ "$DB_STATE" == "running" ]] || exit 25
[[ "$DB_HEALTH" == "healthy" || "$DB_HEALTH" == "no-healthcheck" ]] || exit 26
[[ "$WEB_CODE" == "200" && "$DIR_CODE" == "200" && "$AST_CODE" == "200" ]] || exit 27

for T in desifaces-notification-dispatch.timer desifaces-safe-docker-cleanup.timer; do
  if systemctl list-unit-files "$T" --no-legend 2>/dev/null | grep -q "$T"; then
    echo "$T enabled=$(systemctl is-enabled "$T" 2>/dev/null || true) active=$(systemctl is-active "$T" 2>/dev/null || true)"
  fi
done

echo "--- recovered containers ---"
docker ps --format '{{.Names}}|{{.Status}}' | grep -E '^(desifaces-|df-)' | sort

echo "RECOVERED_PRODUCTION_RUNTIME=PASS"
POST
)

RUN_OK=0
for attempt in $(seq 1 6); do
  if az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript --scripts "$POST_SCRIPT" --query 'value[0].message' -o tsv; then
    RUN_OK=1
    break
  fi
  echo "run_command_attempt=$attempt status=retry"
  sleep 20
done
[[ "$RUN_OK" == "1" ]] || fail "recovered guest certification could not be executed"

echo
echo "===== 10. PUBLIC ENDPOINT CERTIFICATION ====="
WEB=""; DIR=""; AST=""
for i in $(seq 1 24); do
  WEB="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 8 https://web.desifaces.ai/auth/login 2>/dev/null || true)"
  DIR="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 8 https://api.desifaces.ai/director/api/health 2>/dev/null || true)"
  AST="$(curl -ksS -o /dev/null -w '%{http_code}' --max-time 8 https://api.desifaces.ai/assistant/api/health 2>/dev/null || true)"
  if [[ "$WEB" == "200" && "$DIR" == "200" && "$AST" == "200" ]]; then
    break
  fi
  sleep 5
done

echo "public_web=$WEB"
echo "public_director=$DIR"
echo "public_assistant=$AST"
[[ "$WEB" == "200" && "$DIR" == "200" && "$AST" == "200" ]] || fail "public endpoint certification failed"

echo
echo "============================================================"
echo " DESIFACES PRODUCTION OS RECOVERY=PASS"
echo "============================================================"
echo "production_os_disk=$PROD_DISK_NAME"
echo "production_snapshot=$PROD_SNAPSHOT"
echo "rollback_snapshot=$WRONG_SNAPSHOT"
echo "database_restore=NONE"
echo "dev_data_import=NONE"
echo "persistent_docker_root=/var/lib/docker"
echo "public_ip=$EXPECTED_PUBLIC_IP"
