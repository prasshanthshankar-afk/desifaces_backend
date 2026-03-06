from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.request import Request, urlopen

import asyncpg
from PIL import Image, ImageOps
from azure.storage.blob import BlobServiceClient
from azure.storage.blob import generate_blob_sas, BlobSasPermissions


# -------------------------
# small helpers
# -------------------------

def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}

def _jget(d: Dict[str, Any], *path: str) -> Any:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()

def _safe(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(s or ""))
    return s[:80] or "x"

def _http_json(method: str, url: str, *, headers: Dict[str, str], body: Optional[Dict[str, Any]] = None, timeout_s: int = 180) -> Dict[str, Any]:
    m = (method or "GET").upper()
    data = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if body is not None and m not in ("GET", "HEAD"):
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = Request(url=url, method=m, headers=hdrs, data=data)
    with urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read() or b""
        txt = raw.decode("utf-8", errors="replace").strip()
        if not txt:
            return {}
        out = json.loads(txt)
        return out if isinstance(out, dict) else {"raw": out}

def _parse_first_image_url(d: Dict[str, Any]) -> str:
    # Typical fal result: {"images":[{"url":...}]} or {"image":{"url":...}}
    imgs = d.get("images")
    if isinstance(imgs, list) and imgs:
        u = _as_dict(imgs[0]).get("url")
        if isinstance(u, str) and u.startswith("http"):
            return u
    img = _as_dict(d.get("image"))
    u = img.get("url")
    if isinstance(u, str) and u.startswith("http"):
        return u
    # Nested response/output/data
    for k in ("response", "output", "data"):
        sub = _as_dict(d.get(k))
        if sub:
            u2 = _parse_first_image_url(sub)
            if u2:
                return u2
    raise RuntimeError(f"Could not find image url in result keys={list(d.keys())[:40]}")

def _ensure_png_bytes(img_bytes: bytes) -> bytes:
    im = Image.open(io.BytesIO(img_bytes))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()

def _make_texture_tile(saree_png_bytes: bytes, *, tile_size: int = 1024, bg_dist_thresh: int = 26) -> bytes:
    """
    Deterministically create a repeatable fabric tile from saree product image.
    """
    import statistics

    im0 = Image.open(io.BytesIO(saree_png_bytes)).convert("RGB")
    w0, h0 = im0.size

    w_small = 320
    h_small = max(1, int(h0 * (w_small / max(1, w0))))
    im = im0.resize((w_small, h_small))
    px = im.load()

    step = max(1, w_small // 80)
    border = []
    for x in range(0, w_small, step):
        border.append(px[x, 0]); border.append(px[x, h_small - 1])
    for y in range(0, h_small, step):
        border.append(px[0, y]); border.append(px[w_small - 1, y])

    br = int(statistics.median([c[0] for c in border])) if border else 255
    bg = int(statistics.median([c[1] for c in border])) if border else 255
    bb = int(statistics.median([c[2] for c in border])) if border else 255

    xmin, ymin, xmax, ymax = w_small, h_small, -1, -1
    fg_count = 0
    thr2 = int(bg_dist_thresh) ** 2

    for y in range(h_small):
        for x in range(w_small):
            r, g, b = px[x, y]
            dr = r - br; dg = g - bg; db = b - bb
            if (dr*dr + dg*dg + db*db) > thr2:
                fg_count += 1
                xmin = min(xmin, x); ymin = min(ymin, y)
                xmax = max(xmax, x); ymax = max(ymax, y)

    if xmax <= xmin or ymax <= ymin or fg_count < 50:
        crop = im0.crop((int(w0*0.2), int(h0*0.2), int(w0*0.8), int(h0*0.8)))
    else:
        sx = w0 / float(w_small)
        sy = h0 / float(h_small)
        pad = 0.06
        x0 = max(0, int((xmin - pad*w_small) * sx))
        y0 = max(0, int((ymin - pad*h_small) * sy))
        x1 = min(w0, int((xmax + pad*w_small) * sx))
        y1 = min(h0, int((ymax + pad*h_small) * sy))
        crop = im0.crop((x0, y0, x1, y1))

    cw, ch = crop.size
    s = max(1, min(cw, ch))
    cx = cw // 2
    cy = ch // 2
    patch = crop.crop((cx - s//2, cy - s//2, cx - s//2 + s, cy - s//2 + s)).resize((tile_size, tile_size))

    a = patch
    b = ImageOps.mirror(patch)
    c = ImageOps.flip(patch)
    d = ImageOps.flip(b)

    mosaic = Image.new("RGB", (tile_size*2, tile_size*2))
    mosaic.paste(a, (0, 0))
    mosaic.paste(b, (tile_size, 0))
    mosaic.paste(c, (0, tile_size))
    mosaic.paste(d, (tile_size, tile_size))

    tile = mosaic.crop((tile_size//2, tile_size//2, tile_size//2 + tile_size, tile_size//2 + tile_size))
    buf = io.BytesIO()
    tile.save(buf, format="PNG")
    return buf.getvalue()

def _make_garment_proxy(tile_png: bytes, saree_alpha_png: bytes, *, out_size: int = 1024) -> bytes:
    """
    Garment proxy = tiled texture under saree silhouette alpha.
    Output RGBA PNG.
    """
    tile = Image.open(io.BytesIO(tile_png)).convert("RGB")
    mask = Image.open(io.BytesIO(saree_alpha_png)).convert("RGBA").resize((out_size, out_size), resample=Image.BILINEAR)
    alpha = mask.getchannel("A")

    canvas = Image.new("RGB", (out_size, out_size))
    tw, th = tile.size
    for y in range(0, out_size, th):
        for x in range(0, out_size, tw):
            canvas.paste(tile, (x, y))

    out = Image.merge("RGBA", (*canvas.split(), alpha))
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


# -------------------------
# azure + sas
# -------------------------

@dataclass
class AzureAcct:
    conn_str: str
    account_name: str
    account_key: str

def _azure_acct_from_conn_str() -> AzureAcct:
    conn = _env("AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is required")
    parts = {}
    for kv in conn.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.strip()] = v.strip()
    acct = parts.get("AccountName") or ""
    key = parts.get("AccountKey") or ""
    if not acct or not key:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING must include AccountName and AccountKey")
    return AzureAcct(conn_str=conn, account_name=acct, account_key=key)

def _sas_url(acct: AzureAcct, *, container: str, blob: str, hours: int = 24) -> str:
    expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
    sas = generate_blob_sas(
        account_name=acct.account_name,
        account_key=acct.account_key,
        container_name=container,
        blob_name=blob,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
    )
    return f"https://{acct.account_name}.blob.core.windows.net/{container}/{blob}?{sas}"


# -------------------------
# db fetch (your schema)
# -------------------------

async def _fetch_example(pool: asyncpg.Pool, example_id: str) -> Dict[str, Any]:
    q = """
    select
      id,
      person_ref,
      garment_refs,
      conditioning_refs,
      target_ref,
      mask_refs,
      quality_json
    from training_examples
    where id = $1
    """
    async with pool.acquire() as con:
        row = await con.fetchrow(q, example_id)
    if not row:
        raise RuntimeError(f"training_examples not found: {example_id}")
    return dict(row)


# -------------------------
# fal call
# -------------------------

def _fal_status_endpoint_id(model_id: str) -> str:
    parts = [p for p in (model_id or "").split("/") if p]
    return "/".join(parts[:2]) if len(parts) >= 2 else (model_id or "").strip("/")

def _fal_submit_and_wait(
    *,
    fal_key: str,
    endpoint_id: str,
    payload: Dict[str, Any],
    poll_secs: float,
    poll_timeout_s: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Queue API:
      POST https://queue.fal.run/{model_id}
      GET  https://queue.fal.run/{model_id}/requests/{request_id}/status
      GET  https://queue.fal.run/{model_id}/requests/{request_id}
    """
    base = "https://queue.fal.run"
    endpoint_id = endpoint_id.strip().strip("/")
    post_url = f"{base}/{endpoint_id}"

    submit = _http_json("POST", post_url, headers={"Authorization": f"Key {fal_key}"}, body=payload, timeout_s=180)
    request_id = str(submit.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError(f"Fal submit missing request_id. submit={submit}")

    status_ep = _fal_status_endpoint_id(endpoint_id)
    status_url = str(submit.get("status_url") or "").strip() or f"{base}/{status_ep}/requests/{request_id}/status"
    result_url = str(submit.get("response_url") or "").strip() or f"{base}/{status_ep}/requests/{request_id}"

    t0 = time.time()
    last = submit
    rewrote_405 = False

    while True:
        if time.time() - t0 > float(poll_timeout_s):
            raise RuntimeError(f"Timeout waiting for COMPLETED. request_id={request_id} last={last}")
        try:
            st = _http_json("GET", status_url, headers={"Authorization": f"Key {fal_key}"}, body=None, timeout_s=120)
        except Exception as e:
            # if a bad status_url subpath 405s in your environment, rewrite to top-level
            msg = repr(e)
            if ("405" in msg) and not rewrote_405:
                rewrote_405 = True
                status_url = f"{base}/{status_ep}/requests/{request_id}/status"
                result_url = f"{base}/{status_ep}/requests/{request_id}"
                time.sleep(poll_secs)
                continue
            raise

        last = st
        s = str(st.get("status") or "").upper()
        if s == "COMPLETED":
            break
        if s in ("FAILED", "ERROR", "CANCELED", "CANCELLED"):
            raise RuntimeError(f"Fal failed. request_id={request_id} status={s} last={st}")
        time.sleep(poll_secs)

    out = _http_json("GET", result_url, headers={"Authorization": f"Key {fal_key}"}, body=None, timeout_s=180)
    dbg = {
        "request_id": request_id,
        "endpoint_id": endpoint_id,
        "status_endpoint_id": status_ep,
        "status_url": status_url,
        "result_url": result_url,
        "rewrote_405": rewrote_405,
        "last_status": last,
        "elapsed_s": round(time.time() - t0, 3),
    }
    return out, dbg


async def main_async(args) -> None:
    # env
    db_url = _env("DATABASE_URL") or _env("POSTGRES_DSN")
    if not db_url:
        raise RuntimeError("DATABASE_URL (or POSTGRES_DSN) is required")

    fal_key = _env("FAL_KEY") or _env("FAL_API_KEY")
    if not fal_key:
        raise RuntimeError("FAL_KEY (or FAL_API_KEY) is required")

    lora_url = args.lora_url or _env("DF_SAREE_TRAINED_LORA_URL") or _env("COMMERCE_SAREE_TRAINED_LORA_URL")
    if not lora_url:
        raise RuntimeError("Missing LoRA URL. Pass --lora-url or set DF_SAREE_TRAINED_LORA_URL")

    lora_scale = float(args.lora_scale or _env("DF_SAREE_TRAINED_LORA_SCALE", "1.10"))

    acct = _azure_acct_from_conn_str()
    bsc = BlobServiceClient.from_connection_string(acct.conn_str)

    # fetch example
    pool = await asyncpg.create_pool(dsn=db_url, min_size=1, max_size=3)
    try:
        ex = await _fetch_example(pool, args.example_id)
    finally:
        await pool.close()

    person_ref = _as_dict(ex["person_ref"])
    garment_refs = _as_dict(ex["garment_refs"])
    mask_refs = _as_dict(ex["mask_refs"])

    person_container = str(person_ref.get("container") or args.az_container)
    person_blob = str(person_ref.get("blob") or "")
    if not person_blob:
        raise RuntimeError("person_ref.blob missing")

    saree = _as_dict(garment_refs.get("saree"))
    saree_container = str(saree.get("container") or args.az_container)
    saree_blob = str(saree.get("blob") or "")
    if not saree_blob:
        raise RuntimeError("garment_refs.saree.blob missing")

    storage_container = str(mask_refs.get("storage_container") or args.az_container)
    storage_prefix = str(mask_refs.get("storage_prefix") or "").strip().strip("/")
    files = _as_dict(_as_dict(mask_refs.get("manifest_json")).get("files"))
    saree_alpha_rel = str(files.get("saree_alpha") or "masks/saree_alpha.png").lstrip("/")
    saree_alpha_blob = f"{storage_prefix}/{saree_alpha_rel}" if storage_prefix else saree_alpha_rel

    # download blobs (raw bytes)
    person_bytes = bsc.get_blob_client(container=person_container, blob=person_blob).download_blob().readall()
    saree_bytes = bsc.get_blob_client(container=saree_container, blob=saree_blob).download_blob().readall()
    alpha_bytes = bsc.get_blob_client(container=storage_container, blob=saree_alpha_blob).download_blob().readall()

    # normalize to png + create proxy
    person_png = _ensure_png_bytes(person_bytes)
    saree_png = _ensure_png_bytes(saree_bytes)
    alpha_png = _ensure_png_bytes(alpha_bytes)

    tile_png = _make_texture_tile(saree_png, tile_size=args.tile_size, bg_dist_thresh=args.bg_dist_thresh)
    proxy_png = _make_garment_proxy(tile_png, alpha_png, out_size=args.proxy_size)

    # upload proxy to Azure so fal can fetch it via URL
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    proxy_blob = f"{args.proxy_prefix.strip().strip('/')}/{args.example_id}/{ts}/garment_proxy.png"
    bsc.get_blob_client(container=args.az_container, blob=proxy_blob).upload_blob(proxy_png, overwrite=True)

    # build SAS URLs for fal inputs
    person_url = _sas_url(acct, container=person_container, blob=person_blob, hours=args.sas_hours)
    saree_url = _sas_url(acct, container=saree_container, blob=saree_blob, hours=args.sas_hours)
    proxy_url = _sas_url(acct, container=args.az_container, blob=proxy_blob, hours=args.sas_hours)

    print("person_url:", person_url)
    print("saree_url :", saree_url)
    print("proxy_url :", proxy_url)
    print("lora_url  :", lora_url)
    print("lora_scale:", lora_scale)

    prompt = args.prompt or (
        "Photorealistic full-body photo. Drape a traditional Indian saree in nivi style with realistic pleats and pallu. "
        "Use the saree references to match the exact fabric pattern and colors. "
        "Preserve face identity, body shape, pose, lighting, and background. Do not change the person or scene."
    )

    # call fal flux2 lora edit
    endpoint_id = "fal-ai/flux-2/lora/edit"
    payload = {
        "prompt": prompt,
        "image_urls": [person_url, proxy_url, saree_url],
        "loras": [{"path": lora_url, "scale": float(lora_scale)}],
        "num_images": int(args.num_images),
        "output_format": str(args.output_format),
    }

    out, dbg = _fal_submit_and_wait(
        fal_key=fal_key,
        endpoint_id=endpoint_id,
        payload=payload,
        poll_secs=float(args.poll_secs),
        poll_timeout_s=int(args.poll_timeout_s),
    )

    out_url = _parse_first_image_url(_as_dict(out))
    print("\n=== FAL RESULT ===")
    print("debug:", json.dumps(dbg, indent=2)[:2000])
    print("output_url:", out_url)

    # optional mirror result to Azure for stable sharing
    if args.mirror_result:
        img_bytes = urlopen(Request(out_url, headers={"User-Agent": "df-test"}), timeout=240).read()
        img_bytes = _ensure_png_bytes(img_bytes)

        out_blob = f"{args.out_prefix.strip().strip('/')}/{args.example_id}/{ts}/flux2_lora_out.png"
        bsc.get_blob_client(container=args.out_container, blob=out_blob).upload_blob(img_bytes, overwrite=True)
        out_sas = _sas_url(acct, container=args.out_container, blob=out_blob, hours=args.sas_hours)
        print("\n=== MIRRORED TO AZURE ===")
        print("out_blob:", out_blob)
        print("out_sas :", out_sas)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--example-id", required=True, help="training_examples.id (uuid)")

    ap.add_argument("--lora-url", default="", help="Overrides DF_SAREE_TRAINED_LORA_URL")
    ap.add_argument("--lora-scale", default="", help="Overrides DF_SAREE_TRAINED_LORA_SCALE (e.g. 1.10)")

    ap.add_argument("--prompt", default="")
    ap.add_argument("--num-images", type=int, default=1)
    ap.add_argument("--output-format", default="png", choices=["png", "jpeg", "webp"])

    ap.add_argument("--poll-secs", type=float, default=3.0)
    ap.add_argument("--poll-timeout-s", type=int, default=900)

    ap.add_argument("--az-container", default="commerce-training", help="Default container for uploads")
    ap.add_argument("--proxy-prefix", default="training/tmp_proxy", help="Where to upload garment proxy")
    ap.add_argument("--sas-hours", type=int, default=24)

    ap.add_argument("--proxy-size", type=int, default=1024)
    ap.add_argument("--tile-size", type=int, default=1024)
    ap.add_argument("--bg-dist-thresh", type=int, default=26)

    ap.add_argument("--mirror-result", action="store_true")
    ap.add_argument("--out-container", default="commerce-output")
    ap.add_argument("--out-prefix", default="commerce/vton/saree_lora_test")
    args = ap.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()