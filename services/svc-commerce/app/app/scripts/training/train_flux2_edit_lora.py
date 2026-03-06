# services/svc-commerce/app/app/scripts/training/train_flux2_edit_lora.py
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.request import Request, urlopen

from azure.storage.blob import BlobServiceClient
from azure.storage.blob import generate_blob_sas, BlobSasPermissions


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _http_json(method: str, url: str, *, headers: Dict[str, str], body: Optional[Dict[str, Any]] = None, timeout_s: int = 120) -> Dict[str, Any]:
    m = (method or "GET").upper()
    data = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if body is not None and m not in ("GET", "HEAD"):
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = Request(url=url, method=m, headers=hdrs, data=data)
    with urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read() or b""
        txt = raw.decode("utf-8", errors="replace")
        j = json.loads(txt) if txt.strip() else {}
        return j if isinstance(j, dict) else {"raw": j}


def _download_bytes(url: str, timeout_s: int = 180) -> bytes:
    req = Request(url, headers={"User-Agent": "desifaces-trainer/1.0"})
    with urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _azure_conn_parts(conn: str) -> Tuple[str, str]:
    parts = {}
    for kv in conn.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.strip()] = v.strip()
    acct = parts.get("AccountName") or ""
    key = parts.get("AccountKey") or ""
    if not acct or not key:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING must include AccountName and AccountKey")
    return acct, key


def _sas_url(account_name: str, account_key: str, container: str, blob: str, *, hours: int = 72) -> str:
    expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
    sas = generate_blob_sas(
        account_name=account_name,
        account_key=account_key,
        container_name=container,
        blob_name=blob,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
    )
    return f"https://{account_name}.blob.core.windows.net/{container}/{blob}?{sas}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip-url", required=True, help="SAS URL to the training zip")
    ap.add_argument("--steps", type=int, default=1200)
    ap.add_argument("--learning-rate", type=float, default=0.00005)
    ap.add_argument("--default-caption", default="Drape a traditional Indian saree in nivi style with realistic pleats and pallu. Preserve identity, pose, lighting, and background. Match fabric pattern and colors.")
    ap.add_argument("--trainer-endpoint", default="fal-ai/flux-2-trainer-v2/edit")
    ap.add_argument("--poll-secs", type=float, default=5.0)
    ap.add_argument("--poll-timeout-s", type=int, default=60 * 60)

    ap.add_argument("--mirror-to-azure", action="store_true")
    ap.add_argument("--azure-container", default="commerce-training")
    ap.add_argument("--azure-prefix", default="checkpoints/saree_flux2_edit")
    ap.add_argument("--sas-hours", type=int, default=168)

    ap.add_argument("--dataset-id", default="")
    args = ap.parse_args()

    fal_key = _env_str("FAL_KEY") or _env_str("FAL_API_KEY")
    if not fal_key:
        raise RuntimeError("FAL_KEY (or FAL_API_KEY) is required")

    base = "https://queue.fal.run"
    endpoint = args.trainer_endpoint.strip().strip("/")
    post_url = f"{base}/{endpoint}"

    submit_body = {
        "image_data_url": args.zip_url,
        "steps": int(args.steps),
        "learning_rate": float(args.learning_rate),
        "default_caption": str(args.default_caption),
    }

    submit = _http_json("POST", post_url, headers={"Authorization": f"Key {fal_key}"}, body=submit_body, timeout_s=180)
    request_id = str(submit.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError(f"Fal submit missing request_id. submit={submit}")

    status_url = f"{base}/{endpoint}/requests/{request_id}/status?logs=1"
    result_url = f"{base}/{endpoint}/requests/{request_id}"

    print("request_id:", request_id)
    print("status_url:", status_url)

    t0 = time.time()
    last = {}
    while True:
        if time.time() - t0 > float(args.poll_timeout_s):
            raise RuntimeError(f"Timeout waiting for COMPLETED. last={last}")
        st = _http_json("GET", status_url, headers={"Authorization": f"Key {fal_key}"}, body=None, timeout_s=120)
        last = st
        s = str(st.get("status") or "").upper()
        if s == "COMPLETED":
            break
        if s in ("FAILED", "ERROR", "CANCELED", "CANCELLED"):
            raise RuntimeError(f"Training failed. status={s} last={st}")
        time.sleep(float(args.poll_secs))

    out = _http_json("GET", result_url, headers={"Authorization": f"Key {fal_key}"}, body=None, timeout_s=180)

    # Extract outputs
    diffusers_url = ""
    config_url = ""

    def scan(obj: Any) -> None:
        nonlocal diffusers_url, config_url
        if isinstance(obj, dict):
            if not diffusers_url:
                u = (((obj.get("diffusers_lora_file") or {}) if isinstance(obj.get("diffusers_lora_file"), dict) else {}).get("url"))
                if isinstance(u, str) and u.startswith("http"):
                    diffusers_url = u
            if not config_url:
                u = (((obj.get("config_file") or {}) if isinstance(obj.get("config_file"), dict) else {}).get("url"))
                if isinstance(u, str) and u.startswith("http"):
                    config_url = u
            for v in obj.values():
                scan(v)
        elif isinstance(obj, list):
            for v in obj:
                scan(v)

    scan(out)

    if not diffusers_url:
        raise RuntimeError(f"Could not find diffusers_lora_file.url in result. keys={list(out.keys())[:40]}")

    print("\n=== FAL OUTPUT ===")
    print("diffusers_lora_file.url =", diffusers_url)
    print("config_file.url         =", config_url or "(missing)")

    # Optional: mirror to Azure (recommended for vendor stability)
    if args.mirror_to_azure:
        conn = _env_str("AZURE_STORAGE_CONNECTION_STRING")
        if not conn:
            raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is required for --mirror-to-azure")

        bsc = BlobServiceClient.from_connection_string(conn)
        account_name, account_key = _azure_conn_parts(conn)

        ds = args.dataset_id.strip() or "unknown_dataset"
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        base_prefix = f"{args.azure_prefix.strip().strip('/')}/{ds}/{ts}_{request_id}"

        lora_bytes = _download_bytes(diffusers_url, timeout_s=600)
        lora_blob = f"{base_prefix}/diffusers_lora.safetensors"
        bsc.get_blob_client(container=args.azure_container, blob=lora_blob).upload_blob(lora_bytes, overwrite=True)

        cfg_blob = ""
        if config_url:
            cfg_bytes = _download_bytes(config_url, timeout_s=300)
            cfg_blob = f"{base_prefix}/config.json"
            bsc.get_blob_client(container=args.azure_container, blob=cfg_blob).upload_blob(cfg_bytes, overwrite=True)

        lora_sas = _sas_url(account_name, account_key, args.azure_container, lora_blob, hours=int(args.sas_hours))
        cfg_sas = _sas_url(account_name, account_key, args.azure_container, cfg_blob, hours=int(args.sas_hours)) if cfg_blob else ""

        print("\n=== MIRRORED TO AZURE ===")
        print("lora_blob =", lora_blob)
        print("lora_sas  =", lora_sas)
        if cfg_blob:
            print("cfg_blob  =", cfg_blob)
            print("cfg_sas   =", cfg_sas)

        print("\nSET THESE:")
        print("DF_SAREE_TRAINED_LORA_URL=" + lora_sas)
        print("DF_SAREE_TRAINED_LORA_SCALE=1.10")

    else:
        print("\nSET THIS (direct from fal):")
        print("DF_SAREE_TRAINED_LORA_URL=" + diffusers_url)
        print("DF_SAREE_TRAINED_LORA_SCALE=1.10")


if __name__ == "__main__":
    main()