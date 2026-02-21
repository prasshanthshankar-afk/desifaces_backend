# services/svc-commerce/app/app/scripts/e2e/df_e2e_commerce_vton.py
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return (v if v is not None else default).strip()


def _env_int(name: str, default: int) -> int:
    v = _env_str(name, "")
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    v = _env_str(name, "")
    if not v:
        return default
    try:
        return float(v)
    except Exception:
        return default


def _as_dict(x: Any) -> Dict[str, Any]:
    if isinstance(x, dict):
        return x
    if x is None:
        return {}
    if isinstance(x, (bytes, bytearray)):
        try:
            x = x.decode("utf-8", errors="ignore")
        except Exception:
            return {}
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


def _walk(obj: Any):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield ("key", k)
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    elif isinstance(obj, str):
        yield ("str", obj)


def _find_uuid_by_key(obj: Any, wanted_keys: List[str]) -> Optional[str]:
    if not isinstance(obj, dict):
        return None
    wset = {k.lower() for k in wanted_keys}
    for k, v in obj.items():
        if k.lower() in wset:
            if isinstance(v, str) and UUID_RE.search(v):
                return UUID_RE.search(v).group(0)
            if isinstance(v, dict):
                inner = _find_uuid_by_key(v, wanted_keys)
                if inner:
                    return inner

    for _, v in obj.items():
        if isinstance(v, dict):
            inner = _find_uuid_by_key(v, wanted_keys)
            if inner:
                return inner
        if isinstance(v, list):
            for item in v:
                inner = _find_uuid_by_key(item, wanted_keys)
                if inner:
                    return inner
    return None


def _find_any_uuid(obj: Any) -> Optional[str]:
    for typ, val in _walk(obj):
        if typ == "str":
            m = UUID_RE.search(val)
            if m:
                return m.group(0)
    return None


def _jwt_sub(token: str) -> Optional[str]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        pad = "=" * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
        j = json.loads(raw.decode("utf-8", errors="ignore"))
        sub = j.get("sub")
        return str(sub) if sub else None
    except Exception:
        return None


def _http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[Dict[str, Any]] = None,
    timeout_s: int = 60,
) -> Tuple[int, Dict[str, str], Any]:
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    req = Request(url=url, method=method.upper(), headers=hdrs, data=body)
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            code = getattr(resp, "status", 200)
            rh = dict(resp.headers.items())
            raw = resp.read() or b""
            txt = raw.decode("utf-8", errors="ignore")
            try:
                return code, rh, json.loads(txt) if txt else {}
            except Exception:
                return code, rh, txt
    except HTTPError as e:
        raw = b""
        try:
            raw = e.read() or b""
        except Exception:
            pass
        txt = raw.decode("utf-8", errors="ignore") if raw else str(e)
        try:
            return e.code, dict(e.headers.items()) if e.headers else {}, json.loads(txt) if txt else {}
        except Exception:
            return e.code, dict(e.headers.items()) if e.headers else {}, txt
    except URLError as e:
        raise RuntimeError(f"HTTP request failed: {url} err={e}") from e


def _http_head(url: str, *, headers: Optional[Dict[str, str]] = None, timeout_s: int = 25) -> int:
    hdrs = {"Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    req = Request(url=url, method="HEAD", headers=hdrs)
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            return int(getattr(resp, "status", 200))
    except HTTPError as e:
        return int(e.code)
    except Exception:
        return 0


def _pick_paths_from_openapi(openapi: Dict[str, Any]) -> Dict[str, str]:
    paths = _as_dict(openapi.get("paths"))
    candidates: List[Tuple[str, str]] = []

    for p, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for m, _spec in ops.items():
            if m.lower() not in ("get", "post", "put", "patch"):
                continue
            candidates.append((m.upper(), p))

    def best(method: str, contains_any: List[str], must_have_param: bool = False) -> Optional[str]:
        scored: List[Tuple[int, str]] = []
        for m, p in candidates:
            if m != method:
                continue
            lp = p.lower()
            score = 0
            for t in contains_any:
                if t in lp:
                    score += 2
            if lp.startswith("/api/"):
                score += 1
            if "commerce" in lp:
                score += 1
            if must_have_param and ("{" not in p or "}" not in p):
                continue
            if score > 0:
                scored.append((score, p))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1] if scored else None

    out: Dict[str, str] = {}
    for k in ("QUOTE_PATH", "CONFIRM_PATH", "STATUS_PATH", "OPENAPI_PATH"):
        v = _env_str(k, "")
        if v:
            out[k.lower()] = v

    if "quote_path" not in out:
        out["quote_path"] = best("POST", ["quote"]) or best("POST", ["quotes"]) or "/api/commerce/quote"
    if "confirm_path" not in out:
        out["confirm_path"] = best("POST", ["confirm"]) or best("POST", ["generate"]) or "/api/commerce/confirm"
    if "status_path" not in out:
        out["status_path"] = (
            best("GET", ["jobs", "status"], must_have_param=True)
            or best("GET", ["job", "status"], must_have_param=True)
            or best("GET", ["jobs"], must_have_param=True)
            or "/api/commerce/jobs/{job_id}/status"
        )

    return out


def _format_path(path_template: str, job_id: str) -> str:
    if "{" in path_template and "}" in path_template:
        return re.sub(r"\{[^}]+\}", job_id, path_template)
    return path_template.rstrip("/") + f"/{job_id}"


@dataclass
class AuthCtx:
    token: str
    user_id: str
    raw: Dict[str, Any]


def _login(core_url: str, email: str, password: str) -> AuthCtx:
    url = core_url.rstrip("/") + "/api/auth/login"
    payload = {"email": email, "password": password, "device_id": "e2e", "client_type": "ios"}
    code, _, out = _http_json("POST", url, data=payload, timeout_s=60)
    if code >= 300:
        raise RuntimeError(f"Login failed: code={code} out={out}")

    j = out if isinstance(out, dict) else _as_dict(out)
    token = j.get("access_token") or j.get("token") or j.get("jwt") or j.get("bearer")
    if not token or not isinstance(token, str):
        raise RuntimeError(f"Login response missing token fields. Got keys={list(j.keys())}")

    user_id = j.get("user_id") or j.get("x_user_id")
    if not user_id and isinstance(j.get("user"), dict):
        user_id = j["user"].get("id")
    if not user_id:
        user_id = _jwt_sub(token)
    if not user_id:
        raise RuntimeError("Could not determine user_id from login response or JWT sub")

    return AuthCtx(token=token, user_id=str(user_id), raw=j)


def _auth_headers(ctx: AuthCtx) -> Dict[str, str]:
    return {"Authorization": f"Bearer {ctx.token}", "X-User-Id": ctx.user_id}


def _build_quote_request(*, num_images: int, resolution: str, human_url: str, garment_url: str, cloth_type: str) -> Dict[str, Any]:
    # Keep this as the "request" object your service expects.
    return {
        "count": num_images,
        "outputs": {"num_images": num_images},
        "language": "en",
        "resolution": resolution,  # we also send resolution at top-level
        "product_assets": {
            "product_type": "apparel",
            "garment_image_url": garment_url,
            "cloth_type": cloth_type,
        },
        "model_ref": {"human_image_url": human_url},
    }


def _try_post_variants(
    commerce_url: str,
    path: str,
    headers: Dict[str, str],
    bodies: List[Dict[str, Any]],
    timeout_s: int = 60,
) -> Tuple[int, Any, Dict[str, Any]]:
    url = commerce_url.rstrip("/") + path
    last_code = 0
    last_out: Any = None
    for b in bodies:
        code, _, out = _http_json("POST", url, headers=headers, data=b, timeout_s=timeout_s)
        last_code, last_out = code, out
        if 200 <= code < 300:
            return code, out, b
    raise RuntimeError(f"POST failed for {url}. last_code={last_code} last_out={last_out}")


def _poll_status(
    commerce_url: str,
    status_path_tmpl: str,
    headers: Dict[str, str],
    job_id: str,
    *,
    timeout_s: int = 420,
    interval_s: float = 2.0,
) -> Dict[str, Any]:
    start = time.time()
    path = _format_path(status_path_tmpl, job_id)
    url = commerce_url.rstrip("/") + path

    while True:
        code, _, out = _http_json("GET", url, headers=headers, data=None, timeout_s=60)

        stage = None
        status = None
        if isinstance(out, dict):
            status = out.get("status") or out.get("job_status")
            computed = out.get("computed") if isinstance(out.get("computed"), dict) else None
            if computed:
                stage = computed.get("stage")
            payload_json = out.get("payload_json")
            if not stage and isinstance(payload_json, dict):
                c = payload_json.get("computed") if isinstance(payload_json.get("computed"), dict) else None
                if c:
                    stage = c.get("stage")

        elapsed = int(time.time() - start)
        print(f"[{datetime.now(timezone.utc).isoformat()}] job={job_id} elapsed_s={elapsed} stage={stage} status={status}")

        if stage in ("succeeded", "failed") or str(status).lower() in ("succeeded", "failed"):
            return out if isinstance(out, dict) else {"raw": out, "code": code}

        if time.time() - start > timeout_s:
            raise RuntimeError(f"Timed out polling status after {timeout_s}s. last_code={code} last_out={out}")

        time.sleep(max(0.25, interval_s))
        interval_s = min(10.0, interval_s * 1.15)


def main() -> int:
    core_url = _env_str("CORE_URL", "http://localhost:8000").rstrip("/")
    commerce_url = _env_str("COMMERCE_URL", "http://localhost:8008").rstrip("/")

    email = _env_str("DF_EMAIL", "user2@desifaces.ai")
    password = _env_str("DF_PASSWORD", "password2")

    # ✅ FIX: match your svc-commerce enums (from the 422)
    mode = _env_str("MODE", "platform_models")  # platform_models | customer_tryon
    product_type = _env_str("PRODUCT_TYPE", "apparel")
    resolution = _env_str("RESOLUTION", "hd")  # hd | hi_res

    human_url = _env_str("HUMAN_IMAGE_URL", "https://storage.googleapis.com/falserverless/catvton/man5.jpg")
    garment_url = _env_str("GARMENT_IMAGE_URL", "https://storage.googleapis.com/falserverless/catvton/tshirt.jpg")
    cloth_type = _env_str("CLOTH_TYPE", "upper")

    num_images = max(1, min(20, _env_int("NUM_IMAGES", 4)))
    timeout_s = _env_int("TIMEOUT_S", 420)
    poll_interval_s = _env_float("POLL_SECS", 2.0)

    run_dir = Path(_env_str("RUN_DIR", f"/tmp/df_e2e_commerce_vton_{_utc_ts()}"))
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"✅ E2E starting. Run dir: {run_dir}")
    print(f"CORE_URL={core_url}")
    print(f"COMMERCE_URL={commerce_url}")
    print(f"MODE={mode} PRODUCT_TYPE={product_type} RESOLUTION={resolution} NUM_IMAGES={num_images} CLOTH_TYPE={cloth_type}")

    ctx = _login(core_url, email, password)
    (run_dir / "auth.json").write_text(json.dumps(ctx.raw, indent=2), encoding="utf-8")
    print(f"✅ Auth OK. X-User-Id={ctx.user_id}")

    headers = _auth_headers(ctx)

    openapi_path = _env_str("OPENAPI_PATH", "/openapi.json")
    code, _, openapi = _http_json("GET", commerce_url + openapi_path, headers=headers, data=None, timeout_s=60)
    if code >= 300 or not isinstance(openapi, dict):
        raise RuntimeError(f"Failed to fetch openapi.json from svc-commerce: code={code} out={openapi}")
    (run_dir / "svc-commerce_openapi.json").write_text(json.dumps(openapi, indent=2), encoding="utf-8")

    paths = _pick_paths_from_openapi(openapi)
    quote_path = paths["quote_path"]
    confirm_path = paths["confirm_path"]
    status_path = paths["status_path"]

    print(f"🔎 Using QUOTE_PATH={quote_path}")
    print(f"🔎 Using CONFIRM_PATH={confirm_path}")
    print(f"🔎 Using STATUS_PATH={status_path}")

    quote_request = _build_quote_request(
        num_images=num_images,
        resolution=resolution,
        human_url=human_url,
        garment_url=garment_url,
        cloth_type=cloth_type,
    )

    # ✅ FIX: send resolution at TOP-LEVEL too (your API validates it there)
    quote_bodies = [
        {"mode": mode, "product_type": product_type, "resolution": resolution, "request": quote_request},
        {"mode": mode, "product_type": product_type, "resolution": resolution, "quote_request": quote_request},
        {"mode": mode, "product_type": product_type, "resolution": resolution, "input": quote_request},
        {"mode": mode, "product_type": product_type, "resolution": resolution, **quote_request},
    ]

    code, quote_out, quote_body_used = _try_post_variants(commerce_url, quote_path, headers, quote_bodies, timeout_s=60)
    (run_dir / "quote_request_used.json").write_text(json.dumps(quote_body_used, indent=2), encoding="utf-8")
    (run_dir / "quote_response.json").write_text(
        json.dumps(quote_out, indent=2) if isinstance(quote_out, (dict, list)) else str(quote_out),
        encoding="utf-8",
    )
    print(f"✅ Quote created. HTTP {code}")

    quote_id = _find_uuid_by_key(quote_out, ["quote_id", "id"]) if isinstance(quote_out, dict) else None
    if not quote_id:
        quote_id = _find_any_uuid(quote_out)
    if not quote_id:
        raise RuntimeError("Could not find quote_id in quote response. See quote_response.json")
    print(f"✅ quote_id={quote_id}")

    # Confirm: keep trying with and without top-level enum fields (some schemas require them here too).
    confirm_bodies = [
        {"quote_id": quote_id, "mode": mode, "product_type": product_type, "resolution": resolution, "quote_request": quote_request},
        {"quote_id": quote_id, "mode": mode, "product_type": product_type, "resolution": resolution, "request": quote_request},
        {"quote_id": quote_id, "mode": mode, "product_type": product_type, "resolution": resolution},
        {"quote_id": quote_id, "quote_request": quote_request},
        {"quote_id": quote_id},
        {"input": {"quote_id": quote_id}, "quote_request": quote_request},
        {"input": {"quote_id": quote_id}},
    ]

    code, confirm_out, confirm_body_used = _try_post_variants(commerce_url, confirm_path, headers, confirm_bodies, timeout_s=60)
    (run_dir / "confirm_request_used.json").write_text(json.dumps(confirm_body_used, indent=2), encoding="utf-8")
    (run_dir / "confirm_response.json").write_text(
        json.dumps(confirm_out, indent=2) if isinstance(confirm_out, (dict, list)) else str(confirm_out),
        encoding="utf-8",
    )
    print(f"✅ Confirm OK. HTTP {code}")

    job_id = None
    campaign_id = None
    if isinstance(confirm_out, dict):
        job_id = _find_uuid_by_key(confirm_out, ["job_id", "studio_job_id", "id"])
        campaign_id = _find_uuid_by_key(confirm_out, ["commerce_campaign_id", "campaign_id"])
    if not job_id:
        job_id = _find_any_uuid(confirm_out)
    if not job_id:
        raise RuntimeError("Could not find job_id/studio_job_id in confirm response. See confirm_response.json")

    print(f"✅ job_id={job_id}")
    if campaign_id:
        print(f"ℹ️ campaign_id={campaign_id}")

    status_out = _poll_status(
        commerce_url,
        status_path,
        headers,
        job_id,
        timeout_s=timeout_s,
        interval_s=poll_interval_s,
    )
    (run_dir / "status_last.json").write_text(json.dumps(status_out, indent=2), encoding="utf-8")

    computed: Dict[str, Any] = {}
    if isinstance(status_out, dict) and isinstance(status_out.get("computed"), dict):
        computed = status_out["computed"]
    elif isinstance(status_out, dict) and isinstance(status_out.get("payload_json"), dict):
        pj = status_out["payload_json"]
        if isinstance(pj.get("computed"), dict):
            computed = pj["computed"]

    stage = str(computed.get("stage") or "").strip()
    provider = str(computed.get("provider") or "").strip()
    variant_count = computed.get("variant_count")
    urls_raw = computed.get("urls")
    urls: List[str] = [u for u in urls_raw if isinstance(u, str) and u.strip()] if isinstance(urls_raw, list) else []

    provider_meta = computed.get("provider_meta") if isinstance(computed.get("provider_meta"), dict) else {}
    provider_images = provider_meta.get("provider_images")
    fal_count = sum(1 for u in urls if "fal.media" in u)

    head_code = _http_head(urls[0], timeout_s=25) if urls else 0

    ok = True
    problems: List[str] = []

    if stage != "succeeded":
        ok = False
        problems.append(f"expected stage=succeeded, got stage={stage!r}")

    if not urls:
        ok = False
        problems.append("expected non-empty computed.urls")

    # Optional strong assertions when real providers are on
    if _env_str("EXPECT_PROVIDER", "fal").strip():
        expected_provider = _env_str("EXPECT_PROVIDER", "fal").strip()
        if expected_provider and provider and provider != expected_provider:
            ok = False
            problems.append(f"expected provider={expected_provider!r}, got provider={provider!r}")

    if urls and head_code not in (200, 301, 302, 307, 308):
        ok = False
        problems.append(f"HEAD first_url returned {head_code}")

    summary = {
        "core_url": core_url,
        "commerce_url": commerce_url,
        "quote_path": quote_path,
        "confirm_path": confirm_path,
        "status_path": status_path,
        "mode": mode,
        "product_type": product_type,
        "resolution": resolution,
        "email": email,
        "user_id": ctx.user_id,
        "quote_id": quote_id,
        "job_id": job_id,
        "campaign_id": campaign_id,
        "stage": stage,
        "provider": provider,
        "variant_count": variant_count,
        "provider_images": provider_images,
        "num_images_expected": num_images,
        "url_count": len(urls),
        "fal_url_count": fal_count,
        "first_url": urls[0] if urls else None,
        "first_url_head_status": head_code,
        "provider_meta": provider_meta,
        "ok": ok,
        "problems": problems,
        "run_dir": str(run_dir),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if ok:
        print("✅ E2E PASS")
        print(json.dumps(summary, indent=2))
        return 0

    print("❌ E2E FAIL")
    print(json.dumps(summary, indent=2))
    print(f"See artifacts in: {run_dir}")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"❌ E2E crashed: {type(e).__name__}: {e}")
        sys.exit(3)