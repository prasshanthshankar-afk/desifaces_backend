from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _jwt_sub(jwt_token: str) -> Optional[str]:
    try:
        parts = jwt_token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


def _http_json(method: str, url: str, headers: Optional[Dict[str, str]] = None, body: Any = None, timeout_s: int = 60) -> Any:
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        raise RuntimeError(f"HTTP {e.code} {method} {url}\n{raw}") from e


def _download(url: str, out_path: str, timeout_s: int = 120) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "desifaces-smoke/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
    with open(out_path, "wb") as f:
        f.write(data)


def _parse_conn_str(conn_str: str) -> Tuple[str, str]:
    parts = {}
    for seg in conn_str.split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k.strip()] = v.strip()
    name = parts.get("AccountName")
    key = parts.get("AccountKey")
    if not name or not key:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING must include AccountName and AccountKey")
    return name, key


def _maybe_upload_to_azure(manifest: Dict[str, Any], cases: List[Dict[str, Any]], run_dir: str) -> None:
    az = _as_dict(manifest.get("azure"))
    if not az.get("enabled"):
        return

    try:
        from azure.storage.blob import BlobServiceClient, ContentSettings, generate_blob_sas, BlobSasPermissions
    except Exception as e:
        raise RuntimeError("azure-storage-blob not installed. Run: python -m pip install -U azure-storage-blob") from e

    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not conn:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is required when azure.enabled=true in manifest")

    container = az.get("container", "commerce-training")
    prefix = az.get("prefix", "smoke/vton_assets/poc")
    ttl_minutes = int(az.get("ttl_minutes", 240))

    account_name, account_key = _parse_conn_str(conn)
    bsc = BlobServiceClient.from_connection_string(conn)
    cc = bsc.get_container_client(container)

    exp = dt.datetime.utcnow() + dt.timedelta(minutes=ttl_minutes)

    for case in cases:
        for item in case.get("items", []):
            src = str(item.get("source_url") or "").strip()
            if not src:
                continue

            # deterministic local file name
            h = hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]
            local = os.path.join(run_dir, "downloads", f"{case['name']}__{item['component_code']}__{h}.jpg")
            _download(src, local)

            blob_name = f"{prefix}/{case['name']}/{item['component_code']}/{os.path.basename(local)}"
            blob = cc.get_blob_client(blob_name)
            with open(local, "rb") as f:
                blob.upload_blob(
                    f,
                    overwrite=True,
                    content_settings=ContentSettings(content_type="image/jpeg"),
                )

            sas = generate_blob_sas(
                account_name=account_name,
                container_name=container,
                blob_name=blob_name,
                account_key=account_key,
                permission=BlobSasPermissions(read=True),
                expiry=exp,
            )
            item["image_url"] = f"https://{account_name}.blob.core.windows.net/{container}/{blob_name}?{sas}"

    # write the expanded manifest for debugging
    with open(os.path.join(run_dir, "manifest_resolved.json"), "w") as f:
        json.dump(manifest, f, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--poll_s", type=int, default=5)
    ap.add_argument("--timeout_s", type=int, default=240)
    args = ap.parse_args()

    manifest = json.load(open(args.manifest, "r"))
    core_url = (os.environ.get("CORE_URL") or manifest.get("core_url") or "").rstrip("/")
    commerce_url = (os.environ.get("COMMERCE_URL") or manifest.get("commerce_url") or "").rstrip("/")
    if not core_url or not commerce_url:
        raise RuntimeError("manifest must include core_url and commerce_url (or set CORE_URL / COMMERCE_URL)")

    email_env = manifest.get("email_env", "DF_EMAIL")
    password_env = manifest.get("password_env", "DF_PASSWORD")
    email = os.environ.get(email_env, "").strip()
    password = os.environ.get(password_env, "").strip()
    if not email or not password:
        raise RuntimeError(f"Missing creds. Set env vars {email_env} and {password_env}.")

    run_dir = f"/tmp/df_vton_smoke_poc_{time.strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(run_dir, exist_ok=True)

    # login
    auth = _http_json("POST", f"{core_url}/api/auth/login", body={"email": email, "password": password})
    token = (auth or {}).get("access_token") or (auth or {}).get("token") or ""
    if not token:
        raise RuntimeError(f"Unexpected login response: {auth}")
    user_id = (auth or {}).get("user_id") or _jwt_sub(token)
    if not user_id:
        raise RuntimeError("Could not infer user_id (neither auth.user_id nor JWT sub present).")

    with open(os.path.join(run_dir, "auth.json"), "w") as f:
        json.dump({"user_id": user_id, "access_token": token}, f, indent=2)

    headers = {"Authorization": f"Bearer {token}", "X-User-Id": str(user_id)}

    defaults = _as_dict(manifest.get("defaults"))
    mode = defaults.get("mode", "platform_models")
    product_type = defaults.get("product_type", "apparel")
    num_images = int(defaults.get("num_images", 4))
    resolution = defaults.get("resolution", "hd")
    provider_policy = defaults.get("provider_policy", "auto")

    cases = manifest.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise RuntimeError("manifest.cases[] required")

    # optional: upload all source images into commerce-training/smoke/... and replace item.image_url
    _maybe_upload_to_azure(manifest, cases, run_dir)

    summary = {"run_dir": run_dir, "core_url": core_url, "commerce_url": commerce_url, "cases": []}

    for case in cases:
        name = case.get("name")
        outfit_kind = case.get("outfit_kind") or "unknown"
        items = case.get("items") or []
        if not name or not items:
            raise RuntimeError(f"Bad case entry: {case}")

        # resolve URLs (prefer azure-uploaded item.image_url, fallback to source_url)
        resolved_items = []
        for it in items:
            url = (it.get("image_url") or it.get("source_url") or "").strip()
            if not url:
                continue
            resolved_items.append(
                {
                    "component_code": it["component_code"],
                    "kind": it.get("kind") or "garment",
                    "image_url": url,
                    "is_primary": bool(it.get("is_primary", False)),
                    "dominance_rank": it.get("dominance_rank"),
                    "meta": {"outfit_kind": outfit_kind},
                }
            )

        dominant = next((x["component_code"] for x in resolved_items if x.get("is_primary")), resolved_items[0]["component_code"])
        dominant_url = next((x["image_url"] for x in resolved_items if x["component_code"] == dominant), resolved_items[0]["image_url"])

        quote_in = {
            "mode": mode,
            "product_type": product_type,
            "outputs": {"num_images": num_images, "num_videos": 0},
            "views": {"full_body": True, "half_body": False},
            "resolution": resolution,
            "provider_policy": provider_policy,
            "product_assets": {
                "items": resolved_items,
                "dominant_component_code": dominant,
                "garment_image_url": dominant_url,
                "primary_image_url": dominant_url,
                "meta": {"outfit_kind": outfit_kind},
            },
        }

        quote_out = _http_json("POST", f"{commerce_url}/api/commerce/quote", headers=headers, body=quote_in)
        quote_id = (quote_out or {}).get("quote_id")
        if not quote_id:
            raise RuntimeError(f"quote failed: {quote_out}")

        confirm_out = _http_json("POST", f"{commerce_url}/api/commerce/confirm", headers=headers, body={"quote_id": quote_id})
        studio_job_id = (confirm_out or {}).get("studio_job_id")
        if not studio_job_id:
            raise RuntimeError(f"confirm failed: {confirm_out}")

        # poll status
        t0 = time.time()
        status = None
        while True:
            status = _http_json("GET", f"{commerce_url}/api/commerce/jobs/{studio_job_id}/status?include_payload=1", headers=headers)
            st = (status or {}).get("status") or (status or {}).get("stage") or ""
            if st in ("succeeded", "failed", "aborted"):
                break
            if time.time() - t0 > args.timeout_s:
                raise RuntimeError(f"timeout waiting for job {studio_job_id}. last={st}")
            time.sleep(args.poll_s)

        urls = (status or {}).get("urls") or (status or {}).get("output_urls") or []
        first_url = urls[0] if isinstance(urls, list) and urls else None

        out_case = {
            "name": name,
            "outfit_kind": outfit_kind,
            "quote_id": quote_id,
            "studio_job_id": studio_job_id,
            "status": (status or {}).get("status"),
            "first_url": first_url,
        }
        summary["cases"].append(out_case)

        with open(os.path.join(run_dir, f"{name}__quote_in.json"), "w") as f:
            json.dump(quote_in, f, indent=2)
        with open(os.path.join(run_dir, f"{name}__quote_out.json"), "w") as f:
            json.dump(quote_out, f, indent=2)
        with open(os.path.join(run_dir, f"{name}__confirm_out.json"), "w") as f:
            json.dump(confirm_out, f, indent=2)
        with open(os.path.join(run_dir, f"{name}__status.json"), "w") as f:
            json.dump(status, f, indent=2)

        print(f"[case] {name} status={(status or {}).get('status')} first_url={first_url}")

    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n✅ DONE\nRUN_DIR={run_dir}\n")


if __name__ == "__main__":
    main()
