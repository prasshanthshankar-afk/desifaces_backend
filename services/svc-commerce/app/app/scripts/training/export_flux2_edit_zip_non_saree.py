from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.request import Request, urlopen

import asyncpg
from PIL import Image
from azure.storage.blob import BlobServiceClient, BlobSasPermissions, ContentSettings, generate_blob_sas


# -------------------------
# small helpers
# -------------------------

def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, (bytes, bytearray)):
        try:
            x = x.decode("utf-8", errors="ignore")
        except Exception:
            return {}
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)
    except Exception:
        return {}


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _safe_root(s: str) -> str:
    s = str(s or "")
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", s)
    return s[:64] or "ex"


def _pick_first(*vals: Any) -> str:
    for v in vals:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def _ensure_png_bytes(img_bytes: bytes) -> bytes:
    im = Image.open(io.BytesIO(img_bytes))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _download_http_bytes(url: str, timeout_s: int = 180) -> bytes:
    req = Request(str(url).strip(), headers={"User-Agent": "desifaces-exporter/1.0"})
    with urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _download_any_bytes(bsc: BlobServiceClient, url_or_path: str) -> bytes:
    s = str(url_or_path or "").strip()
    if not s:
        raise RuntimeError("empty url_or_path")

    if s.startswith("az://"):
        rest = s[len("az://") :]
        container, blob = rest.split("/", 1)
        return bsc.get_blob_client(container=container, blob=blob).download_blob().readall()

    if s.startswith("http://") or s.startswith("https://"):
        return _download_http_bytes(s)

    if os.path.exists(s):
        with open(s, "rb") as f:
            return f.read()

    raise RuntimeError(f"unsupported url/path: {s}")


# -------------------------
# azure + sas
# -------------------------

@dataclass
class AzureCtx:
    container: str
    conn_str: str
    account_name: str
    account_key: str


def _azure_ctx_from_conn_str(container: str) -> AzureCtx:
    conn = _env("AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is required")

    parts: Dict[str, str] = {}
    for kv in conn.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.strip()] = v.strip()

    acct = parts.get("AccountName") or ""
    key = parts.get("AccountKey") or ""
    if not acct or not key:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING must include AccountName and AccountKey")

    return AzureCtx(container=container, conn_str=conn, account_name=acct, account_key=key)


def _upload_blob_bytes(bsc: BlobServiceClient, container: str, blob: str, data: bytes, content_type: str) -> None:
    bc = bsc.get_blob_client(container=container, blob=blob)
    bc.upload_blob(
        data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
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


# -------------------------
# db fetch (non-saree)
# -------------------------

async def _fetch_examples(
    pool: asyncpg.Pool,
    dataset_id: str,
    split: str,
    limit: int,
) -> List[Dict[str, Any]]:
    q = """
    select
      id,
      split,
      task,
      person_ref,
      garment_refs,
      conditioning_refs,
      target_ref,
      mask_refs,
      labels_json,
      quality_json,
      created_at
    from training_examples
    where dataset_id = $1
      and split = $2
      and target_ref is not null
    order by created_at asc
    limit $3
    """
    async with pool.acquire() as con:
        rows = await con.fetch(q, dataset_id, split, limit)
    return [dict(r) for r in rows]


def _passes_quality(row: Dict[str, Any], *, require_approved_manual: bool) -> Tuple[bool, Dict[str, Any]]:
    qj = _as_dict(row.get("quality_json"))
    status = str(qj.get("status") or "").strip().lower()
    training_decision = str(qj.get("training_decision") or "").strip().lower()
    accepted = bool(qj.get("accepted"))

    if require_approved_manual:
        ok = training_decision == "approved_manual"
    else:
        ok = training_decision == "approved_manual" or accepted or status in {"accepted", "review"}

    return ok, {
        "status": status,
        "accepted": accepted,
        "training_decision": training_decision,
        "require_approved_manual": require_approved_manual,
    }


# -------------------------
# main
# -------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--split", default="train", choices=["train", "val", "test"])
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--out-container", default="commerce-training")
    ap.add_argument("--out-prefix", default="training/non_saree_manual_approved/flux2_edit_zips")
    ap.add_argument("--sas-hours", type=int, default=48)

    ap.add_argument("--require-approved-manual", action="store_true")
    ap.add_argument(
        "--default-caption",
        default=(
            "Dress the person in traditional Indian non-saree ethnic wear. "
            "Preserve identity, pose, body shape, lighting, and background. "
            "Make the garment look naturally worn and realistic."
        ),
    )
    ap.add_argument(
        "--families",
        default="",
        help="Optional comma-separated family filter, e.g. kurta,salwar_suit",
    )
    args = ap.parse_args()

    db_url = (_env("DATABASE_URL") or _env("POSTGRES_DSN")).strip()
    if not db_url:
        raise RuntimeError("DATABASE_URL (or POSTGRES_DSN) is required")

    allowed_families: Set[str] = {
        x.strip().lower()
        for x in str(args.families or "").split(",")
        if x.strip()
    }

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

        work_dir = tempfile.mkdtemp(prefix=f"df_flux2_edit_non_saree_{args.dataset_id[:8]}_")
        data_dir = os.path.join(work_dir, "data")
        os.makedirs(data_dir, exist_ok=True)

        kept = 0
        rejected = 0
        rej_reasons: Dict[str, int] = {}
        kept_by_family: Dict[str, int] = {}

        for r in rows:
            labels_json = _as_dict(r.get("labels_json"))
            family = str(labels_json.get("seed_name") or labels_json.get("family") or "").strip().lower()

            if allowed_families and family not in allowed_families:
                rejected += 1
                rej_reasons["family_filtered_out"] = rej_reasons.get("family_filtered_out", 0) + 1
                continue

            ok, qdbg = _passes_quality(r, require_approved_manual=bool(args.require_approved_manual))
            if not ok:
                rejected += 1
                key = f"quality(status={qdbg.get('status')},training_decision={qdbg.get('training_decision')})"
                rej_reasons[key] = rej_reasons.get(key, 0) + 1
                continue

            ex_id = str(r["id"])
            root = _safe_root(ex_id.replace("-", "")[:24])

            person_ref = _as_dict(r.get("person_ref"))
            target_ref = _as_dict(r.get("target_ref"))
            garment_refs = _as_dict(r.get("garment_refs"))

            # Prefer stable az:// refs first, then SAS/http fallbacks.
            person_url = _pick_first(
                person_ref.get("catalog_url"),
                person_ref.get("request_url"),
                person_ref.get("url"),
                person_ref.get("human_image_url"),
            )
            garment_url = _pick_first(
                garment_refs.get("dataset_az_url"),
                garment_refs.get("source_image_url"),
                garment_refs.get("raw_image_url"),
            )
            target_url = _pick_first(
                target_ref.get("target_az_url"),
                target_ref.get("original_output_url"),
            )

            if not person_url or not garment_url or not target_url:
                rejected += 1
                rej_reasons["missing_required_urls"] = rej_reasons.get("missing_required_urls", 0) + 1
                continue

            try:
                person_png = _ensure_png_bytes(_download_any_bytes(bsc, person_url))
                garment_png = _ensure_png_bytes(_download_any_bytes(bsc, garment_url))
                target_png = _ensure_png_bytes(_download_any_bytes(bsc, target_url))
            except Exception as e:
                rejected += 1
                key = f"download_or_png_error:{type(e).__name__}"
                rej_reasons[key] = rej_reasons.get(key, 0) + 1
                continue

            start_path = os.path.join(data_dir, f"{root}_start.png")
            start2_path = os.path.join(data_dir, f"{root}_start2.png")
            end_path = os.path.join(data_dir, f"{root}_end.png")
            txt_path = os.path.join(data_dir, f"{root}.txt")

            with open(start_path, "wb") as f:
                f.write(person_png)

            with open(start2_path, "wb") as f:
                f.write(garment_png)

            with open(end_path, "wb") as f:
                f.write(target_png)

            family_text = family.replace("_", " ").strip() or "Indian ethnic wear"
            caption = f"{args.default_caption} Family: {family_text}."

            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(caption)

            kept += 1
            kept_by_family[family_text] = kept_by_family.get(family_text, 0) + 1

        if kept <= 0:
            raise RuntimeError(f"All examples rejected. rejected={rejected} reasons={rej_reasons}")

        zip_name = f"non_saree_flux2_edit_{args.dataset_id[:8]}_{args.split}_{kept}.zip"
        zip_path = os.path.join(work_dir, zip_name)

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for fn in sorted(os.listdir(data_dir)):
                z.write(os.path.join(data_dir, fn), arcname=fn)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        zip_blob = f"{args.out_prefix.strip().strip('/')}/{args.dataset_id}/{ts}/{zip_name}"

        with open(zip_path, "rb") as f:
            _upload_blob_bytes(bsc, az.container, zip_blob, f.read(), "application/zip")

        zip_sas_url = _sas_url_for_blob(az, zip_blob, hours=int(args.sas_hours))

        summary = {
            "dataset_id": args.dataset_id,
            "split": args.split,
            "limit": args.limit,
            "require_approved_manual": bool(args.require_approved_manual),
            "allowed_families": sorted(allowed_families),
            "kept": kept,
            "kept_by_family": kept_by_family,
            "rejected": rejected,
            "rejection_reasons": rej_reasons,
            "zip": {
                "container": az.container,
                "blob": zip_blob,
                "sas_url": zip_sas_url,
            },
        }

        with open(os.path.join(work_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        print(json.dumps(summary, indent=2))
        print("\nZIP_SAS_URL=" + zip_sas_url)
        print("WORK_DIR=" + work_dir)

    asyncio.run(_run())


if __name__ == "__main__":
    main()