#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import io
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def now_ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(obj) + "\n", encoding="utf-8")


def parse_json_bytes(body: bytes) -> Any:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {"raw_text": text}


def b64url_decode(segment: str) -> bytes:
    padding = "=" * ((4 - (len(segment) % 4)) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def jwt_sub(access_token: str) -> Optional[str]:
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(b64url_decode(parts[1]).decode("utf-8"))
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


def http_request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: int = 120,
) -> Tuple[int, bytes, Any]:
    req = urllib.request.Request(url=url, data=data, method=method.upper())
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.getcode(), body, parse_json_bytes(body)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, body, parse_json_bytes(body)
    except urllib.error.URLError as exc:
        payload = {"error": "url_error", "reason": str(exc)}
        body = json.dumps(payload).encode("utf-8")
        return 0, body, payload


def multipart_encode(
    *,
    fields: Optional[Dict[str, str]] = None,
    files: Optional[List[Tuple[str, str, bytes, str]]] = None,
) -> Tuple[str, bytes]:
    boundary = f"----DesiFacesBoundary{uuid4().hex}"
    buf = io.BytesIO()

    def write_line(line: bytes) -> None:
        buf.write(line + b"\r\n")

    for name, value in (fields or {}).items():
        write_line(f"--{boundary}".encode("utf-8"))
        write_line(f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"))
        write_line(b"")
        write_line(str(value).encode("utf-8"))

    for field_name, filename, content, content_type in (files or []):
        write_line(f"--{boundary}".encode("utf-8"))
        write_line(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"'
            ).encode("utf-8")
        )
        write_line(f"Content-Type: {content_type}".encode("utf-8"))
        write_line(b"")
        buf.write(content)
        buf.write(b"\r\n")

    write_line(f"--{boundary}--".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", buf.getvalue()


def extract_access_token(login_json: Dict[str, Any]) -> Optional[str]:
    candidates = [
        login_json.get("access_token"),
        login_json.get("token"),
        (login_json.get("data") or {}).get("access_token")
        if isinstance(login_json.get("data"), dict)
        else None,
        (login_json.get("data") or {}).get("token")
        if isinstance(login_json.get("data"), dict)
        else None,
    ]
    for token in candidates:
        if token:
            return str(token)
    return None


def extract_user_id(login_json: Dict[str, Any], access_token: Optional[str]) -> Optional[str]:
    candidates = [
        login_json.get("user_id"),
        login_json.get("id"),
        (login_json.get("user") or {}).get("id")
        if isinstance(login_json.get("user"), dict)
        else None,
        (login_json.get("data") or {}).get("user_id")
        if isinstance(login_json.get("data"), dict)
        else None,
    ]
    for user_id in candidates:
        if user_id:
            return str(user_id)
    if access_token:
        return jwt_sub(access_token)
    return None


def login(core_url: str, email: str, password: str, timeout: int) -> Dict[str, Any]:
    url = f"{core_url.rstrip('/')}/api/auth/login"
    payload = {"email": email, "password": password}
    status, _, data = http_request(
        "POST",
        url,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"Login failed ({status}): {json_dumps(data)}")

    token = extract_access_token(data)
    if not token:
        raise RuntimeError(f"Login succeeded but no access token found: {json_dumps(data)}")

    user_id = extract_user_id(data, token)
    if not user_id:
        raise RuntimeError(f"Login succeeded but no user_id found: {json_dumps(data)}")

    return {
        "access_token": token,
        "user_id": user_id,
        "raw": data,
    }


def upload_asset(
    commerce_url: str,
    access_token: str,
    user_id: str,
    role: str,
    file_path: Path,
    timeout: int,
) -> Dict[str, Any]:
    if not file_path.exists():
        raise RuntimeError(f"File not found: {file_path}")

    content = file_path.read_bytes()
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

    mp_type, mp_body = multipart_encode(
        fields={"role": role},
        files=[("file", file_path.name, content, content_type)],
    )

    url = f"{commerce_url.rstrip('/')}/api/commerce/assets/upload"

    status, _, data = http_request(
        "POST",
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-User-Id": user_id,
            "Content-Type": mp_type,
            "Accept": "application/json",
        },
        data=mp_body,
        timeout=timeout,
    )

    if status not in (200, 201):
        raise RuntimeError(
            f"Upload failed for role={role}, file={file_path} ({status}): {json_dumps(data)}"
        )

    asset_id = data.get("asset_id")
    if not asset_id:
        raise RuntimeError(
            f"Upload returned success but missing asset_id for file={file_path}: {json_dumps(data)}"
        )

    return data


def build_quote_payloads(
    garment_uploads: Sequence[Dict[str, Any]],
    garment_type: str,
    variant_count: int,
    mode: str,
    model_upload: Optional[Dict[str, Any]],
    provider: Optional[str],
    strict: bool,
) -> List[Dict[str, Any]]:
    api_mode = "platform_models" if mode == "vendor_only" else "customer_tryon"

    items: List[Dict[str, Any]] = []
    for idx, uploaded in enumerate(garment_uploads, start=1):
        asset_id = str(uploaded.get("asset_id") or "")
        preview_url = str(uploaded.get("preview_url") or "")
        storage_ref = str(uploaded.get("storage_ref") or "")

        item: Dict[str, Any] = {
            "asset_id": asset_id,
            "role": "garment",
            "component_code": garment_type,
            "garment_image_url": preview_url,
            "image_url": preview_url,
            "url": preview_url,
            "preview_url": preview_url,
            "storage_ref": storage_ref,
            "meta": {
                "garment_type": garment_type,
                "outfit_kind": garment_type,
                "item_index": idx,
                "views": ["front"],
            },
        }
        items.append(item)

    if not items:
        raise RuntimeError("build_quote_payloads: no garment uploads were provided")

    provider_hints: Dict[str, Any] = {}
    if provider:
        provider_hints["provider"] = provider
        provider_hints["fusion_provider"] = provider
    if strict:
        provider_hints["strict"] = True

    payload: Dict[str, Any] = {
        "mode": api_mode,
        "product_type": "apparel",
        "variant_count": variant_count,
        "product_assets": {
            "items": items,
            "primary": items[0],
        },
        "garment_image_url": items[0]["garment_image_url"],
    }

    if provider_hints:
        payload["provider_hints"] = provider_hints

    if model_upload and api_mode == "customer_tryon":
        model_asset_id = str(model_upload.get("asset_id") or "")
        model_preview_url = str(model_upload.get("preview_url") or "")
        payload["person_asset_id"] = model_asset_id
        payload["person_image_url"] = model_preview_url
        payload["model_image_url"] = model_preview_url

    return [payload]


def try_quote(
    commerce_url: str,
    access_token: str,
    user_id: str,
    payloads: Sequence[Dict[str, Any]],
    timeout: int,
) -> Tuple[str, Dict[str, Any], str]:
    url = f"{commerce_url.rstrip('/')}/api/commerce/quote"
    attempts: List[Dict[str, Any]] = []

    for idx, payload in enumerate(payloads, start=1):
        status, _, data = http_request(
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-User-Id": user_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            data=json.dumps(payload).encode("utf-8"),
            timeout=timeout,
        )
        if 200 <= status < 300:
            return url, data, json_dumps(payload)

        attempts.append(
            {
                "url": url,
                "payload_index": idx,
                "status": status,
                "payload": payload,
                "response": data,
            }
        )

    raise RuntimeError("Quote failed for all payloads:\n" + json_dumps(attempts))


def extract_quote_id(quote_json: Dict[str, Any]) -> Optional[str]:
    candidates = [
        quote_json.get("quote_id"),
        quote_json.get("id"),
        (quote_json.get("quote") or {}).get("id")
        if isinstance(quote_json.get("quote"), dict)
        else None,
    ]
    for value in candidates:
        if value:
            return str(value)
    return None


def extract_job_id(obj: Dict[str, Any]) -> Optional[str]:
    candidates = [
        obj.get("job_id"),
        obj.get("studio_job_id"),
        obj.get("commerce_job_id"),
        obj.get("id"),
        (obj.get("job") or {}).get("id") if isinstance(obj.get("job"), dict) else None,
        (obj.get("job") or {}).get("job_id") if isinstance(obj.get("job"), dict) else None,
        (obj.get("job") or {}).get("studio_job_id") if isinstance(obj.get("job"), dict) else None,
        (obj.get("data") or {}).get("job_id") if isinstance(obj.get("data"), dict) else None,
        (obj.get("data") or {}).get("studio_job_id") if isinstance(obj.get("data"), dict) else None,
    ]
    for value in candidates:
        if value:
            return str(value)
    return None


def try_confirm(
    commerce_url: str,
    access_token: str,
    user_id: str,
    quote_id: str,
    timeout: int,
) -> Tuple[str, Dict[str, Any], str]:
    candidate_paths = [
        "/api/commerce/confirm",
        "/api/commerce/vton/confirm",
    ]
    payloads = [
        {"quote_id": quote_id},
        {"quote_id": quote_id, "accept": True},
        {"id": quote_id},
    ]
    attempts: List[Dict[str, Any]] = []

    for path in candidate_paths:
        url = f"{commerce_url.rstrip('/')}{path}"
        for payload in payloads:
            status, _, data = http_request(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-User-Id": user_id,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                data=json.dumps(payload).encode("utf-8"),
                timeout=timeout,
            )
            if 200 <= status < 300:
                return url, data, json_dumps(payload)

            attempts.append(
                {
                    "url": url,
                    "status": status,
                    "payload": payload,
                    "response": data,
                }
            )

    raise RuntimeError("Confirm failed for all candidate endpoints/payloads:\n" + json_dumps(attempts))


def extract_status(obj: Dict[str, Any]) -> str:
    candidates = [
        obj.get("status"),
        obj.get("state"),
        obj.get("job_status"),
        (obj.get("job") or {}).get("status") if isinstance(obj.get("job"), dict) else None,
        (obj.get("data") or {}).get("status") if isinstance(obj.get("data"), dict) else None,
    ]
    for value in candidates:
        if value:
            return str(value)
    return "unknown"


def is_terminal(status: str) -> bool:
    return status.lower() in {
        "succeeded",
        "success",
        "failed",
        "error",
        "cancelled",
        "canceled",
    }


def status_is_success(status: str) -> bool:
    return status.lower() in {"succeeded", "success"}


def try_status_once(
    commerce_url: str,
    access_token: str,
    user_id: str,
    job_id: str,
    timeout: int,
) -> Tuple[str, int, Dict[str, Any]]:
    candidate_paths = [
        f"/api/commerce/jobs/{job_id}/status",
        f"/api/commerce/vton/jobs/{job_id}/status",
    ]

    last_url = ""
    last_status = 0
    last_data: Dict[str, Any] = {}

    for path in candidate_paths:
        url = f"{commerce_url.rstrip('/')}{path}"
        status, _, data = http_request(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-User-Id": user_id,
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        if 200 <= status < 300:
            return url, status, data
        last_url, last_status, last_data = url, status, data

    return last_url, last_status, last_data


def collect_http_urls(obj: Any, found: Optional[List[str]] = None) -> List[str]:
    if found is None:
        found = []

    if isinstance(obj, dict):
        for _, value in obj.items():
            collect_http_urls(value, found)
    elif isinstance(obj, list):
        for item in obj:
            collect_http_urls(item, found)
    elif isinstance(obj, str):
        if obj.startswith("http://") or obj.startswith("https://"):
            if obj not in found:
                found.append(obj)

    return found


def poll_job(
    commerce_url: str,
    access_token: str,
    user_id: str,
    job_id: str,
    poll_interval_sec: int,
    timeout_sec: int,
    out_dir: Path,
) -> Dict[str, Any]:
    started = time.time()
    last_data: Dict[str, Any] = {}

    while True:
        url, http_code, data = try_status_once(
            commerce_url=commerce_url,
            access_token=access_token,
            user_id=user_id,
            job_id=job_id,
            timeout=120,
        )
        last_data = data if isinstance(data, dict) else {"raw": data}
        status = extract_status(last_data)
        elapsed = int(time.time() - started)

        print(f"[poll] elapsed={elapsed}s status={status} http={http_code} url={url}")
        write_json(out_dir / "status_latest.json", last_data)

        if http_code >= 200 and http_code < 300 and is_terminal(status):
            return last_data

        if time.time() - started >= timeout_sec:
            raise RuntimeError(
                f"Polling timed out after {timeout_sec}s. Last response:\n{json_dumps(last_data)}"
            )

        time.sleep(poll_interval_sec)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Single-shot DesiFaces VTON runner for any garment type."
    )
    ap.add_argument(
        "--core-url",
        default=os.environ.get("CORE_URL", "http://localhost:8000"),
        help="svc-core base URL",
    )
    ap.add_argument(
        "--commerce-url",
        default=os.environ.get("COMMERCE_URL", "http://localhost:8008"),
        help="svc-commerce base URL",
    )
    ap.add_argument(
        "--email",
        default=os.environ.get("DF_EMAIL"),
        required=os.environ.get("DF_EMAIL") is None,
        help="login email",
    )
    ap.add_argument(
        "--password",
        default=os.environ.get("DF_PASSWORD"),
        required=os.environ.get("DF_PASSWORD") is None,
        help="login password",
    )
    ap.add_argument(
        "--garment-type",
        required=True,
        help="e.g. salwar_suit, shirt, tshirt, blazer, lehenga, kurta_set, saree_set",
    )
    ap.add_argument(
        "--garment-file",
        action="append",
        required=True,
        help="path to garment image; pass multiple times for multi-piece outfits",
    )
    ap.add_argument(
        "--person-file",
        default=None,
        help="optional model/person image file; omit for vendor_only/platform-model flow",
    )
    ap.add_argument(
        "--mode",
        default="vendor_only",
        choices=["vendor_only", "user_model"],
        help="vendor_only => platform_models, user_model => customer_tryon",
    )
    ap.add_argument(
        "--variants",
        type=int,
        default=4,
        help="number of requested variants",
    )
    ap.add_argument(
        "--provider",
        default=None,
        help="optional provider override",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="pass strict hint in provider_hints",
    )
    ap.add_argument(
        "--poll-interval-sec",
        type=int,
        default=10,
        help="poll interval",
    )
    ap.add_argument(
        "--timeout-sec",
        type=int,
        default=1800,
        help="overall job poll timeout",
    )
    ap.add_argument(
        "--out-dir",
        default=f"/tmp/df_vton_one_shot_{now_ts()}",
        help="output directory",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    garment_files = [Path(p).expanduser().resolve() for p in args.garment_file]
    person_file = Path(args.person_file).expanduser().resolve() if args.person_file else None

    print(f"OUT_DIR={out_dir}")
    print("[1/6] Login...")
    auth = login(args.core_url, args.email, args.password, timeout=120)
    access_token = auth["access_token"]
    user_id = auth["user_id"]
    write_json(out_dir / "auth.json", auth["raw"])
    print(f"X_USER_ID={user_id}")

    print("[2/6] Upload garment image(s)...")
    garment_uploads: List[Dict[str, Any]] = []
    garment_asset_ids: List[str] = []
    for idx, garment_file in enumerate(garment_files, start=1):
        uploaded = upload_asset(
            commerce_url=args.commerce_url,
            access_token=access_token,
            user_id=user_id,
            role="garment",
            file_path=garment_file,
            timeout=300,
        )
        garment_uploads.append(uploaded)
        garment_asset_ids.append(str(uploaded["asset_id"]))
        write_json(out_dir / f"upload_garment_{idx}.json", uploaded)
        print(
            f"  garment[{idx}] asset_id={uploaded['asset_id']} "
            f"preview_url={uploaded.get('preview_url', '')}"
        )

    model_asset_id: Optional[str] = None
    model_upload: Optional[Dict[str, Any]] = None
    if args.mode == "user_model":
        if not person_file:
            raise RuntimeError("--mode user_model requires --person-file")
        print("[3/6] Upload person/model image...")
        uploaded_model = upload_asset(
            commerce_url=args.commerce_url,
            access_token=access_token,
            user_id=user_id,
            role="model",
            file_path=person_file,
            timeout=300,
        )
        model_upload = uploaded_model
        model_asset_id = str(uploaded_model["asset_id"])
        write_json(out_dir / "upload_model.json", uploaded_model)
        print(
            f"  model asset_id={model_asset_id} "
            f"preview_url={uploaded_model.get('preview_url', '')}"
        )
    else:
        print("[3/6] Skipping person upload (vendor_only mode)...")

    print("[4/6] Quote...")
    quote_payloads = build_quote_payloads(
        garment_uploads=garment_uploads,
        garment_type=args.garment_type,
        variant_count=args.variants,
        mode=args.mode,
        model_upload=model_upload,
        provider=args.provider,
        strict=args.strict,
    )
    quote_url, quote_json, quote_payload_used = try_quote(
        commerce_url=args.commerce_url,
        access_token=access_token,
        user_id=user_id,
        payloads=quote_payloads,
        timeout=300,
    )
    write_json(out_dir / "quote.json", quote_json)
    (out_dir / "quote_payload_used.json").write_text(quote_payload_used + "\n", encoding="utf-8")
    print(f"  quote endpoint={quote_url}")

    job_id = extract_job_id(quote_json)
    confirm_json: Dict[str, Any] = {}

    if job_id:
        print("[5/6] Quote already returned job_id; skipping confirm...")
        write_json(out_dir / "confirm.json", {"skipped": True, "reason": "job_id_from_quote"})
    else:
        quote_id = extract_quote_id(quote_json)
        if not quote_id:
            raise RuntimeError(f"Quote succeeded but no quote_id/job_id found: {json_dumps(quote_json)}")

        print("[5/6] Confirm...")
        confirm_url, confirm_json, confirm_payload_used = try_confirm(
            commerce_url=args.commerce_url,
            access_token=access_token,
            user_id=user_id,
            quote_id=quote_id,
            timeout=300,
        )
        write_json(out_dir / "confirm.json", confirm_json)
        (out_dir / "confirm_payload_used.json").write_text(
            confirm_payload_used + "\n", encoding="utf-8"
        )
        print(f"  confirm endpoint={confirm_url}")

        job_id = extract_job_id(confirm_json)
        if not job_id:
            raise RuntimeError(
                f"Confirm succeeded but no job_id found: {json_dumps(confirm_json)}"
            )

    print(f"[6/6] Poll job... job_id={job_id}")
    status_json = poll_job(
        commerce_url=args.commerce_url,
        access_token=access_token,
        user_id=user_id,
        job_id=job_id,
        poll_interval_sec=args.poll_interval_sec,
        timeout_sec=args.timeout_sec,
        out_dir=out_dir,
    )
    write_json(out_dir / "status_final.json", status_json)

    final_status = extract_status(status_json)
    urls = collect_http_urls(status_json)

    summary = {
        "out_dir": str(out_dir),
        "user_id": user_id,
        "garment_type": args.garment_type,
        "mode": args.mode,
        "garment_asset_ids": garment_asset_ids,
        "model_asset_id": model_asset_id,
        "job_id": job_id,
        "final_status": final_status,
        "final_urls": urls,
    }
    write_json(out_dir / "summary.json", summary)

    print("")
    print("===== SUMMARY =====")
    print(f"job_id={job_id}")
    print(f"final_status={final_status}")
    print(f"summary_json={out_dir / 'summary.json'}")

    if urls:
        print("final_urls:")
        for url in urls[:12]:
            print(f" - {url}")
    else:
        print("final_urls: none found in status payload")

    if not status_is_success(final_status):
        raise RuntimeError(f"VTON job did not succeed. Final status={final_status}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        eprint(f"ERROR: {exc}")
        raise SystemExit(1)