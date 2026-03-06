# services/svc-commerce/app/app/services/providers/saree_drape_provider.py
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Helpers (local, robust)
# -----------------------------------------------------------------------------


def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    try:
        return dict(x)
    except Exception:
        return {}


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    v = (os.getenv(name) or "").strip()
    if not v:
        return default
    try:
        return int(float(v))
    except Exception:
        return default


def _coerce_float(v: Any, default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        try:
            return float(str(v))
        except Exception:
            return default


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _stable_seed(*parts: str) -> int:
    h = _sha256("|".join([p or "" for p in parts]))
    return int(h[:8], 16) & 0x7FFFFFFF


def _stable_seed_hex16(*parts: str) -> str:
    h = _sha256("|".join([p or "" for p in parts]))
    return h[:16]


def _safe_filename(ext: str = ".png") -> str:
    ext = ext if ext.startswith(".") else f".{ext}"
    return f"{uuid4().hex}{ext}"


def _is_http_url(x: Any) -> bool:
    return isinstance(x, str) and x.strip().startswith(("http://", "https://"))


def _ext_from_url(url: str, default_ext: str = ".png") -> str:
    try:
        p = urlparse(url).path or ""
        _, ext = os.path.splitext(p.lower())
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".blend"):
            return ext
    except Exception:
        pass
    return default_ext


def _download_to_path(url: str, out_path: str, *, timeout_s: int, max_bytes: int) -> None:
    if not _is_http_url(url):
        raise ValueError(f"download url must be http(s): {url!r}")
    req = Request(url, headers={"User-Agent": "desifaces-svc-commerce/1.0"})
    total = 0
    with urlopen(req, timeout=timeout_s) as resp:
        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(f"download exceeded max_bytes={max_bytes} url={url}")
                f.write(chunk)


def _run_coro_sync(coro: Any) -> Any:
    """
    Run an awaitable from sync code. If we're already inside a running loop,
    create a private loop to run the coroutine.
    """
    try:
        loop = asyncio.get_running_loop()
        if loop and loop.is_running():
            new_loop = asyncio.new_event_loop()
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
    except RuntimeError:
        pass
    return asyncio.run(coro)


def _call_any_upload(storage: Any, local_path: str, blob_name: str, content_type: str) -> str:
    """
    AzureStorageService method-name tolerant upload.
    Expected to return a SAS URL or public URL string.
    """
    if not storage:
        raise RuntimeError("storage missing")

    candidates = ["upload_file", "upload_path", "upload_local_file", "upload"]
    last_err: Optional[Exception] = None

    for method_name in candidates:
        m = getattr(storage, method_name, None)
        if not callable(m):
            continue
        try:
            sig = None
            try:
                sig = inspect.signature(m)
            except Exception:
                sig = None

            if sig and "content_type" in sig.parameters:
                url = m(local_path, blob_name, content_type=content_type)
            elif sig and "mime_type" in sig.parameters:
                url = m(local_path, blob_name, mime_type=content_type)
            else:
                try:
                    url = m(local_path, blob_name, content_type=content_type)
                except TypeError:
                    url = m(local_path, blob_name)

            if inspect.isawaitable(url):
                url = _run_coro_sync(url)

            if isinstance(url, dict):
                url = url.get("url")

            if _is_http_url(url):
                return str(url).strip()
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"storage upload failed; last_err={last_err!r}")


def _write_json(path: str, obj: Any) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)
    except Exception:
        pass


def _sanitize_for_path(x: Any) -> str:
    s = str(x)
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:80] or "job"


_UUID_WITH_VARIANT_RE = re.compile(r"^([0-9a-fA-F-]{36})-(\d{1,3})$")


def _split_job_id(job_id: Any) -> Tuple[str, int]:
    """
    Supports common pattern: "<uuid>-<variant_idx>".
    Returns (base_job_id, variant_idx). If no suffix, variant_idx=0.
    """
    s = str(job_id or "").strip()
    m = _UUID_WITH_VARIANT_RE.match(s)
    if m:
        try:
            return m.group(1), int(m.group(2))
        except Exception:
            return m.group(1), 0
    return s, 0


def _infer_variant_idx_from_job_id(job_id: Any) -> int:
    return _split_job_id(job_id)[1]


# -----------------------------------------------------------------------------
# fal.ai queue runner (robust for subpaths + transient errors)
# -----------------------------------------------------------------------------


def _fal_key() -> str:
    return (os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY") or "").strip()


def _fal_headers() -> Dict[str, str]:
    key = _fal_key()
    if not key:
        raise RuntimeError("FAL_KEY missing (set it in df-svc-commerce-worker env)")
    return {"Authorization": f"Key {key}", "Content-Type": "application/json"}


def _fal_queue_run(model_id: str, inp: Dict[str, Any], *, timeout_s: int = 900, poll_s: int = 2) -> Dict[str, Any]:
    """
    Direct HTTP Queue runner.
    - Submit to:  POST https://queue.fal.run/<full model path>  with json=<input>
    - Status/Result endpoints sometimes drop subpaths; sometimes keep partial prefix.
      This runner tries multiple model prefixes derived from model_id to find working
      status/result endpoints.

    Also treats 429/500/502/503/504 as transient in polling.
    """
    headers = _fal_headers()

    parts = [p for p in (model_id or "").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"invalid fal model_id: {model_id!r}")

    # Submit must be full path exactly as model_id.
    submit_url = f"https://queue.fal.run/{model_id}"

    # For status/result, try multiple prefixes: "fal-ai/fashn", "fal-ai/fashn/tryon", ...
    prefixes: List[str] = []
    for i in range(2, len(parts) + 1):
        prefixes.append("/".join(parts[:i]))
    # prefer shorter prefixes first (they are typically the canonical queue model_id)
    prefixes = list(dict.fromkeys(prefixes))

    def _snip(r: httpx.Response, lim: int = 2000) -> str:
        try:
            return (r.text or "")[:lim]
        except Exception:
            return ""

    def _is_transient(code: int) -> bool:
        return code in (429, 500, 502, 503, 504)

    with httpx.Client(timeout=120) as client:
        sub = client.post(submit_url, headers=headers, json=inp)
        if sub.status_code >= 400:
            raise RuntimeError(f"fal submit failed status={sub.status_code} url={submit_url} body={_snip(sub)}")
        q = sub.json()

        request_id = q.get("request_id") or q.get("requestId") or q.get("id")
        if not request_id:
            raise RuntimeError(f"fal submit missing request_id: {q}")

        # Candidate status/result URLs
        status_candidates: List[str] = []
        result_candidates: List[str] = []

        # If fal returns explicit URLs, try them first.
        qs = q.get("status_url")
        qr = q.get("response_url")
        if isinstance(qs, str) and qs.strip():
            status_candidates.append(qs.strip())
        if isinstance(qr, str) and qr.strip():
            result_candidates.append(qr.strip())
            # often status is result_url + /status
            result_base = qr.strip().rstrip("/")
            status_candidates.append(result_base + "/status")

        # Derived from prefixes
        for pref in prefixes:
            base = f"https://queue.fal.run/{pref}"
            result_candidates.append(f"{base}/requests/{request_id}")
            status_candidates.append(f"{base}/requests/{request_id}/status")

        # uniq preserve order
        def _uniq(xs: List[str]) -> List[str]:
            out: List[str] = []
            seen = set()
            for x in xs:
                if x and x not in seen:
                    seen.add(x)
                    out.append(x)
            return out

        status_candidates = _uniq(status_candidates)
        result_candidates = _uniq(result_candidates)

        # Find a working status endpoint (first one that returns JSON and not 404/405/422)
        picked_status_url: Optional[str] = None
        last_status_obj: Optional[Dict[str, Any]] = None
        last_pick_err: Optional[str] = None

        for cand in status_candidates:
            try:
                r = client.get(cand, headers=headers, params={"logs": "1"})
                if r.status_code == 422:
                    r = client.get(cand, headers=headers)
                if r.status_code in (404, 405):
                    continue
                if r.status_code >= 400:
                    last_pick_err = f"status={r.status_code} url={cand} body={_snip(r)}"
                    continue
                js = r.json()
                if isinstance(js, dict) and js:
                    picked_status_url = cand
                    last_status_obj = js
                    break
            except Exception as e:
                last_pick_err = f"{type(e).__name__}: {e}"
                continue

        if not picked_status_url:
            raise RuntimeError(
                f"fal could not find working status_url model_id={model_id} request_id={request_id} "
                f"last_err={last_pick_err} candidates={status_candidates}"
            )

        # Poll status
        t0 = time.time()
        while True:
            r = client.get(picked_status_url, headers=headers, params={"logs": "1"})
            if r.status_code == 422:
                r = client.get(picked_status_url, headers=headers)

            if _is_transient(r.status_code):
                time.sleep(max(1, poll_s))
                if time.time() - t0 > timeout_s:
                    raise TimeoutError(f"fal status timeout (transient loop) model_id={model_id} request_id={request_id}")
                continue

            if r.status_code >= 400:
                raise RuntimeError(f"fal status failed status={r.status_code} url={picked_status_url} body={_snip(r)}")

            last_status_obj = r.json()
            status = (last_status_obj.get("status") or "").upper()

            # If status provides response_url, prefer it
            ru = last_status_obj.get("response_url")
            if isinstance(ru, str) and ru.strip():
                result_candidates = _uniq([ru.strip()] + result_candidates)

            if status == "COMPLETED":
                break
            if status in ("FAILED", "CANCELED", "CANCELLED"):
                raise RuntimeError(f"fal job failed model_id={model_id} request_id={request_id} status={last_status_obj}")
            if time.time() - t0 > timeout_s:
                raise TimeoutError(f"fal job timeout model_id={model_id} request_id={request_id} last={last_status_obj}")

            time.sleep(max(1, poll_s))

        # Fetch result (try candidates; retry transient codes a bit)
        picked_result_url: Optional[str] = None
        out_obj: Optional[Dict[str, Any]] = None
        last_res_err: Optional[str] = None

        for cand in result_candidates:
            tries = 0
            while True:
                tries += 1
                rr = client.get(cand, headers=headers)
                if _is_transient(rr.status_code) and tries <= 6:
                    time.sleep(min(10, max(1, poll_s) * tries))
                    continue
                if rr.status_code in (404, 405, 422):
                    last_res_err = f"status={rr.status_code} url={cand} body={_snip(rr)}"
                    break
                if rr.status_code >= 400:
                    last_res_err = f"status={rr.status_code} url={cand} body={_snip(rr)}"
                    break
                obj = rr.json()
                if isinstance(obj, dict) and obj:
                    picked_result_url = cand
                    out_obj = obj
                    break
                last_res_err = f"empty_json url={cand}"
                break
            if picked_result_url and out_obj is not None:
                break

        if not picked_result_url or out_obj is None:
            raise RuntimeError(
                f"fal could not fetch result model_id={model_id} request_id={request_id} "
                f"picked_status_url={picked_status_url} last_err={last_res_err} candidates={result_candidates}"
            )

        return {
            "_request_id": str(request_id),
            "_submit_url": submit_url,
            "_picked_status_url": picked_status_url,
            "_picked_result_url": picked_result_url,
            "_queue_status": last_status_obj,
            "output": out_obj,
        }


def _fal_out(resp: Dict[str, Any]) -> Dict[str, Any]:
    out = resp.get("output") if isinstance(resp, dict) else None
    return out if isinstance(out, dict) else {}


def _fal_extract_image_url_images(resp: Dict[str, Any]) -> str:
    out = _fal_out(resp)
    images = _as_list(out.get("images"))
    for im in images:
        u = _as_dict(im).get("url")
        if _is_http_url(u):
            return str(u).strip()
    raise RuntimeError(f"fal output missing images[].url keys: {list(out.keys())}")


def _fal_extract_image_url_leffa(resp: Dict[str, Any]) -> str:
    out = _fal_out(resp)
    img = _as_dict(out.get("image"))
    u = img.get("url")
    if _is_http_url(u):
        return str(u).strip()
    # some variants provide images[]
    return _fal_extract_image_url_images(resp)


# -----------------------------------------------------------------------------
# Caption QC (Moondream family) with fallback
# -----------------------------------------------------------------------------


def _caption_contains_saree(text: str) -> bool:
    t = (text or "").lower()
    keys = [
        "saree",
        "sari",
        "saari",
        "pallu",
        "pleat",
        "traditional indian",
        "indian saree",
        "indian sari",
    ]
    return any(k in t for k in keys)


def _fal_caption_any(image_url: str, *, model_ids: List[str], timeout_s: int, poll_s: int) -> Tuple[str, Dict[str, Any]]:
    """
    Returns (caption_text, debug_meta). Tries multiple caption models.
    """
    dbg: Dict[str, Any] = {"image_url": image_url, "attempts": []}
    last_err: Optional[str] = None

    for mid in model_ids:
        try:
            resp = _fal_queue_run(mid, {"image_url": image_url}, timeout_s=timeout_s, poll_s=poll_s)
            out = _fal_out(resp)
            # moondream2/moondream3/moondream-next return {"output": "..."}
            caption = out.get("output")
            if isinstance(caption, str) and caption.strip():
                dbg["attempts"].append({"model": mid, "request_id": resp.get("_request_id"), "ok": True})
                return caption.strip(), dbg
            dbg["attempts"].append({"model": mid, "request_id": resp.get("_request_id"), "ok": False, "reason": "empty_output"})
            last_err = "empty_output"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            dbg["attempts"].append({"model": mid, "ok": False, "error": last_err})
            continue

    raise RuntimeError(f"caption_failed last_err={last_err} dbg={dbg}")


# -----------------------------------------------------------------------------
# AZ ref resolution (deterministic cache by container/blob)
# -----------------------------------------------------------------------------


def _parse_az_ref(az_ref: str) -> Tuple[str, str]:
    s = (az_ref or "").strip()
    if not s.startswith("az://"):
        raise ValueError(f"not an az ref: {az_ref!r}")
    rest = s[len("az://") :]
    parts = [p for p in rest.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"invalid az ref: {az_ref!r}")
    container = parts[0]
    blob = "/".join(parts[1:])
    return container, blob


def _safe_blob_relpath(blob: str) -> str:
    # prevent traversal; keep stable, inspectable paths
    segs = []
    for s in (blob or "").split("/"):
        s = s.strip()
        if not s or s in (".", ".."):
            continue
        segs.append(s)
    return "/".join(segs)


def _cache_path_for_az(cache_dir: str, container: str, blob: str) -> str:
    blob_rel = _safe_blob_relpath(blob)
    local = os.path.join(cache_dir, container, blob_rel)
    os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
    return local


def _maybe_write_download_result(ret: Any, local_path: str) -> bool:
    if ret is None:
        return False
    try:
        if isinstance(ret, (bytes, bytearray)):
            with open(local_path, "wb") as f:
                f.write(bytes(ret))
            return os.path.exists(local_path) and os.path.getsize(local_path) > 0
        if isinstance(ret, dict):
            for k in ("data", "content", "bytes"):
                v = ret.get(k)
                if isinstance(v, (bytes, bytearray)):
                    with open(local_path, "wb") as f:
                        f.write(bytes(v))
                    return os.path.exists(local_path) and os.path.getsize(local_path) > 0
            p = ret.get("path") or ret.get("local_path")
            if isinstance(p, str) and os.path.exists(p) and os.path.getsize(p) > 0:
                if p != local_path:
                    shutil.copyfile(p, local_path)
                return os.path.exists(local_path) and os.path.getsize(local_path) > 0
        if hasattr(ret, "readall"):
            data = ret.readall()
            if isinstance(data, (bytes, bytearray)) and data:
                with open(local_path, "wb") as f:
                    f.write(bytes(data))
                return os.path.exists(local_path) and os.path.getsize(local_path) > 0
        if hasattr(ret, "read"):
            data = ret.read()
            if isinstance(data, (bytes, bytearray)) and data:
                with open(local_path, "wb") as f:
                    f.write(bytes(data))
                return os.path.exists(local_path) and os.path.getsize(local_path) > 0
    except Exception:
        return False
    return False


def _call_any_download_az(storage: Any, az_ref: str, local_path: str) -> str:
    if not storage:
        raise RuntimeError("storage missing (needed to download az:// refs)")

    container, blob = _parse_az_ref(az_ref)
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)

    candidates = [
        ("download_to_path", (az_ref, local_path)),
        ("download_blob_to_path", (az_ref, local_path)),
        ("download_path", (az_ref, local_path)),
        ("download_file", (az_ref, local_path)),
        ("download", (az_ref, local_path)),
        ("download_to_path", (container, blob, local_path)),
        ("download_blob_to_path", (container, blob, local_path)),
        ("download_path", (container, blob, local_path)),
        ("download_file", (container, blob, local_path)),
        ("download", (container, blob, local_path)),
        ("download_blob", (container, blob)),
        ("download_bytes", (container, blob)),
        ("get_blob", (container, blob)),
    ]

    last_err: Optional[Exception] = None
    tried_any = False

    for name, args in candidates:
        m = getattr(storage, name, None)
        if not callable(m):
            continue
        tried_any = True
        try:
            ret = m(*args)
            if inspect.isawaitable(ret):
                ret = _run_coro_sync(ret)

            if _maybe_write_download_result(ret, local_path):
                return local_path

            if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                return local_path
        except Exception as e:
            last_err = e
            continue

    fallback_err: Optional[Exception] = None
    try:
        conn = (os.getenv("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
        if not conn:
            raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING missing for az:// download fallback")
        from azure.storage.blob import BlobServiceClient  # type: ignore

        svc = BlobServiceClient.from_connection_string(conn)
        bc = svc.get_blob_client(container=container, blob=blob)
        stream = bc.download_blob()
        data = stream.readall()
        with open(local_path, "wb") as f:
            f.write(data)
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            return local_path
        raise RuntimeError("azure sdk download produced empty file")
    except Exception as e:
        fallback_err = e

    raise RuntimeError(
        f"storage download failed for {az_ref!r} -> {local_path!r}; "
        f"tried_storage_methods={tried_any} last_err={last_err!r} fallback_err={fallback_err!r}"
    )


def _resolve_ref_to_local(storage: Any, ref: str, local_path: str, *, cache_dir: Optional[str] = None) -> str:
    s = (ref or "").strip()
    if not s:
        return ""
    if os.path.exists(s):
        return s
    if s.startswith("az://"):
        container, blob = _parse_az_ref(s)
        if cache_dir:
            cache_path = _cache_path_for_az(cache_dir, container, blob)
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                return cache_path
            tmp = f"{cache_path}.tmp.{uuid4().hex}"
            _call_any_download_az(storage, s, tmp)
            os.replace(tmp, cache_path)
            return cache_path
        return _call_any_download_az(storage, s, local_path)
    if _is_http_url(s):
        _download_to_path(s, local_path, timeout_s=120, max_bytes=80 * 1024 * 1024)
        return local_path
    raise RuntimeError(f"unsupported ref: {ref!r}")


# -----------------------------------------------------------------------------
# Fabric tiling + garment proxy (REQUIRED by vton_provider.py)
# -----------------------------------------------------------------------------


def _prepare_saree_texture_tile(
    *,
    saree_full_path: str,
    out_tile_path: str,
    tile_size: int = 1024,
    bg_dist_thresh: int = 28,
) -> Dict[str, Any]:
    """
    Convert a saree product photo into a repeatable fabric tile (deterministic).
    """
    from PIL import Image, ImageOps
    import statistics

    im0 = Image.open(saree_full_path).convert("RGB")
    w0, h0 = im0.size

    w_small = 320
    h_small = max(1, int(h0 * (w_small / max(1, w0))))
    im = im0.resize((w_small, h_small))
    px = im.load()

    step = max(1, w_small // 80)
    border = []
    for x in range(0, w_small, step):
        border.append(px[x, 0])
        border.append(px[x, h_small - 1])
    for y in range(0, h_small, step):
        border.append(px[0, y])
        border.append(px[w_small - 1, y])

    br = int(statistics.median([c[0] for c in border])) if border else 255
    bg = int(statistics.median([c[1] for c in border])) if border else 255
    bb = int(statistics.median([c[2] for c in border])) if border else 255

    xmin, ymin, xmax, ymax = w_small, h_small, -1, -1
    fg_count = 0
    thr2 = int(bg_dist_thresh) ** 2

    for y in range(h_small):
        for x in range(w_small):
            r, g, b = px[x, y]
            dr = r - br
            dg = g - bg
            db = b - bb
            if (dr * dr + dg * dg + db * db) > thr2:
                fg_count += 1
                xmin = min(xmin, x)
                ymin = min(ymin, y)
                xmax = max(xmax, x)
                ymax = max(ymax, y)

    dbg: Dict[str, Any] = {
        "src": saree_full_path,
        "src_size": [w0, h0],
        "bg_rgb": [br, bg, bb],
        "fg_count_small": fg_count,
        "bbox_small": [xmin, ymin, xmax, ymax],
        "tile_size": int(tile_size),
        "bg_dist_thresh": int(bg_dist_thresh),
    }

    if xmax <= xmin or ymax <= ymin or fg_count < 50:
        cx0 = int(w0 * 0.2)
        cy0 = int(h0 * 0.2)
        cx1 = int(w0 * 0.8)
        cy1 = int(h0 * 0.8)
        crop = im0.crop((cx0, cy0, cx1, cy1))
        dbg["bbox_fallback"] = True
    else:
        sx = w0 / float(w_small)
        sy = h0 / float(h_small)
        pad = 0.06
        x0 = max(0, int((xmin - pad * w_small) * sx))
        y0 = max(0, int((ymin - pad * h_small) * sy))
        x1 = min(w0, int((xmax + pad * w_small) * sx))
        y1 = min(h0, int((ymax + pad * h_small) * sy))
        crop = im0.crop((x0, y0, x1, y1))
        dbg["bbox_full"] = [x0, y0, x1, y1]
        dbg["bbox_fallback"] = False

    cw, ch = crop.size
    s = max(1, min(cw, ch))
    cx = cw // 2
    cy = ch // 2
    patch = crop.crop((cx - s // 2, cy - s // 2, cx - s // 2 + s, cy - s // 2 + s))
    patch = patch.resize((int(tile_size), int(tile_size)))

    a = patch
    b = ImageOps.mirror(patch)
    c = ImageOps.flip(patch)
    d = ImageOps.flip(b)

    mosaic = Image.new("RGB", (tile_size * 2, tile_size * 2))
    mosaic.paste(a, (0, 0))
    mosaic.paste(b, (tile_size, 0))
    mosaic.paste(c, (0, tile_size))
    mosaic.paste(d, (tile_size, tile_size))

    tile = mosaic.crop((tile_size // 2, tile_size // 2, tile_size // 2 + tile_size, tile_size // 2 + tile_size))
    os.makedirs(os.path.dirname(out_tile_path) or ".", exist_ok=True)
    tile.save(out_tile_path, "PNG")
    dbg["out_tile"] = out_tile_path
    return dbg


def _make_garment_proxy_from_mask(
    *,
    tile_path: str,
    alpha_mask_path: str,
    out_path: str,
    size: int = 1024,
) -> Dict[str, Any]:
    """
    Build a wearable saree proxy image:
      - alpha = saree silhouette (alpha_mask_path)
      - rgb = tiled fabric texture
    Output is RGBA PNG on transparent background.
    """
    from PIL import Image

    tile = Image.open(tile_path).convert("RGB")
    mask_rgba = Image.open(alpha_mask_path).convert("RGBA")

    mask_rgba = mask_rgba.resize((size, size), resample=Image.BILINEAR)
    alpha = mask_rgba.getchannel("A")

    canvas = Image.new("RGB", (size, size))
    tw, th = tile.size
    for y in range(0, size, th):
        for x in range(0, size, tw):
            canvas.paste(tile, (x, y))

    out = Image.merge("RGBA", (*canvas.split(), alpha))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    out.save(out_path, "PNG")

    mn, mx = alpha.getextrema()
    return {
        "proxy_size": int(size),
        "alpha_extrema": [int(mn), int(mx)],
        "tile": tile_path,
        "mask": alpha_mask_path,
        "out": out_path,
    }


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------


@dataclass
class SareeDrapeConfig:
    enabled: bool = True
    strict: bool = True
    require_full_body: bool = True

    # Proxy generation (required)
    alpha_mask_path: str = ""
    cache_dir: str = "/var/cache/df_saree_templates"

    # Provider order (ML-first)
    provider_order: str = "flux2_pro,fashn,imageapps,leffa"

    # Flux2 multi-reference edit (best PoC for saree drape)
    flux_enabled: bool = True
    flux_model_id: str = "fal-ai/flux-2-pro/edit"
    flux_output_format: str = "png"
    flux_image_size: str = "auto"
    flux_safety_tolerance: str = "2"
    flux_enable_safety_checker: bool = True

    # FASHN v1.6
    fashn_model_id: str = "fal-ai/fashn/tryon/v1.6"
    fashn_category: str = "auto"
    fashn_mode: str = "quality"
    fashn_garment_photo_type: str = "auto"
    fashn_moderation_level: str = "permissive"
    fashn_num_samples: int = 2
    fashn_segmentation_free: bool = True
    fashn_output_format: str = "png"

    # Image-apps virtual try-on (extra fallback)
    imageapps_model_id: str = "fal-ai/image-apps-v2/virtual-try-on"
    imageapps_preserve_pose: bool = True
    imageapps_aspect_ratio: str = "3:4"

    # Leffa fallback
    leffa_model_id: str = "fal-ai/leffa/virtual-tryon"
    leffa_garment_type: str = "dresses"
    leffa_num_inference_steps: int = 50
    leffa_guidance_scale: float = 2.5
    leffa_enable_safety_checker: bool = True
    leffa_output_format: str = "png"

    # Caption QC
    caption_qc_enabled: bool = True
    caption_qc_fail_open: bool = True
    caption_models: str = "fal-ai/moondream-next,fal-ai/moondream2,fal-ai/moondream3-preview/caption"

    # Fal timing
    fal_timeout_s: int = 900
    fal_poll_s: int = 2

    # Output / debug
    output_prefix: str = "commerce/vton/saree_drape"
    run_dir_base: str = "/var/cache/df_saree_drape_runs"
    keep_run_dir: bool = True

    @staticmethod
    def from_env() -> "SareeDrapeConfig":
        strict = _env_bool("COMMERCE_SAREE_STRICT", True)
        enabled = _env_bool("COMMERCE_ENABLE_SAREE_DRAPE_PROVIDER", True) or _env_bool("DF_ENABLE_SAREE_DRAPE_PROVIDER", False)
        if strict:
            enabled = True

        flux_enabled = _env_bool("COMMERCE_SAREE_FLUX_ENABLED", True)
        # If no FAL_KEY, disable all fal providers (but strict PoC expects it)
        if not _fal_key():
            flux_enabled = False

        return SareeDrapeConfig(
            enabled=enabled,
            strict=strict,
            require_full_body=_env_bool("DF_SAREE_DRAPE_REQUIRE_FULL_BODY", True),
            alpha_mask_path=(_env_str("COMMERCE_SAREE_ALPHA_MASK_PATH", "") or _env_str("DF_SAREE_ALPHA_MASK_PATH", "")).strip(),
            cache_dir=(_env_str("COMMERCE_SAREE_CACHE_DIR", "") or _env_str("DF_SAREE_CACHE_DIR", "") or "/var/cache/df_saree_templates").strip(),
            provider_order=(_env_str("COMMERCE_SAREE_PROVIDER_ORDER", "") or "flux2_pro,fashn,imageapps,leffa").strip(),
            flux_enabled=flux_enabled,
            flux_model_id=(_env_str("COMMERCE_SAREE_FLUX_MODEL_ID", "") or "fal-ai/flux-2-pro/edit").strip(),
            flux_output_format=(_env_str("COMMERCE_SAREE_FLUX_OUTPUT_FORMAT", "") or "png").strip(),
            flux_image_size=(_env_str("COMMERCE_SAREE_FLUX_IMAGE_SIZE", "") or "auto").strip(),
            flux_safety_tolerance=(_env_str("COMMERCE_SAREE_FLUX_SAFETY_TOLERANCE", "") or "2").strip(),
            flux_enable_safety_checker=_env_bool("COMMERCE_SAREE_FLUX_SAFETY_CHECKER", True),
            fashn_model_id=(_env_str("COMMERCE_SAREE_FASHN_MODEL_ID", "") or "fal-ai/fashn/tryon/v1.6").strip(),
            fashn_category=(_env_str("COMMERCE_SAREE_FASHN_CATEGORY", "") or "auto").strip(),
            fashn_mode=(_env_str("COMMERCE_SAREE_FASHN_MODE", "") or "quality").strip(),
            fashn_garment_photo_type=(_env_str("COMMERCE_SAREE_FASHN_GARMENT_PHOTO_TYPE", "") or "auto").strip(),
            fashn_moderation_level=(_env_str("COMMERCE_SAREE_FASHN_MODERATION_LEVEL", "") or "permissive").strip(),
            fashn_num_samples=_env_int("COMMERCE_SAREE_FASHN_NUM_SAMPLES", 2),
            fashn_segmentation_free=_env_bool("COMMERCE_SAREE_FASHN_SEGMENTATION_FREE", True),
            fashn_output_format=(_env_str("COMMERCE_SAREE_FASHN_OUTPUT_FORMAT", "") or "png").strip(),
            imageapps_model_id=(_env_str("COMMERCE_SAREE_IMAGEAPPS_MODEL_ID", "") or "fal-ai/image-apps-v2/virtual-try-on").strip(),
            imageapps_preserve_pose=_env_bool("COMMERCE_SAREE_IMAGEAPPS_PRESERVE_POSE", True),
            imageapps_aspect_ratio=(_env_str("COMMERCE_SAREE_IMAGEAPPS_ASPECT_RATIO", "") or "3:4").strip(),
            leffa_model_id=(_env_str("COMMERCE_SAREE_LEFFA_MODEL_ID", "") or "fal-ai/leffa/virtual-tryon").strip(),
            leffa_garment_type=(_env_str("COMMERCE_SAREE_LEFFA_GARMENT_TYPE", "") or "dresses").strip(),
            leffa_num_inference_steps=_env_int("COMMERCE_SAREE_LEFFA_STEPS", 50),
            leffa_guidance_scale=_coerce_float(os.getenv("COMMERCE_SAREE_LEFFA_GUIDANCE"), 2.5),
            leffa_enable_safety_checker=_env_bool("COMMERCE_SAREE_LEFFA_SAFETY", True),
            leffa_output_format=(_env_str("COMMERCE_SAREE_LEFFA_OUTPUT_FORMAT", "") or "png").strip(),
            caption_qc_enabled=_env_bool("COMMERCE_SAREE_CAPTION_QC_ENABLED", True),
            caption_qc_fail_open=_env_bool("COMMERCE_SAREE_CAPTION_QC_FAIL_OPEN", True),
            caption_models=(_env_str("COMMERCE_SAREE_CAPTION_MODELS", "") or "fal-ai/moondream-next,fal-ai/moondream2,fal-ai/moondream3-preview/caption").strip(),
            fal_timeout_s=_env_int("COMMERCE_SAREE_FAL_TIMEOUT_S", 900),
            fal_poll_s=_env_int("COMMERCE_SAREE_FAL_POLL_S", 2),
            output_prefix=(_env_str("DF_SAREE_DRAPE_OUT_PREFIX", "commerce/vton/saree_drape") or "commerce/vton/saree_drape").strip().strip("/"),
            run_dir_base=_env_str("DF_SAREE_RUN_DIR_BASE", "/var/cache/df_saree_drape_runs"),
            keep_run_dir=_env_bool("DF_SAREE_KEEP_RUN_DIR", True),
        )


class SareeDrapeProvider:
    """
    Production-PoC:
      - MUST expose build_garment_proxy_url() (vton_provider depends on it)
      - run() tries ML-first saree drape via FLUX multi-reference edit
      - falls back to other try-on endpoints
      - caption QC prevents "dress-like" outputs from being accepted
    """

    def __init__(
        self,
        *,
        storage: Any = None,
        blender_runner: Any = None,
        runner: Any = None,
        config: Optional[SareeDrapeConfig] = None,
    ) -> None:
        self.storage = storage
        self.runner = blender_runner or runner
        self.cfg = config or SareeDrapeConfig.from_env()

    # -------------------------------------------------------------------------
    # REQUIRED by vton_provider.py
    # -------------------------------------------------------------------------
    def build_garment_proxy_url(self, *, user_id: str, job_id: str, saree_url: str) -> Tuple[str, Dict[str, Any]]:
        """
        NOTE: job_id here should preferably be the *base* job id (no variant suffix),
        so all variants reuse the same proxy blob prefix deterministically.
        """
        if not self.storage:
            raise RuntimeError("SAREE_PROXY_REQUIRES_STORAGE")
        if not self.cfg.alpha_mask_path:
            raise RuntimeError("SAREE_PROXY_MISSING_ALPHA_MASK_PATH (set COMMERCE_SAREE_ALPHA_MASK_PATH)")

        base_job_id, _ = _split_job_id(job_id)

        debug: Dict[str, Any] = {"stage": "garment_proxy", "user_id": user_id, "job_id": base_job_id, "saree_url": saree_url}
        os.makedirs(self.cfg.run_dir_base, exist_ok=True)

        safe_job = _sanitize_for_path(base_job_id)
        run_dir = tempfile.mkdtemp(prefix=f"df_saree_proxy_{safe_job}_", dir=self.cfg.run_dir_base)
        debug["run_dir"] = run_dir

        try:
            max_bytes = 60 * 1024 * 1024
            saree_full_path = os.path.join(run_dir, f"saree_full{_ext_from_url(saree_url, '.png')}")
            _download_to_path(saree_url, saree_full_path, timeout_s=120, max_bytes=max_bytes)

            tile_path = os.path.join(run_dir, "saree_tile.png")
            tile_dbg = _prepare_saree_texture_tile(
                saree_full_path=saree_full_path,
                out_tile_path=tile_path,
                tile_size=_env_int("DF_SAREE_TEXTURE_TILE_SIZE", 1024),
                bg_dist_thresh=_env_int("DF_SAREE_TEXTURE_TILE_BG_THRESH", 28),
            )
            debug["tile"] = tile_dbg

            alpha_local = _resolve_ref_to_local(
                self.storage,
                self.cfg.alpha_mask_path,
                os.path.join(run_dir, "saree_alpha.png"),
                cache_dir=self.cfg.cache_dir or "/var/cache/df_saree_templates",
            )
            debug["alpha_mask_local"] = alpha_local
            debug["alpha_mask_ref"] = self.cfg.alpha_mask_path

            proxy_local = os.path.join(run_dir, "garment_proxy.png")
            proxy_dbg = _make_garment_proxy_from_mask(
                tile_path=tile_path,
                alpha_mask_path=alpha_local,
                out_path=proxy_local,
                size=_env_int("DF_SAREE_PROXY_SIZE", 1024),
            )
            debug["proxy"] = proxy_dbg

            proxy_blob = f"{self.cfg.output_prefix}_proxy/{user_id}/{base_job_id}/{uuid4().hex}/garment_proxy_{_safe_filename('.png')}"
            proxy_url = _call_any_upload(self.storage, proxy_local, proxy_blob, "image/png")
            debug["proxy_upload"] = {"blob": proxy_blob, "url": proxy_url}

            _write_json(os.path.join(run_dir, "proxy_debug.json"), debug)
            return proxy_url, debug
        finally:
            if not bool(self.cfg.keep_run_dir):
                shutil.rmtree(run_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # Routing / applicability
    # -------------------------------------------------------------------------
    def can_handle(self, *, request: Dict[str, Any], resolved_inputs: Dict[str, Any]) -> bool:
        if not self.cfg.enabled:
            return False
        ri = _as_dict(resolved_inputs)
        outfit_kind = (ri.get("outfit_kind") or ri.get("outfit") or "").strip().lower()
        if outfit_kind == "saree_set":
            return True
        if bool(ri.get("saree_like")):
            return True
        rk = (_as_dict(request.get("input")).get("outfit_kind") or request.get("outfit_kind") or "").strip().lower()
        return rk == "saree_set"

    # -------------------------------------------------------------------------
    # MAIN: REAL saree drape (PoC)
    # -------------------------------------------------------------------------
    def run(
        self,
        *,
        job_id: Any,
        user_id: Any,
        request: Dict[str, Any],
        resolved_inputs: Dict[str, Any],
    ) -> Dict[str, Any]:
        ri = _as_dict(resolved_inputs)

        base_job_id, variant_idx = _split_job_id(job_id)

        debug: Dict[str, Any] = {
            "provider": "saree_drape",
            "provider_mode": "ml_first",
            "steps": [],
            "job_id": str(job_id),
            "base_job_id": str(base_job_id),
            "variant_idx": int(variant_idx),
            "cfg": {
                "provider_order": self.cfg.provider_order,
                "flux_model_id": self.cfg.flux_model_id,
                "fashn_model_id": self.cfg.fashn_model_id,
                "imageapps_model_id": self.cfg.imageapps_model_id,
                "leffa_model_id": self.cfg.leffa_model_id,
                "caption_qc_enabled": self.cfg.caption_qc_enabled,
                "caption_models": self.cfg.caption_models,
                "caption_qc_fail_open": self.cfg.caption_qc_fail_open,
                "strict": bool(self.cfg.strict),
            },
        }

        if not self.can_handle(request=request, resolved_inputs=ri):
            raise ValueError("SAREE_DRAPE_NOT_APPLICABLE")
        if not self.storage:
            raise RuntimeError("SAREE_DRAPE_REQUIRES_STORAGE")
        if not _fal_key():
            raise RuntimeError("SAREE_DRAPE_REQUIRES_FAL_KEY")

        # Person image URL
        model_ref = _as_dict(ri.get("model_ref")) or _as_dict(_as_dict(request.get("input")).get("model_ref")) or _as_dict(request.get("model_ref"))
        model_url = None
        for k in ("human_image_url", "image_url", "url", "ref_url", "photo_url"):
            v = model_ref.get(k)
            if _is_http_url(v):
                model_url = str(v).strip()
                break
        if not model_url:
            raise ValueError("SAREE_DRAPE_MISSING_MODEL_URL")
        debug["model_url"] = model_url

        full_body = bool(
            _as_dict(ri.get("views")).get("full_body")
            or _as_dict(_as_dict(request.get("input")).get("views")).get("full_body")
            or bool(model_ref.get("full_body"))
        )
        if self.cfg.require_full_body and not full_body:
            raise ValueError("SAREE_DRAPE_REQUIRES_FULL_BODY")

        # Saree image URL
        saree_url = str(ri.get("saree_url") or "").strip()
        if not _is_http_url(saree_url):
            items = _as_list(ri.get("items")) or _as_list(_as_dict(ri.get("product_assets")).get("items"))
            if items:
                u = _as_dict(items[0]).get("image_url") or _as_dict(items[0]).get("url")
                if _is_http_url(u):
                    saree_url = str(u).strip()
        if not _is_http_url(saree_url):
            raise ValueError("SAREE_DRAPE_MISSING_SAREE_URL")
        debug["saree_url"] = saree_url

        seed_int = _stable_seed(str(job_id), str(user_id), saree_url, "saree_drape_v2")
        debug["seed"] = int(seed_int)

        os.makedirs(self.cfg.run_dir_base, exist_ok=True)
        safe_job = _sanitize_for_path(base_job_id)
        run_dir = tempfile.mkdtemp(prefix=f"df_saree_drape_{safe_job}_v{int(variant_idx)}_", dir=self.cfg.run_dir_base)
        debug["run_dir"] = run_dir

        providers = [p.strip().lower() for p in (self.cfg.provider_order or "").split(",") if p.strip()]
        if not providers:
            providers = ["flux2_pro", "fashn", "imageapps", "leffa"]

        attempts: List[Dict[str, Any]] = []
        best_url: Optional[str] = None
        best_provider: Optional[str] = None
        best_request_id: Optional[str] = None
        first_candidate_url: Optional[str] = None
        first_candidate_provider: Optional[str] = None

        # Build proxy once (used by flux prompt; harmless for other providers)
        proxy_url: Optional[str] = None
        proxy_dbg: Optional[Dict[str, Any]] = None
        try:
            debug["steps"].append("build_garment_proxy")
            proxy_url, proxy_dbg = self.build_garment_proxy_url(user_id=str(user_id), job_id=str(base_job_id), saree_url=saree_url)
            debug["proxy_url"] = proxy_url
            debug["proxy_debug"] = proxy_dbg
        except Exception as e:
            debug["proxy_error"] = f"{type(e).__name__}: {e}"
            # Flux can still run without proxy, but saree quality drops.
            proxy_url = None

        caption_model_ids = [m.strip() for m in (self.cfg.caption_models or "").split(",") if m.strip()]

        def _caption_qc(image_url: str) -> Tuple[bool, Dict[str, Any]]:
            if not self.cfg.caption_qc_enabled:
                return True, {"enabled": False, "skipped": True}
            try:
                cap, capdbg = _fal_caption_any(
                    image_url,
                    model_ids=caption_model_ids,
                    timeout_s=min(300, int(self.cfg.fal_timeout_s)),
                    poll_s=max(1, int(self.cfg.fal_poll_s)),
                )
                ok = _caption_contains_saree(cap)
                return ok, {"enabled": True, "caption": cap, "ok": ok, "caption_debug": capdbg}
            except Exception as e:
                # IMPORTANT: do not let transient caption infra kill drape generation
                err = f"{type(e).__name__}: {e}"
                if self.cfg.caption_qc_fail_open:
                    return True, {"enabled": True, "error": err, "fail_open": True, "ok": True}
                return False, {"enabled": True, "error": err, "fail_open": False, "ok": False}

        def _download_upload(out_img_url: str, name: str) -> str:
            local = os.path.join(run_dir, name)
            _download_to_path(out_img_url, local, timeout_s=120, max_bytes=120 * 1024 * 1024)
            # include base_job_id + variant_idx to keep variant outputs distinct and groupable
            blob = f"{self.cfg.output_prefix}/{user_id}/{base_job_id}/v{int(variant_idx)}/{uuid4().hex}/{name}"
            return _call_any_upload(self.storage, local, blob, "image/png")

        try:
            for prov in providers:
                try:
                    if prov in ("flux2", "flux2_pro", "flux"):
                        if not self.cfg.flux_enabled:
                            attempts.append({"provider": prov, "skipped": True, "reason": "flux_disabled"})
                            continue

                        debug["steps"].append("fal_flux2_edit")
                        # Multi-reference: person + proxy (silhouette+fabric) + original product photo
                        image_urls: List[str] = [model_url]
                        if proxy_url:
                            image_urls.append(proxy_url)
                        image_urls.append(saree_url)

                        if proxy_url:
                            prompt = (
                                "You are given references:\n"
                                "@image1 is a full-body photo of a person.\n"
                                "@image2 is a saree silhouette proxy with the saree fabric pattern.\n"
                                "@image3 is the saree product photo showing the fabric.\n\n"
                                "Edit @image1 so the person is wearing a traditional Indian saree in Nivi drape style. "
                                "Use the exact fabric pattern and colors from @image3 and the drape silhouette from @image2. "
                                "Add a matching blouse. The saree must have realistic pleats at the waist and a pallu draped over the left shoulder. "
                                "Preserve the person's identity, face, body shape, pose, lighting, and background from @image1. "
                                "Photorealistic, natural cloth folds, correct occlusion with arms/hair."
                            )
                        else:
                            prompt = (
                                "You are given references:\n"
                                "@image1 is a full-body photo of a person.\n"
                                "@image2 is the saree product photo showing the fabric.\n\n"
                                "Edit @image1 so the person is wearing a traditional Indian saree in Nivi drape style. "
                                "Use the exact fabric pattern and colors from @image2. Add a matching blouse. "
                                "The saree must have realistic pleats at the waist and a pallu draped over the left shoulder. "
                                "Preserve the person's identity, face, body shape, pose, lighting, and background from @image1. "
                                "Photorealistic, natural cloth folds, correct occlusion with arms/hair."
                            )

                        resp = _fal_queue_run(
                            self.cfg.flux_model_id,
                            {
                                "prompt": prompt,
                                "image_urls": image_urls,
                                "image_size": self.cfg.flux_image_size,
                                "seed": int(seed_int),
                                "safety_tolerance": str(self.cfg.flux_safety_tolerance),
                                "enable_safety_checker": bool(self.cfg.flux_enable_safety_checker),
                                "output_format": self.cfg.flux_output_format,
                            },
                            timeout_s=int(self.cfg.fal_timeout_s),
                            poll_s=int(self.cfg.fal_poll_s),
                        )
                        req_id = str(resp.get("_request_id") or "")
                        out_img_url = _fal_extract_image_url_images(resp)
                        url = _download_upload(out_img_url, f"tryon_flux2_{_safe_filename('.png')}")
                        qc_ok, qc_dbg = _caption_qc(url)

                        if first_candidate_url is None:
                            first_candidate_url, first_candidate_provider = url, "flux2_pro"

                        attempts.append({"provider": "flux2_pro", "request_id": req_id, "fal_url": out_img_url, "url": url, "qc": qc_dbg})

                        if qc_ok:
                            best_url, best_provider, best_request_id = url, "flux2_pro", req_id
                            break
                        continue

                    if prov == "fashn":
                        debug["steps"].append("fal_fashn")
                        resp = _fal_queue_run(
                            self.cfg.fashn_model_id,
                            {
                                "model_image": model_url,
                                "garment_image": saree_url,
                                "category": self.cfg.fashn_category,
                                "mode": self.cfg.fashn_mode,
                                "garment_photo_type": self.cfg.fashn_garment_photo_type,
                                "moderation_level": self.cfg.fashn_moderation_level,
                                "seed": int(seed_int),
                                "num_samples": int(max(1, self.cfg.fashn_num_samples)),
                                "segmentation_free": bool(self.cfg.fashn_segmentation_free),
                                "output_format": self.cfg.fashn_output_format,
                            },
                            timeout_s=int(self.cfg.fal_timeout_s),
                            poll_s=int(self.cfg.fal_poll_s),
                        )
                        req_id = str(resp.get("_request_id") or "")
                        out_img_url = _fal_extract_image_url_images(resp)
                        url = _download_upload(out_img_url, f"tryon_fashn_{_safe_filename('.png')}")
                        qc_ok, qc_dbg = _caption_qc(url)

                        if first_candidate_url is None:
                            first_candidate_url, first_candidate_provider = url, "fashn"

                        attempts.append({"provider": "fashn", "request_id": req_id, "fal_url": out_img_url, "url": url, "qc": qc_dbg})

                        if qc_ok:
                            best_url, best_provider, best_request_id = url, "fashn", req_id
                            break
                        continue

                    if prov in ("imageapps", "virtual_tryon"):
                        debug["steps"].append("fal_imageapps")
                        resp = _fal_queue_run(
                            self.cfg.imageapps_model_id,
                            {
                                "person_image_url": model_url,
                                "clothing_image_url": saree_url,
                                "preserve_pose": bool(self.cfg.imageapps_preserve_pose),
                                "aspect_ratio": {"ratio": self.cfg.imageapps_aspect_ratio},
                            },
                            timeout_s=int(self.cfg.fal_timeout_s),
                            poll_s=int(self.cfg.fal_poll_s),
                        )
                        req_id = str(resp.get("_request_id") or "")
                        out_img_url = _fal_extract_image_url_images(resp)
                        url = _download_upload(out_img_url, f"tryon_imageapps_{_safe_filename('.png')}")
                        qc_ok, qc_dbg = _caption_qc(url)

                        if first_candidate_url is None:
                            first_candidate_url, first_candidate_provider = url, "imageapps"

                        attempts.append({"provider": "imageapps", "request_id": req_id, "fal_url": out_img_url, "url": url, "qc": qc_dbg})

                        if qc_ok:
                            best_url, best_provider, best_request_id = url, "imageapps", req_id
                            break
                        continue

                    if prov == "leffa":
                        debug["steps"].append("fal_leffa")
                        resp = _fal_queue_run(
                            self.cfg.leffa_model_id,
                            {
                                "human_image_url": model_url,
                                "garment_image_url": saree_url,
                                "garment_type": self.cfg.leffa_garment_type,
                                "num_inference_steps": int(self.cfg.leffa_num_inference_steps),
                                "guidance_scale": float(self.cfg.leffa_guidance_scale),
                                "enable_safety_checker": bool(self.cfg.leffa_enable_safety_checker),
                                "output_format": self.cfg.leffa_output_format,
                            },
                            timeout_s=int(self.cfg.fal_timeout_s),
                            poll_s=int(self.cfg.fal_poll_s),
                        )
                        req_id = str(resp.get("_request_id") or "")
                        out_img_url = _fal_extract_image_url_leffa(resp)
                        url = _download_upload(out_img_url, f"tryon_leffa_{_safe_filename('.png')}")
                        qc_ok, qc_dbg = _caption_qc(url)

                        if first_candidate_url is None:
                            first_candidate_url, first_candidate_provider = url, "leffa"

                        attempts.append({"provider": "leffa", "request_id": req_id, "fal_url": out_img_url, "url": url, "qc": qc_dbg})

                        if qc_ok:
                            best_url, best_provider, best_request_id = url, "leffa", req_id
                            break
                        continue

                    attempts.append({"provider": prov, "skipped": True, "reason": "unknown_provider"})
                except Exception as e:
                    err = f"{prov}:{type(e).__name__}: {e}"
                    attempts.append({"provider": prov, "error": err})
                    continue

            debug["attempts"] = attempts

            if not best_url:
                if not self.cfg.strict and first_candidate_url:
                    best_url = first_candidate_url
                    best_provider = first_candidate_provider or "unknown"
                    best_request_id = None
                    debug["non_strict_fallback"] = {"picked_url": best_url, "provider": best_provider}
                else:
                    raise RuntimeError(
                        f"SAREE_TRYON_FAILED no_acceptable_candidate strict={self.cfg.strict} "
                        f"attempts_sample={attempts[:4]}... total={len(attempts)}"
                    )

            debug["final"] = {"provider": best_provider, "request_id": best_request_id, "output_url": best_url}
            _write_json(os.path.join(run_dir, "debug.json"), {"debug": debug})

            return {
                "provider": "saree_drape",
                "provider_mode": f"ml_{best_provider}",
                "status": "succeeded",
                "baseline_url": best_url,
                "output_url": best_url,
                "fal_request_id": best_request_id,
                "seed": int(seed_int),
                "variant_idx": int(variant_idx),
                "variant_seed": str(_stable_seed_hex16(str(base_job_id), str(variant_idx), str(user_id), saree_url, "saree_drape_v2")),
                "debug": debug,
            }

        except Exception as e:
            debug["error"] = f"{type(e).__name__}: {e}"
            _write_json(os.path.join(run_dir, "debug.json"), {"debug": debug})
            raise
        finally:
            if not bool(self.cfg.keep_run_dir):
                shutil.rmtree(run_dir, ignore_errors=True)