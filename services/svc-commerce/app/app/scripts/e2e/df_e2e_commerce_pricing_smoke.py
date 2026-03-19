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


DEFAULT_USERS = [
    {"label": "prepaid", "email": "user1@desifaces.ai", "password": "password1"},
    {"label": "postpaid", "email": "user2@desifaces.ai", "password": "password2"},
]


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

    if not data.get("asset_id"):
        raise RuntimeError(f"Upload succeeded but no asset_id returned: {json_dumps(data)}")
    return data


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


def extract_quote_id(obj: Dict[str, Any]) -> Optional[str]:
    candidates = [
        obj.get("quote_id"),
        obj.get("id"),
        (obj.get("quote") or {}).get("id") if isinstance(obj.get("quote"), dict) else None,
        (obj.get("data") or {}).get("quote_id") if isinstance(obj.get("data"), dict) else None,
    ]
    for value in candidates:
        if value:
            return str(value)
    return None


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


def collect_http_urls(obj: Any, found: Optional[List[str]] = None) -> List[str]:
    if found is None:
        found = []
    if isinstance(obj, dict):
        for v in obj.values():
            collect_http_urls(v, found)
    elif isinstance(obj, list):
        for v in obj:
            collect_http_urls(v, found)
    elif isinstance(obj, str):
        if obj.startswith("http://") or obj.startswith("https://"):
            if obj not in found:
                found.append(obj)
    return found


def extract_pricing_block(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        if isinstance(obj.get("pricing"), dict):
            return obj["pricing"]
        if isinstance(obj.get("payload_json"), dict) and isinstance(obj["payload_json"].get("pricing"), dict):
            return obj["payload_json"]["pricing"]
        if isinstance(obj.get("meta_json"), dict) and isinstance(obj["meta_json"].get("pricing"), dict):
            return obj["meta_json"]["pricing"]
        if isinstance(obj.get("data"), dict):
            nested = extract_pricing_block(obj["data"])
            if nested:
                return nested
    return {}


def build_one_shot_payload(
    *,
    garment_uploads: Sequence[Dict[str, Any]],
    garment_type: str,
    mode: str,
    variants: int,
    model_upload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    api_mode = "platform_models" if mode == "vendor_only" else "customer_tryon"

    items: List[Dict[str, Any]] = []
    for idx, uploaded in enumerate(garment_uploads, start=1):
        asset_id = str(uploaded.get("asset_id") or "")
        preview_url = str(uploaded.get("preview_url") or "")
        storage_ref = str(uploaded.get("storage_ref") or "")
        items.append(
            {
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
        )

    payload: Dict[str, Any] = {
        "mode": api_mode,
        "product_type": "apparel",
        "variant_count": variants,
        "product_assets": {
            "items": items,
            "primary": items[0],
        },
        "garment_type": garment_type,
        "garment_image_url": items[0]["garment_image_url"],
    }

    if model_upload and api_mode == "customer_tryon":
        payload["person_asset_id"] = str(model_upload.get("asset_id") or "")
        payload["person_image_url"] = str(model_upload.get("preview_url") or "")
        payload["model_image_url"] = str(model_upload.get("preview_url") or "")
        payload["model_ref"] = {
            "asset_id": str(model_upload.get("asset_id") or ""),
            "human_image_url": str(model_upload.get("preview_url") or ""),
        }

    return payload


def build_quote_payload(
    *,
    garment_uploads: Sequence[Dict[str, Any]],
    garment_type: str,
    mode: str,
    variants: int,
    model_upload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    return build_one_shot_payload(
        garment_uploads=garment_uploads,
        garment_type=garment_type,
        mode=mode,
        variants=variants,
        model_upload=model_upload,
    )


def try_one_shot_generate(
    *,
    commerce_url: str,
    access_token: str,
    user_id: str,
    payload: Dict[str, Any],
    timeout: int,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    endpoints = [
        "/api/commerce/generate",
        "/api/commerce/v1/jobs",
    ]
    attempts: List[Dict[str, Any]] = []

    for path in endpoints:
        url = f"{commerce_url.rstrip('/')}{path}"
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

        attempts.append({"url": url, "status": status, "response": data})

    return None, {"attempts": attempts}, None


def do_quote_confirm(
    *,
    commerce_url: str,
    access_token: str,
    user_id: str,
    quote_payload: Dict[str, Any],
    timeout: int,
) -> Tuple[Dict[str, Any], Dict[str, Any], str, str]:
    quote_url = f"{commerce_url.rstrip('/')}/api/commerce/quote"
    quote_status, _, quote_json = http_request(
        "POST",
        quote_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-User-Id": user_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(quote_payload).encode("utf-8"),
        timeout=timeout,
    )
    if not (200 <= quote_status < 300):
        raise RuntimeError(f"Quote failed ({quote_status}): {json_dumps(quote_json)}")

    quote_id = extract_quote_id(quote_json)
    if not quote_id:
        raise RuntimeError(f"Quote succeeded but no quote_id found: {json_dumps(quote_json)}")

    confirm_payload = {"quote_id": quote_id}
    confirm_url = f"{commerce_url.rstrip('/')}/api/commerce/confirm"
    confirm_status, _, confirm_json = http_request(
        "POST",
        confirm_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-User-Id": user_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(confirm_payload).encode("utf-8"),
        timeout=timeout,
    )
    if not (200 <= confirm_status < 300):
        raise RuntimeError(f"Confirm failed ({confirm_status}): {json_dumps(confirm_json)}")

    return quote_json, confirm_json, json_dumps(quote_payload), json_dumps(confirm_payload)


def try_status_once(
    *,
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


def poll_job(
    *,
    commerce_url: str,
    access_token: str,
    user_id: str,
    job_id: str,
    poll_interval_sec: int,
    timeout_sec: int,
    out_dir: Path,
    label: str,
) -> Dict[str, Any]:
    started = time.time()
    last_data: Dict[str, Any] = {}
    saw_reserved = False

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
        pricing = extract_pricing_block(last_data)
        pricing_state = str(pricing.get("state") or pricing.get("reservation_status") or "").strip().lower()
        elapsed = int(time.time() - started)

        if pricing_state in {"reserved", "committed"}:
            saw_reserved = True

        snap = {
            "elapsed_sec": elapsed,
            "http_code": http_code,
            "status_url": url,
            "status": status,
            "pricing_state": pricing_state,
            "payload": last_data,
        }
        write_json(out_dir / f"{label}_status_latest.json", snap)
        print(f"[{label}] poll elapsed={elapsed}s status={status} pricing={pricing_state or 'n/a'}")

        if http_code >= 200 and http_code < 300 and is_terminal(status):
            last_data["_saw_reserved_before_terminal"] = saw_reserved
            return last_data

        if time.time() - started >= timeout_sec:
            raise RuntimeError(
                f"[{label}] Polling timed out after {timeout_sec}s. Last response:\n{json_dumps(last_data)}"
            )

        time.sleep(poll_interval_sec)


def validate_pricing_expectations(result: Dict[str, Any], label: str) -> Dict[str, Any]:
    final_status = extract_status(result)
    pricing = extract_pricing_block(result)
    pricing_state = str(pricing.get("state") or pricing.get("reservation_status") or "").strip().lower()
    saw_reserved = bool(result.get("_saw_reserved_before_terminal"))

    checks = {
        "final_status_success": status_is_success(final_status),
        "saw_reserved_before_terminal": saw_reserved,
        "final_pricing_committed": pricing_state == "committed",
        "has_pricing_block": bool(pricing),
    }

    if not checks["final_status_success"]:
        raise RuntimeError(f"[{label}] final job status not succeeded: {final_status}")
    if not checks["has_pricing_block"]:
        raise RuntimeError(f"[{label}] missing pricing block in final status payload")
    if not checks["saw_reserved_before_terminal"]:
        raise RuntimeError(f"[{label}] pricing never appeared reserved/committed during polling")
    if not checks["final_pricing_committed"]:
        raise RuntimeError(
            f"[{label}] final pricing state is not committed; got={pricing_state or 'empty'}"
        )

    return checks


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="End-to-end commerce pricing smoke test for prepaid + postpaid users."
    )
    ap.add_argument("--core-url", default=os.environ.get("CORE_URL", "http://localhost:8000"))
    ap.add_argument("--commerce-url", default=os.environ.get("COMMERCE_URL", "http://localhost:8008"))
    ap.add_argument("--garment-file", required=True, help="Path to garment image")
    ap.add_argument("--garment-type", required=True, help="e.g. salwar_suit, saree_set, shirt")
    ap.add_argument("--person-file", default=None, help="Optional model image for user_model flow")
    ap.add_argument("--mode", default="vendor_only", choices=["vendor_only", "user_model"])
    ap.add_argument("--variants", type=int, default=4)
    ap.add_argument("--poll-interval-sec", type=int, default=10)
    ap.add_argument("--timeout-sec", type=int, default=1800)
    ap.add_argument("--out-dir", default=f"/tmp/df_commerce_pricing_smoke_{now_ts()}")
    return ap.parse_args()


def run_for_user(
    *,
    core_url: str,
    commerce_url: str,
    garment_file: Path,
    garment_type: str,
    person_file: Optional[Path],
    mode: str,
    variants: int,
    poll_interval_sec: int,
    timeout_sec: int,
    out_dir: Path,
    user_conf: Dict[str, str],
) -> Dict[str, Any]:
    label = str(user_conf["label"])
    email = str(user_conf["email"])
    password = str(user_conf["password"])
    user_dir = out_dir / label
    user_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== {label.upper()} :: {email} ===")
    auth = login(core_url, email, password, timeout=120)
    access_token = auth["access_token"]
    user_id = auth["user_id"]
    write_json(user_dir / "auth.json", auth["raw"])

    garment_upload = upload_asset(
        commerce_url=commerce_url,
        access_token=access_token,
        user_id=user_id,
        role="garment",
        file_path=garment_file,
        timeout=300,
    )
    write_json(user_dir / "upload_garment.json", garment_upload)

    model_upload = None
    if mode == "user_model":
        if not person_file:
            raise RuntimeError("--mode user_model requires --person-file")
        model_upload = upload_asset(
            commerce_url=commerce_url,
            access_token=access_token,
            user_id=user_id,
            role="model",
            file_path=person_file,
            timeout=300,
        )
        write_json(user_dir / "upload_model.json", model_upload)

    one_shot_payload = build_one_shot_payload(
        garment_uploads=[garment_upload],
        garment_type=garment_type,
        mode=mode,
        variants=variants,
        model_upload=model_upload,
    )
    quote_payload = build_quote_payload(
        garment_uploads=[garment_upload],
        garment_type=garment_type,
        mode=mode,
        variants=variants,
        model_upload=model_upload,
    )

    generate_url, generate_json, generate_payload_used = try_one_shot_generate(
        commerce_url=commerce_url,
        access_token=access_token,
        user_id=user_id,
        payload=one_shot_payload,
        timeout=300,
    )

    quote_json: Dict[str, Any] = {}
    confirm_json: Dict[str, Any] = {}
    path_used = ""
    job_source = ""

    if generate_url and isinstance(generate_json, dict) and extract_job_id(generate_json):
        path_used = generate_url
        job_source = "one_shot"
        write_json(user_dir / "generate.json", generate_json)
        (user_dir / "generate_payload_used.json").write_text(
            generate_payload_used + "\n", encoding="utf-8"
        )
        job_id = extract_job_id(generate_json)
    else:
        if generate_json:
            write_json(user_dir / "generate_attempts.json", generate_json)

        quote_json, confirm_json, quote_payload_used, confirm_payload_used = do_quote_confirm(
            commerce_url=commerce_url,
            access_token=access_token,
            user_id=user_id,
            quote_payload=quote_payload,
            timeout=300,
        )
        write_json(user_dir / "quote.json", quote_json)
        write_json(user_dir / "confirm.json", confirm_json)
        (user_dir / "quote_payload_used.json").write_text(quote_payload_used + "\n", encoding="utf-8")
        (user_dir / "confirm_payload_used.json").write_text(confirm_payload_used + "\n", encoding="utf-8")
        path_used = "/api/commerce/quote -> /api/commerce/confirm"
        job_source = "quote_confirm"
        job_id = extract_job_id(confirm_json) or extract_job_id(quote_json)

    if not job_id:
        raise RuntimeError(f"[{label}] unable to extract job_id from generate/confirm response")

    final_status_json = poll_job(
        commerce_url=commerce_url,
        access_token=access_token,
        user_id=user_id,
        job_id=job_id,
        poll_interval_sec=poll_interval_sec,
        timeout_sec=timeout_sec,
        out_dir=user_dir,
        label=label,
    )
    write_json(user_dir / "status_final.json", final_status_json)

    checks = validate_pricing_expectations(final_status_json, label=label)

    pricing = extract_pricing_block(final_status_json)
    urls = collect_http_urls(final_status_json)
    result = {
        "label": label,
        "email": email,
        "user_id": user_id,
        "job_source": job_source,
        "path_used": path_used,
        "job_id": job_id,
        "final_status": extract_status(final_status_json),
        "pricing_state": pricing.get("state") or pricing.get("reservation_status"),
        "pricing": pricing,
        "checks": checks,
        "final_urls": urls,
    }
    write_json(user_dir / "result.json", result)
    return result


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    garment_file = Path(args.garment_file).expanduser().resolve()
    if not garment_file.exists():
        raise RuntimeError(f"Garment file not found: {garment_file}")

    person_file = Path(args.person_file).expanduser().resolve() if args.person_file else None
    if args.mode == "user_model" and (not person_file or not person_file.exists()):
        raise RuntimeError(f"Person file not found: {person_file}")

    summary = {
        "out_dir": str(out_dir),
        "core_url": args.core_url,
        "commerce_url": args.commerce_url,
        "garment_file": str(garment_file),
        "garment_type": args.garment_type,
        "mode": args.mode,
        "variants": args.variants,
        "results": [],
    }

    failures: List[str] = []
    for user_conf in DEFAULT_USERS:
        try:
            result = run_for_user(
                core_url=args.core_url,
                commerce_url=args.commerce_url,
                garment_file=garment_file,
                garment_type=args.garment_type,
                person_file=person_file,
                mode=args.mode,
                variants=args.variants,
                poll_interval_sec=args.poll_interval_sec,
                timeout_sec=args.timeout_sec,
                out_dir=out_dir,
                user_conf=user_conf,
            )
            summary["results"].append(result)
        except Exception as exc:
            failure = {
                "label": user_conf["label"],
                "email": user_conf["email"],
                "error": str(exc),
            }
            summary["results"].append(failure)
            failures.append(f"{user_conf['label']}: {exc}")

    write_json(out_dir / "summary.json", summary)

    print("\n===== COMMERCE PRICING SMOKE SUMMARY =====")
    print(f"summary_json={out_dir / 'summary.json'}")
    for item in summary["results"]:
        label = item.get("label")
        if item.get("error"):
            print(f" - {label}: FAILED :: {item['error']}")
        else:
            print(
                f" - {label}: OK :: job_id={item.get('job_id')} "
                f"status={item.get('final_status')} pricing={item.get('pricing_state')}"
            )

    if failures:
        raise RuntimeError(" ; ".join(failures))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        eprint(f"ERROR: {exc}")
        raise SystemExit(1)