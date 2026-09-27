#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || { echo "FAIL: DEV host required" >&2; exit 1; }

echo "============================================================"
echo " DESIFACES — CANONICAL RUNTIME NAME NORMALIZATION"
echo " host=$(hostname -s)"
echo " production_touch=NONE"
echo " rename_only=YES"
echo "============================================================"

# Recover Director first if prior rollback left it only under a rollback name.
if ! docker inspect df-svc-director >/dev/null 2>&1 && ! docker inspect df-v3-svc-director >/dev/null 2>&1; then
  candidate="$(
    docker ps -a       --filter 'label=com.docker.compose.service=svc-director'       --format '{{.Names}}'     | grep -E '^(df-v3-svc-director|df-svc-director).*rollback'     | head -n 1 || true
  )"
  [[ -n "$candidate" ]] || {
    echo "FAIL: no Director container or rollback container found" >&2
    docker ps -a --filter 'label=com.docker.compose.service=svc-director'       --format '  {{.Names}}  {{.Status}}  {{.Image}}' >&2 || true
    exit 1
  }

  echo "DIRECTOR_RECOVERY_SOURCE=$candidate"
  docker rename "$candidate" df-svc-director
  docker update --restart=unless-stopped df-svc-director >/dev/null
  [[ "$(docker inspect -f '{{.State.Running}}' df-svc-director)" == "true" ]]     || docker start df-svc-director >/dev/null
  echo "DIRECTOR_CANONICAL_RECOVERY=PASS"
fi

mapfile -t OLD_NAMES < <(
  docker ps -a --format '{{.Names}}'   | grep -E '^(df-v3-|desifaces-v3-)'   | sort
)

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
declare -A FINAL_BY_OLD
declare -A BEFORE_STATE
declare -A BEFORE_STARTED

for old in "${OLD_NAMES[@]}"; do
  new="$old"
  new="${new/#df-v3-/df-}"
  new="${new/#desifaces-v3-/desifaces-}"

  old_state="$(docker inspect -f '{{.State.Status}}' "$old")"
  BEFORE_STATE["$old"]="$old_state"
  BEFORE_STARTED["$old"]="$(docker inspect -f '{{.State.StartedAt}}' "$old")"

  if docker inspect "$new" >/dev/null 2>&1; then
    new_state="$(docker inspect -f '{{.State.Status}}' "$new")"

    if [[ "$old_state" == "running" && "$new_state" == "running" ]]; then
      echo "FAIL: both versioned and canonical containers are running: $old / $new" >&2
      exit 1
    fi

    if [[ "$old_state" == "running" ]]; then
      legacy="${new}-legacy-${STAMP}"
      suffix=0
      while docker inspect "$legacy" >/dev/null 2>&1; do
        suffix=$((suffix+1))
        legacy="${new}-legacy-${STAMP}-${suffix}"
      done
      docker rename "$new" "$legacy"
      echo "PRESERVED_STALE_CANONICAL $new -> $legacy"
      docker rename "$old" "$new"
      FINAL_BY_OLD["$old"]="$new"
      echo "RENAMED $old -> $new"
    else
      legacy="${new}-legacy-${STAMP}"
      suffix=0
      while docker inspect "$legacy" >/dev/null 2>&1; do
        suffix=$((suffix+1))
        legacy="${new}-legacy-${STAMP}-${suffix}"
      done
      docker rename "$old" "$legacy"
      FINAL_BY_OLD["$old"]="$legacy"
      echo "PRESERVED_VERSIONED_CONTAINER $old -> $legacy"
    fi
  else
    docker rename "$old" "$new"
    FINAL_BY_OLD["$old"]="$new"
    echo "RENAMED $old -> $new"
  fi
done

echo "RUNTIME_NAME_NORMALIZATION=PASS count=${#OLD_NAMES[@]}"

for old in "${OLD_NAMES[@]}"; do
  final="${FINAL_BY_OLD[$old]}"
  after_state="$(docker inspect -f '{{.State.Status}}' "$final")"
  after_started="$(docker inspect -f '{{.State.StartedAt}}' "$final")"

  [[ "$after_state" == "${BEFORE_STATE[$old]}" ]] || {
    echo "FAIL: state changed during rename: $old -> $final" >&2
    exit 1
  }
  [[ "$after_started" == "${BEFORE_STARTED[$old]}" ]] || {
    echo "FAIL: container restarted during rename: $old -> $final" >&2
    exit 1
  }
done
echo "RUNTIME_RENAME_INVARIANCE=PASS"

# Patch every active Compose overlay so future compose operations do not recreate
# version-specific container/process names. Persistent image/volume identifiers are
# intentionally left alone because they are artifact/data provenance, not runtime names.
mapfile -t ROOTS < <(
  {
    docker ps -a --format '{{.Label "com.docker.compose.project.working_dir"}}'
    printf '%s\n' /home/azureuser/workspace/desifaces-v3
  } | sed '/^$/d' | sort -u
)

patched=0
for root in "${ROOTS[@]}"; do
  file="$root/docker-compose.v3.yml"
  [[ -f "$file" ]] || continue

  python3 - "$file" <<'PY'
from pathlib import Path
import re,sys

p=Path(sys.argv[1])
text=p.read_text()
text=re.sub(r"(?m)^name:\s*desifaces-v3\s*$","name: desifaces",text)
text=text.replace("container_name: desifaces-v3-db","container_name: desifaces-db")
text=text.replace("container_name: desifaces-v3-redis","container_name: desifaces-redis")
text=text.replace("container_name: df-v3-","container_name: df-")

if re.search(r"container_name:\s*(?:df-v3-|desifaces-v3-)",text):
    raise SystemExit("versioned container name remains")

p.write_text(text)
PY
  patched=$((patched+1))
done

echo "RUNTIME_COMPOSE_NAMES_PATCHED=PASS files=$patched"

remaining="$(
  docker ps -a --format '{{.Names}}'   | grep -E '^(df-v3-|desifaces-v3-)' || true
)"

[[ -z "$remaining" ]] || {
  echo "FAIL: version-specific container names remain:" >&2
  printf '%s\n' "$remaining" >&2
  exit 1
}

docker inspect df-svc-director >/dev/null 2>&1 || {
  echo "FAIL: df-svc-director missing after normalization" >&2
  exit 1
}

echo
echo "CANONICAL_RUNTIME_NAMES=PASS"
echo "DIRECTOR_NAME=df-svc-director"
echo "DATABASE_NAME=desifaces-db"
echo "REDIS_NAME=desifaces-redis"
echo "VERSIONED_CONTAINER_NAMES=0"
echo "PRODUCTION_TOUCH=NONE"
