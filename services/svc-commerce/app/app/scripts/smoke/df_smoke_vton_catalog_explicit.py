from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests


CORE_URL = os.getenv("CORE_URL", "http://localhost:8000").rstrip("/")
COMMERCE_URL = os.getenv("COMMERCE_URL", "http://localhost:8008").rstrip("/")
EMAIL = os.getenv("DF_EMAIL", "")
PASSWORD = os.getenv("DF_PASSWORD", "")

GARMENT_FILE = os.getenv("GARMENT_FILE", "")
GARMENT_KIND = os.getenv("GARMENT_KIND", "")
OUTFIT_KIND = os.getenv("OUTFIT_KIND", "")
MODEL_URL = os.getenv("MODEL_URL", "").strip()   # optional
TEST_LABEL = os.getenv("TEST_LABEL", "catalog_test")
TIMEOUT_S = int(os.getenv("TIMEOUT_S", "900"))
POLL_S = int(os.getenv("POLL_S", "5"))


def die(msg: str) -> None:
    raise SystemExit(msg)


def _jwt_sub(token: str) -> Optional[str]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
        body = json.loads(decoded)
        sub = body.get("sub")
        return str(sub).strip() if sub else None
    except Exception:
        return None


def login() -> tuple[str, str]:
    r = requests.post(
        f"{CORE_URL}/api/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()

    token = body.get("access_token")
    if not token:
        die(f"No access_token in login response: {body}")

    user_id = (
        body.get("user_id")
        or (body.get("user") or {}).get("id")
        or (body.get("data") or {}).get("user_id")
        or _jwt_sub(token)
    )
    if not user_id:
        die(f"No user_id in login response and JWT sub not found: {body}")

    return token, str(user_id)


def headers(token: str, user_id: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-User-Id": user_id,
    }


def upload_asset(token: str, user_id: str, file_path: str) -> Dict[str, Any]:
    p = Path(file_path)
    if not p.exists():
        die(f"GARMENT_FILE not found: {file_path}")

    with p.open("rb") as f:
        files = {"file": (p.name, f, "application/octet-stream")}
        data = {"kind": "product_image"}
        r = requests.post(
            f"{COMMERCE_URL}/api/commerce/assets/upload",
            headers=headers(token, user_id),
            data=data,
            files=files,
            timeout=180,
        )
    r.raise_for_status()
    return r.json()


def build_quote_payload(asset_upload: Dict[str, Any]) -> Dict[str, Any]:
    asset_url = (
        asset_upload.get("url")
        or asset_upload.get("asset_url")
        or asset_upload.get("sas_url")
        or asset_upload.get("blob_url")
    )
    asset_id = asset_upload.get("asset_id") or asset_upload.get("id")

    if not asset_url and not asset_id:
        die(f"Upload response missing usable asset reference: {asset_upload}")

    product_asset = {
        "asset_role": "primary",
        "garment_kind": GARMENT_KIND,
        "url": asset_url,
        "asset_id": asset_id,
        "meta": {"views": ["full_body"]},
    }

    payload: Dict[str, Any] = {
        "provider_kind": "platform_models",
        "outfit_kind": OUTFIT_KIND or GARMENT_KIND,
        "garment_kind": GARMENT_KIND,
        "product_assets": [product_asset],
        "variant_styles": [],
        "meta": {"test_label": TEST_LABEL},
    }

    if MODEL_URL:
        payload["model_ref"] = {
            "url": MODEL_URL,
            "human_image_url": MODEL_URL,
            "meta": {"views": ["full_body"]},
        }

    return payload


def quote(token: str, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(
        f"{COMMERCE_URL}/api/commerce/quote",
        headers={**headers(token, user_id), "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def confirm(token: str, user_id: str, quote_obj: Dict[str, Any]) -> Dict[str, Any]:
    quote_id = quote_obj.get("quote_id") or quote_obj.get("id")
    if not quote_id:
        die(f"Quote response missing quote_id: {quote_obj}")

    r = requests.post(
        f"{COMMERCE_URL}/api/commerce/confirm",
        headers={**headers(token, user_id), "Content-Type": "application/json"},
        json={"quote_id": quote_id},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def poll_status(token: str, user_id: str, job_id: str) -> Dict[str, Any]:
    start = time.time()
    while True:
        r = requests.get(
            f"{COMMERCE_URL}/api/commerce/jobs/{job_id}/status",
            headers=headers(token, user_id),
            params={"include_payload": 1},
            timeout=120,
        )
        r.raise_for_status()
        body = r.json()
        status = str(body.get("status") or "").lower()
        if status in {"succeeded", "failed", "canceled", "cancelled"}:
            return body
        if time.time() - start > TIMEOUT_S:
            die(f"Timed out polling job {job_id}")
        time.sleep(POLL_S)


def main() -> None:
    if not EMAIL or not PASSWORD:
        die("Set DF_EMAIL and DF_PASSWORD")
    if not GARMENT_FILE:
        die("Set GARMENT_FILE")
    if not GARMENT_KIND:
        die("Set GARMENT_KIND")

    token, user_id = login()
    uploaded = upload_asset(token, user_id, GARMENT_FILE)
    payload = build_quote_payload(uploaded)
    quote_obj = quote(token, user_id, payload)
    confirm_obj = confirm(token, user_id, quote_obj)

    job_id = confirm_obj.get("job_id") or confirm_obj.get("id")
    if not job_id:
        die(f"Confirm response missing job_id: {confirm_obj}")

    status = poll_status(token, user_id, job_id)

    print(json.dumps({
        "test_label": TEST_LABEL,
        "garment_kind": GARMENT_KIND,
        "outfit_kind": OUTFIT_KIND or GARMENT_KIND,
        "model_url": MODEL_URL or None,
        "quote": quote_obj,
        "confirm": confirm_obj,
        "status": status,
    }, indent=2))


if __name__ == "__main__":
    main()