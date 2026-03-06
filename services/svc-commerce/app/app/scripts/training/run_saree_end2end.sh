#!/usr/bin/env bash
# services/svc-commerce/app/app/scripts/training/run_saree_end2end.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../../../.." && pwd)"
ENV_FILE="${DF_ENV_FILE:-$REPO_ROOT/infra/.env}"

SVC_COMMERCE="${SVC_COMMERCE:-svc-commerce}"
DRAPE_STYLE="${DRAPE_STYLE:-nivi}"
NUM_EXAMPLES="${NUM_EXAMPLES:-1}"
QC_N="${QC_N:-$NUM_EXAMPLES}"
SEED="${SEED:-1337}"
CONCURRENCY="${CONCURRENCY:-4}"

RUN_DIR="/tmp/df_saree_e2e_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RUN_DIR"

echo "RUN_DIR=$RUN_DIR"
echo "REPO_ROOT=$REPO_ROOT"
echo "ENV_FILE=$ENV_FILE"
echo "SVC_COMMERCE=$SVC_COMMERCE"
echo "NUM_EXAMPLES=$NUM_EXAMPLES QC_N=$QC_N CONCURRENCY=$CONCURRENCY"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: env file not found: $ENV_FILE" >&2
  exit 2
fi

dc() { (cd "$REPO_ROOT" && docker compose --env-file "$ENV_FILE" "$@"); }

DATASET_NAME="saree_e2e_${DRAPE_STYLE}_$(date +%Y%m%d_%H%M%S)"
STORAGE_PREFIX="training/saree_e2e/${DATASET_NAME}"

GEN_LOG="$RUN_DIR/generate.log"

# Run generator in container (mint SAS inside container; no rehydrate)
dc exec -T \
  -e DATASET_NAME="$DATASET_NAME" \
  -e STORAGE_PREFIX="$STORAGE_PREFIX" \
  -e DRAPE_STYLE="$DRAPE_STYLE" \
  -e SEED="$SEED" \
  -e NUM_EXAMPLES="$NUM_EXAMPLES" \
  -e CONCURRENCY="$CONCURRENCY" \
  -e PERSON_URL="${PERSON_URL:-}" \
  -e SAREE_URL="${SAREE_URL:-}" \
  -e PERSON_AZ="${PERSON_AZ:-}" \
  -e SAREE_AZ="${SAREE_AZ:-}" \
  "$SVC_COMMERCE" bash -lc '
set -euo pipefail
cd /app

export EVAL_DIR="/tmp/${DATASET_NAME}"
mkdir -p "$EVAL_DIR"

python - <<PY
import os, json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from azure.storage.blob import generate_blob_sas, BlobSasPermissions

conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or ""
parts = dict(p.split("=",1) for p in conn.split(";") if "=" in p)
acct = parts.get("AccountName"); key = parts.get("AccountKey")
if not acct or not key:
    raise SystemExit("AZURE_STORAGE_CONNECTION_STRING missing or unparsable (need AccountName/AccountKey)")

def to_sas(u: str) -> str:
    u = (u or "").strip()
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if not u.startswith("az://"):
        raise SystemExit(f"Unsupported ref (use https or az://): {u}")
    rest = u[len("az://"):]
    c, b = rest.split("/", 1)
    expiry = datetime.now(timezone.utc) + timedelta(hours=12)
    tok = generate_blob_sas(acct, c, b, account_key=key, permission=BlobSasPermissions(read=True), expiry=expiry)
    return f"https://{acct}.blob.core.windows.net/{c}/{quote(b)}?{tok}"

eval_dir = os.environ["EVAL_DIR"]
person = (os.environ.get("PERSON_URL") or os.environ.get("PERSON_AZ") or "").strip()
saree  = (os.environ.get("SAREE_URL")  or os.environ.get("SAREE_AZ")  or "").strip()
if not person or not saree:
    raise SystemExit("Need PERSON_URL or PERSON_AZ, and SAREE_URL or SAREE_AZ")

json.dump([{"url": to_sas(person)}], open(f"{eval_dir}/persons.json","w"), indent=2)
json.dump([{"url": to_sas(saree )}], open(f"{eval_dir}/sarees.json","w"), indent=2)
json.dump([], open(f"{eval_dir}/blouses.json","w"))
json.dump([], open(f"{eval_dir}/pallus.json","w"))
print("WROTE_POOLS", eval_dir)
PY

python -m app.scripts.training.generate_saree_synth_dataset \
  --dataset_name "${DATASET_NAME}" \
  --drape_style "${DRAPE_STYLE}" \
  --persons_json "$EVAL_DIR/persons.json" \
  --sarees_json  "$EVAL_DIR/sarees.json" \
  --blouses_json "$EVAL_DIR/blouses.json" \
  --pallus_json  "$EVAL_DIR/pallus.json" \
  --num_examples "${NUM_EXAMPLES}" \
  --seed "${SEED}" \
  --concurrency "${CONCURRENCY}" \
  --storage_container commerce-training \
  --storage_prefix "${STORAGE_PREFIX}" \
  --no-rehydrate_inputs \
  --enable_refine
' 2>&1 | tee "$GEN_LOG"

# Extract dataset_id (NO host python; no quoting issues)
DATASET_ID="$(grep -oE '"dataset_id"[[:space:]]*:[[:space:]]*"[^"]+"' "$GEN_LOG" | head -n 1 | sed -E 's/.*"dataset_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
if [[ -z "$DATASET_ID" ]]; then
  echo "ERROR: Could not parse dataset_id from $GEN_LOG" >&2
  exit 3
fi
echo "DATASET_ID=$DATASET_ID"

# Run QC (no --cols; matches your container qc_saree_dataset.py)
dc exec -T "$SVC_COMMERCE" bash -lc "
cd /app
python -m app.scripts.training.qc_saree_dataset --dataset_id \"$DATASET_ID\" --n \"$QC_N\" --out_dir /tmp/saree_qc
" 2>&1 | tee "$RUN_DIR/qc.log" || true



# Try to copy QC artifacts (may or may not exist depending on qc implementation)
CID="$(dc ps -q "$SVC_COMMERCE")"
mkdir -p "$RUN_DIR/qc"
docker cp "$CID:/tmp/saree_qc/$DATASET_ID" "$RUN_DIR/qc/" >/dev/null 2>&1 || true

echo
echo "✅ E2E DONE"
echo "Dataset: $DATASET_ID"
echo "Run dir: $RUN_DIR"
echo "If QC copied: $RUN_DIR/qc/$DATASET_ID"
