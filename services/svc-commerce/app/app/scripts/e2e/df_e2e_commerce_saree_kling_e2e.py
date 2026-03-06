#!/usr/bin/env python3
"""
services/svc-commerce/app/app/scripts/e2e/df_e2e_commerce_saree_kling_e2e.py

Production-grade E2E test for svc-commerce saree try-on:

✅ Auth via svc-core (DF_EMAIL/DF_PASSWORD) OR use TOKEN directly
✅ Uses HUMAN_URL + GARMENT_URL (preferred) OR uploads if HUMAN_FILE/SAREE_FILE provided
✅ Accepts HUMAN_URL/GARMENT_URL as:
     - https://... (Azure blob/public)
     - az://container/blob/path
     - pasted lines like: export HUMAN_URL='https://...'
✅ Auto-fixes private Azure blob URLs by generating SAS URLs (inside container)
✅ POST /api/commerce/quote
✅ POST /api/commerce/confirm
✅ Poll /api/commerce/jobs/{job_id}/status?include_payload=1
✅ Optional: direct fal Kling try-on (RUN_DIRECT_KLING=1) to compare provider vs svc-commerce

IMPORTANT (mode enum):
  svc-commerce validates mode as one of:
    - platform_models
    - customer_tryon
  This script defaults to customer_tryon.

Run inside Docker (recommended):
  docker compose --env-file infra/.env exec -T svc-commerce bash -lc '
    export CORE_URL="http://svc-core:8000";
    export COMMERCE_URL="http://svc-commerce:8008";
    export DF_EMAIL="user2@desifaces.ai";
    export DF_PASSWORD="password2";
    export COMMERCE_E2E_MODE="customer_tryon";
    export HUMAN_URL="az://commerce-training/pools/.../persons/x.png";
    export GARMENT_URL="az://commerce-training/pools/.../sarees/y.png";
    python3 /app/app/scripts/e2e/df_e2e_commerce_saree_kling_e2e.py
  '

Optional Kling:
  export RUN_DIRECT_KLING=1
  export FAL_KEY="..."
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx


# ----------------------------
# utils
# ----------------------------

def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _mkdir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _write_json(path: str, obj: Any) -> None:
    _mkdir(os.path.dirname(path) or ".")
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return default


def _b64url_decode(s: str) -> bytes:
    s = s.strip()
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _jwt_claims(token: str) -> Dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = _b64url_decode(parts[1])
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def _headers(token: str, user_id: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-User-Id": user_id}


def _first_http_url(obj: Any) -> str:
    if isinstance(obj, dict):
        for _, v in obj.items():
            u = _first_http_url(v)
            if u:
                return u
    elif isinstance(obj, list):
        for v in obj:
            u = _first_http_url(v)
            if u:
                return u
    elif isinstance(obj, str):
        s = obj.strip()
        if s.startswith("http://") or s.startswith("https://"):
            return s
    return ""


def _find_urls(obj: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(obj, dict):
        return out

    for k in ("output_url", "baseline_url", "preview_url", "final_url"):
        v = obj.get(k)
        if isinstance(v, str) and v.startswith("http"):
            out[k] = v

    urls = obj.get("urls")
    if isinstance(urls, dict):
        for k, v in urls.items():
            if isinstance(v, str) and v.startswith("http"):
                out[f"urls.{k}"] = v

    comp = obj.get("computed")
    if isinstance(comp, dict):
        for k in ("output_url", "baseline_url"):
            v = comp.get(k)
            if isinstance(v, str) and v.startswith("http"):
                out[f"computed.{k}"] = v
        vo = comp.get("video_outputs")
        if isinstance(vo, dict):
            for k, v in vo.items():
                if isinstance(v, str) and v.startswith("http"):
                    out[f"computed.video_outputs.{k}"] = v

    for key in ("variants", "images", "outputs"):
        vv = obj.get(key)
        if isinstance(vv, list):
            for i, item in enumerate(vv[:10]):
                if isinstance(item, dict):
                    u = item.get("url") or item.get("output_url") or item.get("image_url")
                    if isinstance(u, str) and u.startswith("http"):
                        out[f"{key}[{i}].url"] = u

    return out


# ----------------------------
# Mode helper (schema-safe)
# ----------------------------

def _commerce_mode() -> str:
    m = (_env("COMMERCE_E2E_MODE", default="") or "").strip()
    if not m:
        m = "customer_tryon"
    if m not in ("platform_models", "customer_tryon"):
        raise RuntimeError(f"Invalid COMMERCE_E2E_MODE={m!r}. Expected 'platform_models' or 'customer_tryon'.")
    return m


# ----------------------------
# URL normalization + SAS helpers
# ----------------------------

def _clean_url_value(raw: str) -> str:
    """
    Accepts:
      - https://...
      - az://container/blob
      - export HUMAN_URL='https://...'
      - "https://..." / 'https://...'
    """
    s = (raw or "").strip()
    if not s:
        return ""

    if s.startswith("export "):
        # export HUMAN_URL='...'
        if "=" in s:
            s = s.split("=", 1)[1].strip()

    # strip surrounding quotes
    if (len(s) >= 2) and ((s[0] == s[-1]) and s[0] in ("'", '"')):
        s = s[1:-1].strip()

    return s.strip()


def _is_sas_url(url: str) -> bool:
    u = (url or "")
    return "?" in u and ("sig=" in u or "se=" in u or "sp=" in u)


def _parse_azure_conn_str(cs: str) -> Tuple[str, str]:
    parts: Dict[str, str] = {}
    for p in (cs or "").split(";"):
        if "=" in p:
            k, v = p.split("=", 1)
            parts[k] = v
    acct = (parts.get("AccountName") or "").strip()
    key = (parts.get("AccountKey") or "").strip()
    if not acct or not key:
        raise RuntimeError("Invalid AZURE_STORAGE_CONNECTION_STRING (missing AccountName/AccountKey)")
    return acct, key


def _parse_az_ref(az_ref: str) -> Tuple[str, str]:
    s = (az_ref or "").strip()
    if not s.startswith("az://"):
        raise RuntimeError(f"Not an az:// ref: {az_ref!r}")
    rest = s[len("az://") :]
    parts = [p for p in rest.split("/") if p]
    if len(parts) < 2:
        raise RuntimeError(f"Invalid az:// ref (need az://container/blob): {az_ref!r}")
    return parts[0], "/".join(parts[1:])


def _az_to_https_blob_url(az_ref: str) -> str:
    """
    az://container/blob -> https://{AccountName}.blob.core.windows.net/container/blob
    """
    conn = (os.getenv("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
    if not conn:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING missing (cannot resolve az:// to https)")
    acct, _ = _parse_azure_conn_str(conn)
    container, blob = _parse_az_ref(az_ref)
    return f"https://{acct}.blob.core.windows.net/{container}/{blob}"


def _make_sas_url(url: str, *, hours: int = 24) -> str:
    """
    Convert an Azure blob URL into a SAS URL using AZURE_STORAGE_CONNECTION_STRING.
    Supports:
      - https://{acct}.blob.core.windows.net/container/blob
      - az://container/blob   (auto-converted to https then SAS)
    """
    url = _clean_url_value(url)
    if not url:
        raise RuntimeError("Empty URL given to _make_sas_url")

    if url.startswith("az://"):
        url = _az_to_https_blob_url(url)

    if _is_sas_url(url):
        return url

    conn = (os.getenv("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
    if not conn:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING missing (cannot generate SAS)")

    try:
        from azure.storage.blob import generate_blob_sas, BlobSasPermissions  # type: ignore
    except Exception as e:
        raise RuntimeError(f"azure-storage-blob not available to generate SAS: {e}")

    acct, key = _parse_azure_conn_str(conn)

    u = urlparse(url)
    path = u.path.lstrip("/")
    if "/" not in path:
        raise RuntimeError(f"Not a blob URL: {url}")
    container, blob = path.split("/", 1)

    token = generate_blob_sas(
        account_name=acct,
        account_key=key,
        container_name=container,
        blob_name=blob,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(hours=hours),
    )
    return f"https://{acct}.blob.core.windows.net/{container}/{blob}?{token}"


def _normalize_input_url(raw: str) -> str:
    """
    Normalize env input into either:
      - https://...
      - az://...  (allowed; handled later)
    """
    s = _clean_url_value(raw)
    return s


def _head_status(client: httpx.Client, url: str) -> int:
    r = client.head(url, follow_redirects=True)
    return int(r.status_code)


def ensure_fetchable_url(client: httpx.Client, url: str, *, label: str, run_dir: str) -> str:
    """
    Ensure URL is reachable (200/3xx). If 403/404, auto-generate SAS and retry.
    Accepts az:// refs.
    Returns the URL to use downstream (possibly SAS).
    """
    url = _normalize_input_url(url)

    if not url:
        raise RuntimeError(f"{label} is empty")

    # If az://, first make it a SAS directly (no HEAD on az://)
    if url.startswith("az://"):
        hours = int(_env("SAS_HOURS", default="24") or "24")
        sas = _make_sas_url(url, hours=hours)
        status2 = _head_status(client, sas)
        _write_json(os.path.join(run_dir, f"head_{label}_az_to_sas.json"), {"az": url, "sas": sas, "status": status2})
        if status2 in (200, 301, 302, 307, 308):
            print(f"✅ {label}: az:// ref converted to SAS URL")
            return sas
        raise RuntimeError(f"{label}: az:// converted SAS still not reachable (HEAD {status2}). sas={sas}")

    # Must be http(s) now
    if not (url.startswith("http://") or url.startswith("https://")):
        raise RuntimeError(f"{label} must be http(s) or az://. Got: {url!r}")

    try:
        status = _head_status(client, url)
    except httpx.UnsupportedProtocol as e:
        raise RuntimeError(f"{label} invalid URL (missing scheme). value={url!r} err={e}") from e

    _write_json(os.path.join(run_dir, f"head_{label}.json"), {"url": url, "status": status})

    if status in (200, 301, 302, 307, 308):
        return url

    # Azure private blobs often return 404 (not 403)
    if status in (403, 404):
        hours = int(_env("SAS_HOURS", default="24") or "24")
        sas = _make_sas_url(url, hours=hours)
        status2 = _head_status(client, sas)
        _write_json(os.path.join(run_dir, f"head_{label}_sas.json"), {"original": url, "sas": sas, "status": status2})
        if status2 in (200, 301, 302, 307, 308):
            print(f"✅ {label}: private blob detected; using SAS URL")
            return sas

    raise RuntimeError(f"{label} not reachable (HEAD {status}). Provide SAS/public URL. url={url}")


# ----------------------------
# Auth
# ----------------------------

@dataclass
class Auth:
    token: str
    user_id: str


def login_or_use_token(client: httpx.Client, core_url: str, run_dir: str) -> Auth:
    token = _env("TOKEN", "DF_TOKEN", "MARKETING_TOKEN", default="")
    x_user = _env("X_USER_ID", "DF_X_USER_ID", default="")

    if token:
        claims = _jwt_claims(token)
        user_id = x_user or str(claims.get("sub") or "").strip()
        if not user_id:
            raise RuntimeError("TOKEN provided but could not infer user_id (sub). Set X_USER_ID.")
        _write_json(os.path.join(run_dir, "auth_from_token.json"), {"user_id": user_id, "claims": claims})
        return Auth(token=token, user_id=user_id)

    email = _env("DF_EMAIL", "MARKETING_EMAIL", "EMAIL", default="")
    password = _env("DF_PASSWORD", "MARKETING_PASSWORD", "PASSWORD", default="")
    if not email or not password:
        raise RuntimeError("Missing DF_EMAIL/DF_PASSWORD or TOKEN.")

    url = core_url.rstrip("/") + "/api/auth/login"
    payload = {"email": email, "password": password}
    r = client.post(url, json=payload)

    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}

    _write_json(os.path.join(run_dir, "login_response.json"), {"status": r.status_code, "body": j})

    if r.status_code >= 300:
        raise RuntimeError(f"Login failed status={r.status_code} body={j}")

    token = (
        j.get("access_token")
        or j.get("token")
        or j.get("jwt")
        or (j.get("data") or {}).get("access_token")
        or ""
    )
    if not token:
        raise RuntimeError(f"Login response missing token. keys={list(j.keys())}")

    user_id = (
        j.get("user_id")
        or j.get("x_user_id")
        or (j.get("data") or {}).get("user_id")
        or ""
    )
    if not user_id:
        claims = _jwt_claims(token)
        user_id = str(claims.get("sub") or "").strip()

    if not user_id:
        raise RuntimeError("Could not determine user_id. Set X_USER_ID explicitly.")

    _write_json(os.path.join(run_dir, "auth.json"), {"email": email, "user_id": user_id, "claims": _jwt_claims(token)})
    return Auth(token=token, user_id=user_id)


# ----------------------------
# HTTP helpers
# ----------------------------

def healthcheck(client: httpx.Client, url: str) -> None:
    r = client.get(url.rstrip("/") + "/api/health")
    if r.status_code != 200:
        raise RuntimeError(f"Healthcheck failed: {url} status={r.status_code} body={r.text[:200]}")


def upload_asset(
    *,
    client: httpx.Client,
    commerce_url: str,
    auth: Auth,
    kind_candidates: Tuple[str, ...],
    file_path: str,
    run_dir: str,
    label: str,
) -> str:
    if not os.path.exists(file_path):
        raise RuntimeError(f"{label} file not found: {file_path}")

    endpoint = commerce_url.rstrip("/") + "/api/commerce/assets/upload"
    last_err: Optional[str] = None

    for kind in kind_candidates:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f, "application/octet-stream")}
            data = {"kind": kind}
            r = client.post(endpoint, headers=_headers(auth.token, auth.user_id), data=data, files=files)

        try:
            j = r.json()
        except Exception:
            j = {"raw": r.text}

        _write_json(os.path.join(run_dir, f"upload_{label}_{kind}.json"), {"status": r.status_code, "body": j})

        if r.status_code == 200:
            u = _first_http_url(j)
            if u:
                return u
            last_err = f"upload ok but no url in response kind={kind}"
        else:
            last_err = f"upload failed kind={kind} status={r.status_code}"

    raise RuntimeError(f"Failed to upload {label}. last_err={last_err}")


# ----------------------------
# svc-commerce flow
# ----------------------------

def post_quote(
    *,
    client: httpx.Client,
    commerce_url: str,
    auth: Auth,
    human_url: str,
    garment_url: str,
    run_dir: str,
) -> Dict[str, Any]:
    endpoint = commerce_url.rstrip("/") + "/api/commerce/quote"

    mode = _commerce_mode()

    quote_payload: Dict[str, Any] = {
        "mode": mode,
        "product_type": "apparel",
        "resolution": _env("COMMERCE_E2E_RESOLUTION", default="hd"),
        "outputs": {"num_images": int(_env("COMMERCE_E2E_NUM_IMAGES", default="1") or "1"), "num_videos": 0},

        "outfit_kind": "saree_set",
        "garment_type": _env("COMMERCE_E2E_GARMENT_TYPE", default="dresses"),
        "drape_style": _env("COMMERCE_E2E_DRAPE_STYLE", default="nivi"),
        "views": {"full_body": True},

        "model_ref": {"url": human_url, "full_body": True},
        "product_assets": {"items": [{"component_code": "saree", "name": "saree", "image_url": garment_url}]},

        # duplicate in input to survive schema drift
        "input": {
            "outfit_kind": "saree_set",
            "garment_type": _env("COMMERCE_E2E_GARMENT_TYPE", default="dresses"),
            "drape_style": _env("COMMERCE_E2E_DRAPE_STYLE", default="nivi"),
            "views": {"full_body": True},
            "model_ref": {"url": human_url, "full_body": True},
            "product_assets": {"items": [{"component_code": "saree", "name": "saree", "image_url": garment_url}]},
        },
    }

    r = client.post(endpoint, headers=_headers(auth.token, auth.user_id), json=quote_payload)
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}

    _write_json(os.path.join(run_dir, "quote_request.json"), quote_payload)
    _write_json(os.path.join(run_dir, "quote_response.json"), {"status": r.status_code, "body": j})

    if r.status_code >= 300:
        raise RuntimeError(f"quote failed status={r.status_code} body={j}")

    return j


def post_confirm(
    *,
    client: httpx.Client,
    commerce_url: str,
    auth: Auth,
    quote_resp: Dict[str, Any],
    run_dir: str,
) -> Dict[str, Any]:
    endpoint = commerce_url.rstrip("/") + "/api/commerce/confirm"

    quote_id = (
        quote_resp.get("quote_id")
        or quote_resp.get("id")
        or (quote_resp.get("quote") or {}).get("quote_id")
        or (quote_resp.get("quote") or {}).get("id")
        or ""
    )
    if not quote_id:
        raise RuntimeError(f"Could not find quote_id in quote response keys={list(quote_resp.keys())}")

    bodies = [
        {"quote_id": quote_id},
        {"quote_id": quote_id, "confirm": True},
        {"quote_id": quote_id, "accepted": True},
        {"quote_id": quote_id, "accept": True},
    ]

    last: Optional[Dict[str, Any]] = None
    for body in bodies:
        r = client.post(endpoint, headers=_headers(auth.token, auth.user_id), json=body)
        try:
            j = r.json()
        except Exception:
            j = {"raw": r.text}
        _write_json(
            os.path.join(run_dir, f"confirm_try_{'_'.join(body.keys())}.json"),
            {"status": r.status_code, "request": body, "body": j},
        )
        last = {"status": r.status_code, "body": j}
        if r.status_code < 300:
            _write_json(os.path.join(run_dir, "confirm_response.json"), j)
            return j

    raise RuntimeError(f"confirm failed. last={last}")


def poll_job(
    *,
    client: httpx.Client,
    commerce_url: str,
    auth: Auth,
    confirm_resp: Dict[str, Any],
    run_dir: str,
) -> Dict[str, Any]:
    job_id = (
        confirm_resp.get("job_id")
        or confirm_resp.get("studio_job_id")      # ✅ critical for your current API
        or confirm_resp.get("id")
        or confirm_resp.get("commerce_job_id")
        or (confirm_resp.get("job") or {}).get("job_id")
        or (confirm_resp.get("job") or {}).get("id")
        or ""
    )
    if not job_id:
        raise RuntimeError(f"Could not find job_id in confirm response keys={list(confirm_resp.keys())}")

    endpoint = commerce_url.rstrip("/") + f"/api/commerce/jobs/{job_id}/status"
    timeout_s = int(_env("COMMERCE_E2E_POLL_TIMEOUT_S", default="900") or "900")
    poll_s = float(_env("COMMERCE_E2E_POLL_SECS", default="2.0") or "2.0")
    t0 = time.time()
    timeline_path = os.path.join(run_dir, "poll_timeline.log")

    while True:
        if time.time() - t0 > timeout_s:
            raise RuntimeError(f"job poll timed out after {timeout_s}s job_id={job_id}")

        r = client.get(endpoint, headers=_headers(auth.token, auth.user_id), params={"include_payload": "1"})
        try:
            j = r.json()
        except Exception:
            j = {"raw": r.text}

        _write_json(os.path.join(run_dir, "job_status_last.json"), {"status": r.status_code, "body": j})

        if r.status_code >= 300:
            raise RuntimeError(f"job status failed status={r.status_code} body={j}")

        status = str(j.get("status") or j.get("state") or j.get("job_status") or "").lower()
        stage = str(j.get("stage") or (j.get("computed") or {}).get("stage") or "")
        provider = str((j.get("computed") or {}).get("provider") or (j.get("result") or {}).get("provider") or "")
        elapsed = int(time.time() - t0)

        line = f"[poll] job={job_id} status={status} stage={stage} provider={provider} elapsed_s={elapsed}"
        print(line)
        with open(timeline_path, "a") as f:
            f.write(line + "\n")

        if status in ("succeeded", "success", "completed", "done"):
            return j
        if status in ("failed", "error", "canceled", "cancelled"):
            return j

        time.sleep(poll_s)


# ----------------------------
# Direct Kling (optional)
# ----------------------------

def run_direct_kling(client: httpx.Client, *, human_url: str, garment_url: str, run_dir: str) -> Optional[str]:
    if (_env("RUN_DIRECT_KLING", default="0") or "0").strip().lower() not in ("1", "true", "yes", "on"):
        return None

    fal_key = _env("FAL_KEY", "FAL_API_KEY", default="")
    if not fal_key:
        raise RuntimeError("RUN_DIRECT_KLING=1 but FAL_KEY is missing")

    endpoint_id = _env("FAL_KLING_ENDPOINT_ID", default="fal-ai/kling/v1-5/kolors-virtual-try-on").strip().strip("/")
    base = "https://queue.fal.run"
    submit_url = f"{base}/{endpoint_id}"

    r = client.post(
        submit_url,
        headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"},
        json={"human_image_url": human_url, "garment_image_url": garment_url},
    )
    r.raise_for_status()
    submit = r.json()
    _write_json(os.path.join(run_dir, "kling_submit.json"), submit)

    rid = str(submit.get("request_id") or "").strip()
    if not rid:
        raise RuntimeError(f"Kling submit missing request_id: {submit}")

    status_url = f"{base}/{endpoint_id}/requests/{rid}/status"
    result_url = f"{base}/{endpoint_id}/requests/{rid}"

    t0 = time.time()
    while True:
        st = client.get(status_url, headers={"Authorization": f"Key {fal_key}"}).json()
        _write_json(os.path.join(run_dir, "kling_status.json"), st)
        s = str(st.get("status") or "").upper()
        print(f"[kling] request_id={rid} status={s} elapsed_s={int(time.time() - t0)}")
        if s == "COMPLETED":
            break
        if s in ("FAILED", "ERROR", "CANCELED", "CANCELLED"):
            raise RuntimeError(f"Kling failed: {st}")
        time.sleep(2.0)

    out = client.get(result_url, headers={"Authorization": f"Key {fal_key}"}).json()
    _write_json(os.path.join(run_dir, "kling_result.json"), out)
    return _first_http_url(out) or None


def main() -> None:
    # better defaults inside Docker
    in_docker = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    core_default = "http://svc-core:8000" if in_docker else "http://localhost:8000"
    commerce_default = "http://svc-commerce:8008" if in_docker else "http://localhost:8008"

    core_url = _env("CORE_URL", default=core_default)
    commerce_url = _env("COMMERCE_URL", default=commerce_default)

    run_dir = _env("RUN_DIR", default=os.path.join("/tmp", f"df_e2e_commerce_saree_{_now_tag()}"))
    _mkdir(run_dir)
    print(f"✅ Run dir: {run_dir}")

    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        print("🔎 Healthchecks...")
        healthcheck(client, core_url)
        healthcheck(client, commerce_url)
        print("✅ Healthchecks OK")

        auth = login_or_use_token(client, core_url, run_dir)
        print(f"✅ Auth OK user_id={auth.user_id}")

        # inputs: prefer URL mode
        human_url = _normalize_input_url(_env("HUMAN_URL", default=""))
        garment_url = _normalize_input_url(_env("GARMENT_URL", default=""))

        # optional upload mode
        human_file = _env("HUMAN_FILE", default="")
        saree_file = _env("SAREE_FILE", default="")

        if human_file and not human_url:
            print("⬆️ Uploading human file...")
            human_url = upload_asset(
                client=client,
                commerce_url=commerce_url,
                auth=auth,
                kind_candidates=("human_full", "human", "model_full", "model", "person_full", "person", "full_body_model", "full_body"),
                file_path=human_file,
                run_dir=run_dir,
                label="human",
            )

        if saree_file and not garment_url:
            print("⬆️ Uploading saree file...")
            garment_url = upload_asset(
                client=client,
                commerce_url=commerce_url,
                auth=auth,
                kind_candidates=("saree_full", "saree", "garment", "uploaded_garment", "product"),
                file_path=saree_file,
                run_dir=run_dir,
                label="saree",
            )

        if not human_url or not garment_url:
            raise RuntimeError("Provide HUMAN_URL and GARMENT_URL (or HUMAN_FILE/SAREE_FILE).")

        print(f"✅ HUMAN_URL(raw)={human_url}")
        print(f"✅ GARMENT_URL(raw)={garment_url}")

        # Ensure URLs are fetchable by providers (auto-SAS if private Azure)
        print("🔎 Ensuring provider fetchability (auto-SAS if needed)...")
        human_url = ensure_fetchable_url(client, human_url, label="human_url", run_dir=run_dir)
        garment_url = ensure_fetchable_url(client, garment_url, label="garment_url", run_dir=run_dir)

        _write_json(os.path.join(run_dir, "inputs.json"), {"human_url": human_url, "garment_url": garment_url})

        kling_url = run_direct_kling(client, human_url=human_url, garment_url=garment_url, run_dir=run_dir)
        if kling_url:
            print(f"🟣 Direct Kling output: {kling_url}")

        print("🧾 POST /api/commerce/quote ...")
        quote = post_quote(client=client, commerce_url=commerce_url, auth=auth, human_url=human_url, garment_url=garment_url, run_dir=run_dir)

        print("✅ POST /api/commerce/confirm ...")
        confirm = post_confirm(client=client, commerce_url=commerce_url, auth=auth, quote_resp=quote, run_dir=run_dir)

        print("⏳ Poll /api/commerce/jobs/{job_id}/status ...")
        status = poll_job(client=client, commerce_url=commerce_url, auth=auth, confirm_resp=confirm, run_dir=run_dir)
        _write_json(os.path.join(run_dir, "final_status.json"), status)

        urls = _find_urls(status)
        if not urls:
            urls = {"first_http_url_found": _first_http_url(status)}

        summary = {
            "run_dir": run_dir,
            "core_url": core_url,
            "commerce_url": commerce_url,
            "mode": _commerce_mode(),
            "user_id": auth.user_id,
            "human_url": human_url,
            "garment_url": garment_url,
            "direct_kling_url": kling_url,
            "extracted_urls": urls,
        }
        _write_json(os.path.join(run_dir, "summary.json"), summary)

        print("\n====== RESULT ======")
        print(json.dumps(summary, indent=2))
        print("====================\n")


if __name__ == "__main__":
    main()