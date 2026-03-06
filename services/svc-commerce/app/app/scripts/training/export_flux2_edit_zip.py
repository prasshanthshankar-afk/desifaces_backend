# services/svc-commerce/app/app/scripts/training/export_flux2_edit_zip.py
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
from PIL import Image, ImageOps
from azure.storage.blob import BlobServiceClient
from azure.storage.blob import generate_blob_sas, BlobSasPermissions


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _jget(d: Dict[str, Any], *path: str) -> Any:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _safe_root(s: str) -> str:
    s = str(s or "")
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", s)
    return s[:48] or "ex"


def _ensure_png_bytes(img_bytes: bytes) -> bytes:
    im = Image.open(io.BytesIO(img_bytes))
    # keep alpha if present
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _prepare_texture_tile_from_png_bytes(
    saree_png_bytes: bytes, *, tile_size: int = 1024, bg_dist_thresh: int = 26
) -> bytes:
    """
    Deterministically create a repeatable fabric tile from the saree product image.
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

    if xmax <= xmin or ymax <= ymin or fg_count < 50:
        # fallback to central crop
        cx0 = int(w0 * 0.2)
        cy0 = int(h0 * 0.2)
        cx1 = int(w0 * 0.8)
        cy1 = int(h0 * 0.8)
        crop = im0.crop((cx0, cy0, cx1, cy1))
    else:
        sx = w0 / float(w_small)
        sy = h0 / float(h_small)
        pad = 0.06
        x0 = max(0, int((xmin - pad * w_small) * sx))
        y0 = max(0, int((ymin - pad * h_small) * sy))
        x1 = min(w0, int((xmax + pad * w_small) * sx))
        y1 = min(h0, int((ymax + pad * h_small) * sy))
        crop = im0.crop((x0, y0, x1, y1))

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
    buf = io.BytesIO()
    tile.save(buf, format="PNG")
    return buf.getvalue()


def _make_garment_proxy(
    tile_png_bytes: bytes, mask_png_bytes: bytes, *, out_size: int = 1024
) -> bytes:
    """
    Garment proxy = tiled texture inside saree_alpha silhouette (alpha channel).
    Output is RGBA PNG transparent background.
    """
    tile = Image.open(io.BytesIO(tile_png_bytes)).convert("RGB")
    mask = Image.open(io.BytesIO(mask_png_bytes)).convert("RGBA").resize((out_size, out_size), resample=Image.BILINEAR)
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


@dataclass
class AzureCtx:
    container: str
    conn_str: str
    account_name: str
    account_key: str


def _azure_ctx_from_conn_str(container: str) -> AzureCtx:
    conn = (os.getenv("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
    if not conn:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is required")

    # Parse minimal AccountName/AccountKey for SAS generation
    parts = {}
    for kv in conn.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.strip()] = v.strip()
    acct = parts.get("AccountName")
    key = parts.get("AccountKey")
    if not acct or not key:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING must include AccountName and AccountKey")
    return AzureCtx(container=container, conn_str=conn, account_name=acct, account_key=key)


def _download_blob_bytes(bsc: BlobServiceClient, container: str, blob: str) -> bytes:
    bc = bsc.get_blob_client(container=container, blob=blob)
    return bc.download_blob().readall()


def _upload_blob_bytes(bsc: BlobServiceClient, container: str, blob: str, data: bytes, content_type: str) -> None:
    bc = bsc.get_blob_client(container=container, blob=blob)
    bc.upload_blob(
        data,
        overwrite=True,
        content_settings={"content_type": content_type},  # type: ignore
    )


def _sas_url_for_blob(ctx: AzureCtx, blob: str, *, hours: int = 24) -> str:
    expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
    sas = generate_blob_sas(
        account_name=ctx.account_name,
        account_key=ctx.account_key,
        container_name=ctx.container,
        blob_name=blob,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
    )
    return f"https://{ctx.account_name}.blob.core.windows.net/{ctx.container}/{blob}?{sas}"


async def _fetch_examples(
    pool: asyncpg.Pool,
    dataset_id: str,
    split: str,
    limit: int,
) -> List[Dict[str, Any]]:
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
    where dataset_id = $1
      and split = $2
      and task = 'saree_tryon'
      and target_ref is not null
    order by created_at asc
    limit $3
    """
    async with pool.acquire() as con:
        rows = await con.fetch(q, dataset_id, split, limit)
    return [dict(r) for r in rows]


def _passes_quality(row: Dict[str, Any], *, min_saree: float, min_pallu: float) -> Tuple[bool, Dict[str, Any]]:
    qj = _as_dict(row.get("quality_json"))
    dbg = _as_dict(qj.get("debug"))
    saree_cov = float(dbg.get("saree_coverage") or 0.0)
    pallu_cov = float(dbg.get("pallu_coverage") or 0.0)
    ok = (saree_cov >= min_saree) and (pallu_cov >= min_pallu)
    return ok, {"saree_coverage": saree_cov, "pallu_coverage": pallu_cov, "min_saree": min_saree, "min_pallu": min_pallu}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--split", default="train", choices=["train", "val", "test"])
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--out-container", default="commerce-training")
    ap.add_argument("--out-prefix", default="training/flux2_edit_zips")
    ap.add_argument("--sas-hours", type=int, default=48)

    ap.add_argument("--min-saree-coverage", type=float, default=0.30)
    ap.add_argument("--min-pallu-coverage", type=float, default=0.06)

    ap.add_argument("--proxy-size", type=int, default=1024)
    ap.add_argument("--tile-size", type=int, default=1024)
    ap.add_argument("--bg-dist-thresh", type=int, default=26)
    args = ap.parse_args()

    db_url = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN") or "").strip()
    if not db_url:
        raise RuntimeError("DATABASE_URL (or POSTGRES_DSN) is required")

    az = _azure_ctx_from_conn_str(args.out_container)
    bsc = BlobServiceClient.from_connection_string(az.conn_str)

    async def _run() -> None:
        pool = await asyncpg.create_pool(dsn=db_url, min_size=1, max_size=5)
        try:
            rows = await _fetch_examples(pool, args.dataset_id, args.split, args.limit)
        finally:
            await pool.close()

        if not rows:
            raise RuntimeError("No training_examples found for dataset/split")

        # Determine mask blob for saree_alpha from the first row's mask_refs
        mr = _as_dict(rows[0].get("mask_refs"))
        storage_container = str(mr.get("storage_container") or az.container)
        storage_prefix = str(mr.get("storage_prefix") or "").strip().strip("/")
        files = _as_dict(_as_dict(mr.get("manifest_json")).get("files"))
        saree_alpha_rel = str(files.get("saree_alpha") or "masks/saree_alpha.png").strip().lstrip("/")
        saree_alpha_blob = f"{storage_prefix}/{saree_alpha_rel}" if storage_prefix else saree_alpha_rel

        mask_bytes = _download_blob_bytes(bsc, storage_container, saree_alpha_blob)

        work_dir = tempfile.mkdtemp(prefix=f"df_flux2_edit_{args.dataset_id[:8]}_")
        data_dir = os.path.join(work_dir, "data")
        os.makedirs(data_dir, exist_ok=True)

        kept = 0
        rejected = 0
        rej_reasons: Dict[str, int] = {}

        caption = (
            "Drape a traditional Indian saree in nivi style with realistic pleats and pallu. "
            "Use the fabric reference to match textile pattern and colors. "
            "Preserve face identity, body shape, pose, lighting, and background. Do not change the person."
        )

        for r in rows:
            ok, qdbg = _passes_quality(r, min_saree=args.min_saree_coverage, min_pallu=args.min_pallu_coverage)
            if not ok:
                rejected += 1
                key = f"quality(saree<{args.min_saree_coverage} or pallu<{args.min_pallu_coverage})"
                rej_reasons[key] = rej_reasons.get(key, 0) + 1
                continue

            ex_id = str(r["id"])
            root = _safe_root(ex_id.replace("-", "")[:24])

            person_ref = _as_dict(r.get("person_ref"))
            target_ref = _as_dict(r.get("target_ref"))
            garment_refs = _as_dict(r.get("garment_refs"))
            cond_refs = _as_dict(r.get("conditioning_refs"))

            # Use container+blob (NOT url), because url SAS may be expired
            p_container = str(person_ref.get("container") or storage_container)
            p_blob = str(person_ref.get("blob") or "")
            t_container = str(target_ref.get("container") or storage_container)
            t_blob = str(target_ref.get("blob") or "")

            saree = _as_dict(garment_refs.get("saree"))
            s_container = str(saree.get("container") or storage_container)
            s_blob = str(saree.get("blob") or "")

            composite = _as_dict(cond_refs.get("composite"))
            c_container = str(composite.get("container") or storage_container)
            c_blob = str(composite.get("blob") or "")

            if not p_blob or not t_blob or not s_blob:
                rejected += 1
                rej_reasons["missing_blob"] = rej_reasons.get("missing_blob", 0) + 1
                continue

            # Download bytes
            person_bytes = _download_blob_bytes(bsc, p_container, p_blob)
            target_bytes = _download_blob_bytes(bsc, t_container, t_blob)
            saree_bytes = _download_blob_bytes(bsc, s_container, s_blob)

            # Normalize to png (safer)
            person_png = _ensure_png_bytes(person_bytes)
            target_png = _ensure_png_bytes(target_bytes)
            saree_png = _ensure_png_bytes(saree_bytes)

            # Build proxy start2
            tile_png = _prepare_texture_tile_from_png_bytes(
                saree_png, tile_size=args.tile_size, bg_dist_thresh=args.bg_dist_thresh
            )
            proxy_png = _make_garment_proxy(tile_png, mask_bytes, out_size=args.proxy_size)

            # Optional start4 (composite if present)
            composite_png: Optional[bytes] = None
            if c_blob:
                try:
                    comp_bytes = _download_blob_bytes(bsc, c_container, c_blob)
                    composite_png = _ensure_png_bytes(comp_bytes)
                except Exception:
                    composite_png = None

            # Write files (flat layout at zip root)
            def w(fn: str, b: bytes) -> None:
                with open(os.path.join(data_dir, fn), "wb") as f:
                    f.write(b)

            w(f"{root}_start.png", person_png)          # start
            w(f"{root}_start2.png", proxy_png)          # garment proxy (wearable silhouette)
            w(f"{root}_start3.png", saree_png)          # raw saree product image
            if composite_png:
                w(f"{root}_start4.png", composite_png)  # optional conditioning composite
            w(f"{root}_end.png", target_png)            # end/target

            with open(os.path.join(data_dir, f"{root}.txt"), "w") as f:
                f.write(caption)

            kept += 1

        if kept <= 0:
            raise RuntimeError(f"All examples rejected. rejected={rejected} reasons={rej_reasons}")

        # Zip it
        zip_name = f"saree_flux2_edit_{args.dataset_id[:8]}_{args.split}_{kept}.zip"
        zip_path = os.path.join(work_dir, zip_name)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for fn in os.listdir(data_dir):
                z.write(os.path.join(data_dir, fn), arcname=fn)

        # Upload zip to Azure
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        zip_blob = f"{args.out_prefix.strip().strip('/')}/{args.dataset_id}/{ts}/{zip_name}"
        with open(zip_path, "rb") as f:
            _upload_blob_bytes(bsc, az.container, zip_blob, f.read(), "application/zip")

        zip_sas_url = _sas_url_for_blob(az, zip_blob, hours=int(args.sas_hours))

        summary = {
            "dataset_id": args.dataset_id,
            "split": args.split,
            "limit": args.limit,
            "kept": kept,
            "rejected": rejected,
            "rejection_reasons": rej_reasons,
            "mask": {
                "storage_container": storage_container,
                "saree_alpha_blob": saree_alpha_blob,
            },
            "zip": {
                "container": az.container,
                "blob": zip_blob,
                "sas_url": zip_sas_url,
            },
        }
        with open(os.path.join(work_dir, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)

        print(json.dumps(summary, indent=2))
        print("\nZIP_SAS_URL=" + zip_sas_url)
        print("WORK_DIR=" + work_dir)

    asyncio.run(_run())


if __name__ == "__main__":
    main()