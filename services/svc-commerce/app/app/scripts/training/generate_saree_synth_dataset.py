from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import math
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from PIL import Image

from app.db import get_pool
from app.services.azure_storage_service import AzureStorageConfig, AzureStorageService


# -----------------------------
# env + hashing
# -----------------------------
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int((_env(name) or "").strip() or default)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float((_env(name) or "").strip() or default)
    except Exception:
        return default


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ceil_mp_1024(w: int, h: int) -> int:
    # BFL typically uses 1024x1024 = 1MP rounding convention
    return int(math.ceil((float(w) * float(h)) / (1024.0 * 1024.0)))


def _estimate_flux2_pro_edit_usd(mp_in: int, mp_out: int) -> float:
    """
    Rough estimator (ballpark):
      - ref image(s): ~$0.015 / MP (rounded up)
      - output image: ~$0.03 for first MP + ~$0.015 per additional MP (rounded up)
    """
    mp_in = max(1, int(mp_in))
    mp_out = max(1, int(mp_out))
    return 0.015 * mp_in + 0.03 + 0.015 * max(0, mp_out - 1)


# -----------------------------
# HTTP helpers (Fal + downloads)
# -----------------------------
def _http_json(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    data: Optional[Dict[str, Any]] = None,
    timeout_s: int = 180,
) -> Dict[str, Any]:
    m = (method or "GET").strip().upper()
    hdrs: Dict[str, str] = {"Accept": "application/json"}
    hdrs.update(headers or {})

    body: Optional[bytes] = None
    if m not in ("GET", "HEAD") and data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = Request(url=url, method=m, headers=hdrs, data=body)

    try:
        with urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read() or b""
            txt = raw.decode("utf-8", errors="replace").strip()
            if not txt:
                return {}
            out = json.loads(txt)
            return out if isinstance(out, dict) else {"raw": out}
    except HTTPError as e:
        raw = b""
        try:
            raw = e.read() or b""
        except Exception:
            pass
        txt = raw.decode("utf-8", errors="replace").strip() if raw else str(e)
        raise RuntimeError(f"HTTPError code={e.code} url={url} body={txt[:800]}") from e
    except URLError as e:
        raise RuntimeError(f"URLError url={url} err={e}") from e


def _parse_az_ref(s: str) -> Optional[Tuple[str, str]]:
    # az://container/blob/path.json
    s = (s or "").strip()
    if not s.startswith("az://"):
        return None
    rest = s[len("az://") :]
    if "/" not in rest:
        return None
    container, blob = rest.split("/", 1)
    container = container.strip()
    blob = blob.strip()
    if not container or not blob:
        return None
    return container, blob


def _download_bytes(url: str, timeout_s: int = 180) -> bytes:
    req = Request(url=url, method="GET", headers={"User-Agent": "desifaces-svc-commerce/1.0"})
    with urlopen(req, timeout=timeout_s) as resp:
        return resp.read() or b""


def _fal_key() -> str:
    return (_env("FAL_KEY") or _env("FAL_API_KEY") or _env("COMMERCE_FAL_KEY")).strip()


def _parse_fal_images(out: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    Returns [(url, content_type), ...] from Fal output.
    Common schema: images[].url + images[].content_type
    """
    urls: List[Tuple[str, str]] = []
    imgs = out.get("images")
    if isinstance(imgs, list):
        for it in imgs:
            if isinstance(it, dict):
                u = it.get("url")
                ct = it.get("content_type") or "image/png"
                if isinstance(u, str) and u.startswith("http"):
                    urls.append((u, str(ct)))
    return urls


def _as_dict_loose(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            j = json.loads(s)
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}
    try:
        if hasattr(x, "items"):
            return dict(x.items())  # type: ignore[arg-type]
    except Exception:
        pass
    try:
        return dict(x)  # fallback
    except Exception:
        return {}


def _is_content_policy_violation(err: Exception) -> bool:
    s = str(err)
    return ("content_policy_violation" in s) or ("flagged by a content checker" in s)


def _safe_prompt_variant(prompt: str) -> str:
    """
    Policy-safer rewrite: keep saree intent but explicitly modest, fully clothed.
    Avoids false positives on some composites.
    """
    return (
        "Photorealistic adult South Asian fashion model wearing a traditional, modest nivi saree with matching blouse and petticoat. "
        "Correct saree folds and pallu drape. Fully clothed, conservative styling. Studio lighting, neutral background. "
        "No nudity, no lingerie, no cleavage, no transparent fabric. No text, no watermark."
    )


async def _fal_run_and_wait(
    *,
    base_url: str,
    model_id: str,
    input_json: Dict[str, Any],
    fal_key: str,
    poll_secs: float,
    poll_timeout_s: int,
    http_timeout_s: int,
    lifecycle_seconds: int = 7200,
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Key {fal_key}",
        "X-Fal-Object-Lifecycle-Preference": json.dumps({"expiration_duration_seconds": lifecycle_seconds}),
    }

    post_url = f"{base_url.rstrip('/')}/{model_id.strip('/')}"
    submit = await asyncio.to_thread(
        _http_json, "POST", post_url, headers=headers, data=input_json, timeout_s=http_timeout_s
    )

    request_id = str(submit.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError(f"fal queue missing request_id. submit={submit}")

    status_url = str(submit.get("status_url") or "").strip()
    result_url = str(submit.get("response_url") or "").strip()
    if not status_url.startswith("http"):
        status_url = f"{base_url.rstrip('/')}/{model_id.strip('/')}/requests/{request_id}/status"
    if not result_url.startswith("http"):
        result_url = f"{base_url.rstrip('/')}/{model_id.strip('/')}/requests/{request_id}"

    t0 = time.time()
    while True:
        st = await asyncio.to_thread(_http_json, "GET", status_url, headers=headers, data=None, timeout_s=http_timeout_s)
        s = str(st.get("status") or "").upper()
        if s == "COMPLETED":
            break
        if time.time() - t0 > float(poll_timeout_s):
            raise RuntimeError(f"fal queue timeout waiting COMPLETED. request_id={request_id} last={st}")
        await asyncio.sleep(poll_secs)

    out = await asyncio.to_thread(_http_json, "GET", result_url, headers=headers, data=None, timeout_s=http_timeout_s)
    return out if isinstance(out, dict) else {"raw": out}


def _guess_ext_from_bytes(b: bytes) -> str:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return "jpg"


# -----------------------------
# Azure upload wrapper (robust)
# -----------------------------
def _upload_bytes(
    storage: AzureStorageService,
    *,
    data: bytes,
    blob_name: str,
    content_type: str,
    container_name: str,
) -> str:
    """
    Matches signature used elsewhere:
      storage.upload_bytes(data=..., blob_name=..., content_type=..., container_name=...)
    """
    fn = getattr(storage, "upload_bytes", None)
    if fn is None:
        raise RuntimeError("AzureStorageService.upload_bytes not found")
    sas_url = fn(data=data, blob_name=blob_name, content_type=content_type, container_name=container_name)
    if not isinstance(sas_url, str) or not sas_url.startswith("http"):
        raise RuntimeError(f"upload_bytes did not return a URL: {sas_url!r}")
    return sas_url


# -----------------------------
# pool parsing / rehosting
# -----------------------------
def _fetch_blob_bytes_azure_sdk(connection_string: str, *, container: str, blob: str) -> bytes:
    try:
        from azure.storage.blob import BlobServiceClient  # type: ignore
    except Exception as e:
        raise RuntimeError("Missing dependency: azure-storage-blob") from e

    bsc = BlobServiceClient.from_connection_string(connection_string)
    bc = bsc.get_blob_client(container=container, blob=blob)
    stream = bc.download_blob()
    return stream.readall()


def _load_pool(path_or_url: str) -> List[Dict[str, Any]]:
    """
    Loads pool list from:
      - local file path
      - http(s) URL
      - az://<container>/<blob>.json
    """
    s = (path_or_url or "").strip()
    if not s:
        return []

    az = _parse_az_ref(s)
    if az is not None:
        conn = _env("AZURE_STORAGE_CONNECTION_STRING") or _env("COMMERCE_AZURE_STORAGE_CONNECTION_STRING")
        if not conn:
            raise RuntimeError(
                "Missing AZURE_STORAGE_CONNECTION_STRING (or COMMERCE_AZURE_STORAGE_CONNECTION_STRING) for az:// pool refs"
            )
        container, blob = az
        raw_bytes = _fetch_blob_bytes_azure_sdk(conn, container=container, blob=blob)
        raw = raw_bytes.decode("utf-8", errors="replace")
        data = json.loads(raw)

    elif s.startswith("http://") or s.startswith("https://"):
        raw_bytes = _download_bytes(s, timeout_s=180)
        raw = raw_bytes.decode("utf-8", errors="replace")
        data = json.loads(raw)

    else:
        data = json.loads(open(s, "r", encoding="utf-8").read())

    out: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for it in data:
            if isinstance(it, str):
                out.append({"url": it})
            elif isinstance(it, dict):
                d = dict(it)
                if "url" not in d and "href" in d:
                    d["url"] = d["href"]
                out.append(d)
    return out


async def _rehydrate_pool_items(
    storage: AzureStorageService,
    *,
    items: List[Dict[str, Any]],
    dataset_container: str,
    dataset_prefix: str,
    kind: str,
    concurrency: int,
) -> List[Dict[str, Any]]:
    """
    Ensures every item has stable {container, blob, url} under dataset_prefix,
    so SAS expiry of bootstrap URLs won't break later.
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    out = [dict(x) for x in items]
    cache: Dict[str, Dict[str, Any]] = {}

    async def one(i: int) -> None:
        async with sem:
            it = out[i]
            if it.get("container") and it.get("blob") and it.get("url"):
                return

            src_url = str(it.get("url") or "").strip()
            if not src_url.startswith("http"):
                raise RuntimeError(f"pool item missing url: {it}")

            if src_url in cache:
                out[i] = dict(cache[src_url])
                return

            b = await asyncio.to_thread(_download_bytes, src_url, 180)
            if not b or len(b) < 2048:
                raise RuntimeError(f"download too small/empty kind={kind} url={src_url[:80]}")

            ext = _guess_ext_from_bytes(b)
            h = _sha256_bytes(b)[:16]
            blob = f"{dataset_prefix.rstrip('/')}/inputs/{kind}/{h}.{ext}"
            url = _upload_bytes(
                storage, data=b, blob_name=blob, content_type=f"image/{ext}", container_name=dataset_container
            )

            rec = {
                "container": dataset_container,
                "blob": blob,
                "url": url,
                "bytes": len(b),
                "sha256": _sha256_bytes(b),
                "src_url": src_url,
            }
            cache[src_url] = rec
            out[i] = dict(rec)

    await asyncio.gather(*[one(i) for i in range(len(out))])
    return out


# -----------------------------
# Template loading (from Azure blobs)
# -----------------------------
@dataclass
class TemplateAssets:
    template_id: str
    storage_container: str
    storage_prefix: str
    manifest_json: Dict[str, Any]
    saree_alpha: Image.Image
    pallu_alpha: Optional[Image.Image]
    blouse_alpha: Optional[Image.Image]
    occlusion: Optional[Image.Image]


def _open_rgba(b: bytes) -> Image.Image:
    return Image.open(io.BytesIO(b)).convert("RGBA")


def _alpha_channel(img: Image.Image) -> Image.Image:
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    return img.split()[-1]


async def _load_template_assets(
    *,
    connection_string: str,
    template_id: str,
    storage_container: str,
    storage_prefix: str,
    manifest_json: Dict[str, Any],
) -> TemplateAssets:
    files = (_as_dict_loose(manifest_json) or {}).get("files") or {}

    def rel(name: str, default_rel: str) -> str:
        r = str(files.get(name) or default_rel).lstrip("/")
        return f"{storage_prefix.rstrip('/')}/{r}"

    async def load_rgba(blob: str) -> Image.Image:
        b = await asyncio.to_thread(
            _fetch_blob_bytes_azure_sdk, connection_string, container=storage_container, blob=blob
        )
        return _open_rgba(b)

    async def load_opt(blob: str) -> Optional[Image.Image]:
        try:
            return await load_rgba(blob)
        except Exception:
            return None

    saree_alpha = await load_rgba(rel("saree_alpha", "masks/saree_alpha.png"))
    pallu_alpha = await load_opt(rel("pallu_alpha", "masks/pallu_alpha.png"))
    blouse_alpha = await load_opt(rel("blouse_alpha", "masks/blouse_alpha.png"))
    occlusion = await load_opt(rel("occlusion", "masks/occlusion.png"))

    return TemplateAssets(
        template_id=template_id,
        storage_container=storage_container,
        storage_prefix=storage_prefix,
        manifest_json=_as_dict_loose(manifest_json),
        saree_alpha=saree_alpha,
        pallu_alpha=pallu_alpha,
        blouse_alpha=blouse_alpha,
        occlusion=occlusion,
    )


# -----------------------------
# Compositing (mask-based; UV optional later)
# -----------------------------
def _fit(img: Image.Image, size: Tuple[int, int]) -> Image.Image:
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    if img.size != size:
        img = img.resize(size, resample=Image.BILINEAR)
    return img


def _apply_mask_alpha(mask_rgba: Image.Image, *, occlusion_rgba: Optional[Image.Image]) -> Image.Image:
    """
    Returns an 'L' alpha mask after subtracting occlusion (if provided).
    """
    a = _alpha_channel(mask_rgba)

    if occlusion_rgba is None:
        return a

    occ_a = _alpha_channel(occlusion_rgba)
    a_px = a.load()
    o_px = occ_a.load()
    w, h = a.size
    out = Image.new("L", (w, h))
    out_px = out.load()
    for y in range(h):
        for x in range(w):
            av = int(a_px[x, y])
            ov = int(o_px[x, y])
            out_px[x, y] = int(av * (1.0 - (ov / 255.0)))
    return out


def _layer_from_texture(texture: Image.Image, *, alpha_mask_l: Image.Image, size: Tuple[int, int]) -> Image.Image:
    tex = _fit(texture, size)
    layer = tex.copy()
    layer.putalpha(alpha_mask_l)
    return layer


def _mask_coverage(alpha_l: Image.Image) -> float:
    try:
        px = alpha_l.get_flattened_data()
    except AttributeError:
        px = alpha_l.getdata()
        
    on = sum(1 for v in px if v > 10)
    total = len(px) or 1
    return float(on) / float(total)


def compose_tryon_composite(
    *,
    person_rgba: Image.Image,
    saree_texture: Image.Image,
    pallu_texture: Optional[Image.Image],
    blouse_texture: Optional[Image.Image],
    tpl: TemplateAssets,
) -> Tuple[Image.Image, Dict[str, Any]]:
    base = person_rgba.convert("RGBA")
    size = base.size

    saree_alpha = _fit(tpl.saree_alpha, size)
    pallu_alpha = _fit(tpl.pallu_alpha, size) if tpl.pallu_alpha else None
    blouse_alpha = _fit(tpl.blouse_alpha, size) if tpl.blouse_alpha else None
    occlusion = _fit(tpl.occlusion, size) if tpl.occlusion else None

    # Use saree texture for pallu if pallu_texture is missing
    pallu_texture = pallu_texture or saree_texture

    a_saree = _apply_mask_alpha(saree_alpha, occlusion_rgba=occlusion)
    layer_saree = _layer_from_texture(saree_texture, alpha_mask_l=a_saree, size=size)
    out = Image.alpha_composite(base, layer_saree)

    cov_pallu = None
    if pallu_alpha is not None:
        a_pallu = _apply_mask_alpha(pallu_alpha, occlusion_rgba=occlusion)
        layer_pallu = _layer_from_texture(pallu_texture, alpha_mask_l=a_pallu, size=size)
        out = Image.alpha_composite(out, layer_pallu)
        cov_pallu = _mask_coverage(a_pallu)

    cov_blouse = None
    if blouse_alpha is not None and blouse_texture is not None:
        a_blouse = _apply_mask_alpha(blouse_alpha, occlusion_rgba=occlusion)
        layer_blouse = _layer_from_texture(blouse_texture, alpha_mask_l=a_blouse, size=size)
        out = Image.alpha_composite(out, layer_blouse)
        cov_blouse = _mask_coverage(a_blouse)

    debug = {
        "size": {"w": size[0], "h": size[1]},
        "saree_coverage": _mask_coverage(a_saree),
        "pallu_coverage": cov_pallu,
        "blouse_coverage": cov_blouse,
        "has_occlusion": bool(occlusion is not None),
        "template_id": tpl.template_id,
    }
    return out, debug


# -----------------------------
# DB helpers (match your DDL)
# -----------------------------
async def db_insert_dataset(
    *,
    dataset_id: str,
    name: str,
    kind: str,
    usage_scope: str,
    license_name: Optional[str],
    license_url: Optional[str],
    storage_container: str,
    storage_prefix: str,
    recipe_json: Dict[str, Any],
    created_by: Optional[str],
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into training_datasets(
              id, name, kind, usage_scope, license_name, license_url,
              storage_container, storage_prefix, recipe_json, stats_json, is_frozen, created_by
            )
            values(
              $1::uuid, $2, $3, $4, $5, $6,
              $7, $8, $9::jsonb, '{}'::jsonb, false, $10::uuid
            )
            """,
            dataset_id,
            name,
            kind,
            usage_scope,
            license_name,
            license_url,
            storage_container,
            storage_prefix,
            json.dumps(recipe_json),
            created_by,
        )


async def db_update_dataset_stats(dataset_id: str, stats_json: Dict[str, Any]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "update training_datasets set stats_json=$2::jsonb, updated_at=now() where id=$1::uuid",
            dataset_id,
            json.dumps(stats_json),
        )


async def db_fetch_templates(drape_style: str, only_active: bool) -> List[Dict[str, Any]]:
    pool = await get_pool()
    where = "garment_type='saree' and drape_style=$1 and status='active'"
    if only_active:
        where += " and is_active=true"

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            select
              id::text as id,
              storage_container,
              storage_prefix,
              manifest_json
            from drape_templates
            where {where}
            order by is_active desc, version desc
            """,
            drape_style,
        )

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "storage_container": r["storage_container"],
                "storage_prefix": r["storage_prefix"],
                "manifest_json": _as_dict_loose(r["manifest_json"]),
            }
        )
    return out


async def db_insert_example(
    *,
    dataset_id: str,
    template_id: str,
    split: str,
    task: str,
    person_ref: Dict[str, Any],
    garment_refs: Dict[str, Any],
    conditioning_refs: Dict[str, Any],
    target_ref: Dict[str, Any],
    mask_refs: Dict[str, Any],
    labels_json: Dict[str, Any],
    quality_json: Dict[str, Any],
    consent_json: Dict[str, Any],
    dedup_hash: str,
    sha256_json: Dict[str, Any],
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into training_examples(
              dataset_id, template_id, split, task,
              person_ref, garment_refs, conditioning_refs,
              target_ref, mask_refs, labels_json,
              quality_json, consent_json,
              dedup_hash, sha256_json
            )
            values(
              $1::uuid, $2::uuid, $3, $4,
              $5::jsonb, $6::jsonb, $7::jsonb,
              $8::jsonb, $9::jsonb, $10::jsonb,
              $11::jsonb, $12::jsonb,
              $13, $14::jsonb
            )
            on conflict (dataset_id, dedup_hash) do nothing
            """,
            dataset_id,
            template_id,
            split,
            task,
            json.dumps(person_ref),
            json.dumps(garment_refs),
            json.dumps(conditioning_refs),
            json.dumps(target_ref),
            json.dumps(mask_refs),
            json.dumps(labels_json),
            json.dumps(quality_json),
            json.dumps(consent_json),
            dedup_hash,
            json.dumps(sha256_json),
        )


# -----------------------------
# split helper
# -----------------------------
def split_from_hash(h: str, train_pct: int, val_pct: int) -> str:
    n = int(h[:8], 16) % 100
    if n < train_pct:
        return "train"
    if n < train_pct + val_pct:
        return "val"
    return "test"


# -----------------------------
# main
# -----------------------------
async def main() -> int:
    ap = argparse.ArgumentParser()

    # dataset identity
    ap.add_argument("--dataset_name", required=True)
    ap.add_argument("--dataset_kind", default=_env("DF_DATASET_KIND", "synthetic"))
    ap.add_argument("--usage_scope", default=_env("DF_DATASET_SCOPE", "commercial_ok"))
    ap.add_argument("--license_name", default=_env("DF_DATASET_LICENSE_NAME", ""))
    ap.add_argument("--license_url", default=_env("DF_DATASET_LICENSE_URL", ""))
    ap.add_argument("--created_by", default=_env("DF_CREATED_BY", ""))  # UUID string optional

    # templates
    ap.add_argument("--drape_style", default=_env("DF_DRAPE_STYLE", "nivi"))
    ap.add_argument(
        "--only_active_template",
        default=_env_bool("DF_ONLY_ACTIVE_TEMPLATE", True),
        action=argparse.BooleanOptionalAction,
    )

    # pools
    ap.add_argument("--persons_json", required=True)
    ap.add_argument("--sarees_json", required=True)
    ap.add_argument("--blouses_json", default="")
    ap.add_argument("--pallus_json", default="")
    ap.add_argument("--num_examples", type=int, default=_env_int("DF_NUM_EXAMPLES", 2000))
    ap.add_argument("--seed", type=int, default=_env_int("DF_DATASET_SEED", 123))
    ap.add_argument("--concurrency", type=int, default=_env_int("DF_DATASET_CONCURRENCY", 4))

    # dataset storage
    ap.add_argument("--storage_container", default=_env("DF_TRAINING_CONTAINER", "commerce-training"))
    ap.add_argument("--storage_prefix", default=_env("DF_TRAINING_PREFIX", ""))
    ap.add_argument(
        "--rehydrate_inputs",
        default=_env_bool("DF_REHYDRATE_INPUTS", True),
        action=argparse.BooleanOptionalAction,
    )

    # refine step
    ap.add_argument(
        "--enable_refine",
        default=_env_bool("DF_ENABLE_REFINE", True),
        action=argparse.BooleanOptionalAction,
    )
    ap.add_argument("--fal_base_url", default=_env("COMMERCE_FAL_BASE_URL", "https://queue.fal.run"))
    ap.add_argument("--i2i_model_id", default=_env("DF_TRAIN_I2I_MODEL_ID", "fal-ai/flux-2-pro/edit"))
    ap.add_argument("--refine_image_size", default=_env("DF_TRAIN_REFINE_IMAGE_SIZE", "portrait_4_3"))
    ap.add_argument("--refine_output_format", default=_env("DF_TRAIN_REFINE_OUTPUT_FORMAT", "png"))
    ap.add_argument("--refine_prompt", default=_env("DF_TRAIN_REFINE_PROMPT", ""))
    ap.add_argument("--poll_secs", type=float, default=_env_float("DF_TRAIN_POLL_SECS", 1.2))
    ap.add_argument("--poll_timeout_s", type=int, default=int(_env_float("DF_TRAIN_POLL_TIMEOUT_S", 900)))
    ap.add_argument("--http_timeout_s", type=int, default=int(_env_float("DF_TRAIN_HTTP_TIMEOUT_S", 180)))

    # optional Fal knobs
    ap.add_argument("--safety_tolerance", default=_env("DF_FAL_SAFETY_TOLERANCE", "2"))  # 1..5 (string is safest)

    # split ratios
    ap.add_argument("--train_pct", type=int, default=_env_int("DF_SPLIT_TRAIN_PCT", 90))
    ap.add_argument("--val_pct", type=int, default=_env_int("DF_SPLIT_VAL_PCT", 7))

    # progress + robustness
    ap.add_argument("--log_every", type=int, default=_env_int("DF_LOG_EVERY", 25))
    ap.add_argument("--checkpoint_every", type=int, default=_env_int("DF_CHECKPOINT_EVERY", 200))
    ap.add_argument("--max_attempts_per_example", type=int, default=_env_int("DF_MAX_ATTEMPTS_PER_EXAMPLE", 3))

    args = ap.parse_args()

    fal_key = _fal_key()
    if args.enable_refine and not fal_key:
        raise SystemExit("Missing FAL_KEY (or FAL_API_KEY / COMMERCE_FAL_KEY) for --enable_refine")

    conn = _env("AZURE_STORAGE_CONNECTION_STRING") or _env("COMMERCE_AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        raise SystemExit("Missing AZURE_STORAGE_CONNECTION_STRING")

    storage = AzureStorageService(
        config=AzureStorageConfig(connection_string=conn, container=args.storage_container, default_sas_hours=24)
    )

    dataset_id = str(uuid4())
    if args.storage_prefix:
        dataset_prefix = args.storage_prefix.rstrip("/")
    else:
        dataset_prefix = f"training/saree_synth/{time.strftime('%Y-%m-%d')}/{dataset_id}"

    recipe = {
        "dataset_id": dataset_id,
        "created_at": _utc_now().isoformat(),
        "seed": args.seed,
        "num_examples": args.num_examples,
        "drape_style": args.drape_style,
        "only_active_template": bool(args.only_active_template),
        "rehydrate_inputs": bool(args.rehydrate_inputs),
        "refine": {
            "enabled": bool(args.enable_refine),
            "fal_base_url": args.fal_base_url,
            "i2i_model_id": args.i2i_model_id,
            "image_size": args.refine_image_size,
            "output_format": args.refine_output_format,
            "safety_tolerance": str(args.safety_tolerance),
        },
        "splits": {"train_pct": args.train_pct, "val_pct": args.val_pct},
        "progress": {"log_every": args.log_every, "checkpoint_every": args.checkpoint_every},
        "robustness": {"max_attempts_per_example": args.max_attempts_per_example},
    }

    await db_insert_dataset(
        dataset_id=dataset_id,
        name=args.dataset_name,
        kind=args.dataset_kind,
        usage_scope=args.usage_scope,
        license_name=(args.license_name or None),
        license_url=(args.license_url or None),
        storage_container=args.storage_container,
        storage_prefix=dataset_prefix,
        recipe_json=recipe,
        created_by=(args.created_by or None),
    )

    persons = _load_pool(args.persons_json)
    sarees = _load_pool(args.sarees_json)
    blouses = _load_pool(args.blouses_json) if args.blouses_json else []
    pallus = _load_pool(args.pallus_json) if args.pallus_json else []

    if not persons or not sarees:
        raise SystemExit("Pools must contain at least persons and sarees")

    if args.rehydrate_inputs:
        persons = await _rehydrate_pool_items(
            storage,
            items=persons,
            dataset_container=args.storage_container,
            dataset_prefix=dataset_prefix,
            kind="persons",
            concurrency=args.concurrency,
        )
        sarees = await _rehydrate_pool_items(
            storage,
            items=sarees,
            dataset_container=args.storage_container,
            dataset_prefix=dataset_prefix,
            kind="sarees",
            concurrency=args.concurrency,
        )
        if blouses:
            blouses = await _rehydrate_pool_items(
                storage,
                items=blouses,
                dataset_container=args.storage_container,
                dataset_prefix=dataset_prefix,
                kind="blouses",
                concurrency=args.concurrency,
            )
        if pallus:
            pallus = await _rehydrate_pool_items(
                storage,
                items=pallus,
                dataset_container=args.storage_container,
                dataset_prefix=dataset_prefix,
                kind="pallus",
                concurrency=args.concurrency,
            )

    templates = await db_fetch_templates(drape_style=args.drape_style, only_active=bool(args.only_active_template))
    if not templates:
        raise SystemExit(f"No drape_templates found for garment_type='saree' drape_style={args.drape_style}")

    tpl_cache: Dict[str, TemplateAssets] = {}

    sem = asyncio.Semaphore(max(1, args.concurrency))
    stats: Dict[str, int] = {"inserted": 0, "train": 0, "val": 0, "test": 0, "failed": 0}
    failures: List[Dict[str, Any]] = []

    stats_lock = asyncio.Lock()
    started_at = time.time()
    est_refine_usd = 0.0

    log_every = max(1, int(args.log_every))
    checkpoint_every = max(1, int(args.checkpoint_every))

    refine_prompt = (args.refine_prompt or "").strip()
    if not refine_prompt:
        refine_prompt = (
            f"Photorealistic Indian woman wearing a correctly draped {args.drape_style} saree. "
            "Realistic fabric folds, correct pallu drape, natural lighting, high detail, clean background. "
            "No text, no watermark."
        )

    def _pick_rng(ix: int, attempt: int) -> random.Random:
        h = _sha256_str(f"{args.seed}:{ix}:{attempt}")[:8]
        seed = int(h, 16) & 0x7FFFFFFF
        return random.Random(seed)

    async def _maybe_log_and_checkpoint(inserted_now: int) -> None:
        nonlocal est_refine_usd
        if inserted_now <= 0:
            return

        need_log = (inserted_now % log_every == 0) or (inserted_now == args.num_examples)
        need_ckpt = (inserted_now % checkpoint_every == 0) or (inserted_now == args.num_examples)

        if need_log:
            elapsed = max(0.001, time.time() - started_at)
            print(
                json.dumps(
                    {
                        "event": "progress",
                        "dataset_id": dataset_id,
                        "inserted": inserted_now,
                        "failed": stats["failed"],
                        "elapsed_s": round(elapsed, 1),
                        "examples_per_min": round((inserted_now / elapsed) * 60.0, 2),
                        "est_refine_usd": round(est_refine_usd, 2),
                    }
                ),
                flush=True,
            )

        if need_ckpt:
            await db_update_dataset_stats(
                dataset_id,
                {
                    "dataset_id": dataset_id,
                    "storage_container": args.storage_container,
                    "storage_prefix": dataset_prefix,
                    "counts": dict(stats),
                    "failures_sample": failures[:30],
                    "est_refine_usd": round(est_refine_usd, 4),
                    "models": {"i2i_model_id": args.i2i_model_id, "fal_base_url": args.fal_base_url},
                    "updated_at": _utc_now().isoformat(),
                },
            )

    async def run_one(ix: int) -> None:
        async with sem:
            last_err: Optional[str] = None

            for attempt in range(max(1, int(args.max_attempts_per_example))):
                try:
                    prng = _pick_rng(ix, attempt)

                    person = persons[prng.randrange(len(persons))]
                    saree = sarees[prng.randrange(len(sarees))]
                    blouse = blouses[prng.randrange(len(blouses))] if blouses else None
                    pallu = pallus[prng.randrange(len(pallus))] if pallus else None
                    tpl = templates[prng.randrange(len(templates))]

                    tpl_id = str(tpl["id"])

                    # Dedup hash: stable over chosen refs AND attempt so retries don't collide
                    dedup_payload = {
                        "template_id": tpl_id,
                        "person": person.get("blob") or person.get("url"),
                        "saree": saree.get("blob") or saree.get("url"),
                        "blouse": (blouse or {}).get("blob") or (blouse or {}).get("url"),
                        "pallu": (pallu or {}).get("blob") or (pallu or {}).get("url"),
                        "seed": args.seed,
                        "ix": ix,
                        "attempt": attempt,
                    }
                    dedup_hash = _sha256_str(json.dumps(dedup_payload, sort_keys=True, separators=(",", ":")))
                    split = split_from_hash(dedup_hash, train_pct=args.train_pct, val_pct=args.val_pct)

                    # Load template assets (cached)
                    if tpl_id not in tpl_cache:
                        tpl_cache[tpl_id] = await _load_template_assets(
                            connection_string=conn,
                            template_id=tpl_id,
                            storage_container=str(tpl["storage_container"]),
                            storage_prefix=str(tpl["storage_prefix"]),
                            manifest_json=_as_dict_loose(tpl.get("manifest_json")),
                        )
                    tpl_assets = tpl_cache[tpl_id]

                    # Download images (we rely on url; pools were rehydrated to stable SAS under dataset prefix)
                    p_bytes = await asyncio.to_thread(_download_bytes, str(person["url"]), 180)
                    s_bytes = await asyncio.to_thread(_download_bytes, str(saree["url"]), 180)
                    b_bytes = await asyncio.to_thread(_download_bytes, str(blouse["url"]), 180) if blouse else None
                    pa_bytes = await asyncio.to_thread(_download_bytes, str(pallu["url"]), 180) if pallu else None

                    if not p_bytes or not s_bytes:
                        raise RuntimeError("missing person/saree bytes")

                    person_img = Image.open(io.BytesIO(p_bytes)).convert("RGBA")
                    saree_tex = Image.open(io.BytesIO(s_bytes)).convert("RGBA")
                    blouse_tex = Image.open(io.BytesIO(b_bytes)).convert("RGBA") if b_bytes else None
                    pallu_tex = Image.open(io.BytesIO(pa_bytes)).convert("RGBA") if pa_bytes else None

                    composite_img, debug = compose_tryon_composite(
                        person_rgba=person_img,
                        saree_texture=saree_tex,
                        pallu_texture=pallu_tex,
                        blouse_texture=blouse_tex,
                        tpl=tpl_assets,
                    )

                    comp_buf = io.BytesIO()
                    composite_img.save(comp_buf, format="PNG")
                    comp_bytes = comp_buf.getvalue()

                    comp_blob = f"{dataset_prefix}/examples/{split}/{dedup_hash}/composite.png"
                    comp_url = _upload_bytes(
                        storage,
                        data=comp_bytes,
                        blob_name=comp_blob,
                        content_type="image/png",
                        container_name=args.storage_container,
                    )

                    target_blob = comp_blob
                    target_url = comp_url
                    target_bytes = comp_bytes

                    # Refine target using FLUX.2 Pro Edit
                    if args.enable_refine:
                        seed = int(dedup_hash[:8], 16) & 0x7FFFFFFF

                        # If we are retrying after a policy violation, switch prompt to safe variant
                        prompt_to_use = refine_prompt if attempt == 0 else _safe_prompt_variant(refine_prompt)

                        payload = {
                            "prompt": prompt_to_use,
                            "image_urls": [comp_url],
                            "image_size": args.refine_image_size,
                            "seed": seed,
                            "output_format": "png" if (args.refine_output_format or "").lower() == "png" else "jpeg",
                            "enable_safety_checker": True,
                            "safety_tolerance": str(args.safety_tolerance),
                        }

                        out = await _fal_run_and_wait(
                            base_url=args.fal_base_url,
                            model_id=args.i2i_model_id,
                            input_json=payload,
                            fal_key=fal_key,
                            poll_secs=args.poll_secs,
                            poll_timeout_s=args.poll_timeout_s,
                            http_timeout_s=args.http_timeout_s,
                            lifecycle_seconds=7200,
                        )

                        urls = _parse_fal_images(out)
                        if not urls:
                            raise RuntimeError(f"refine produced no images. keys={list(out.keys())}")

                        src_url, content_type = urls[0]
                        t_bytes = await asyncio.to_thread(_download_bytes, src_url, 180)
                        if not t_bytes or len(t_bytes) < 2048:
                            raise RuntimeError("refine download too small/empty")

                        target_blob = f"{dataset_prefix}/examples/{split}/{dedup_hash}/target.png"
                        target_url = _upload_bytes(
                            storage,
                            data=t_bytes,
                            blob_name=target_blob,
                            content_type=content_type or "image/png",
                            container_name=args.storage_container,
                        )
                        target_bytes = t_bytes

                    person_ref = {
                        "container": person.get("container"),
                        "blob": person.get("blob"),
                        "url": person.get("url"),
                        "src_url": person.get("src_url"),
                    }
                    garment_refs: Dict[str, Any] = {
                        "saree": {
                            "container": saree.get("container"),
                            "blob": saree.get("blob"),
                            "url": saree.get("url"),
                            "src_url": saree.get("src_url"),
                        },
                        "blouse": (
                            {
                                "container": (blouse or {}).get("container"),
                                "blob": (blouse or {}).get("blob"),
                                "url": (blouse or {}).get("url"),
                                "src_url": (blouse or {}).get("src_url"),
                            }
                            if blouse
                            else None
                        ),
                        "pallu": (
                            {
                                "container": (pallu or {}).get("container"),
                                "blob": (pallu or {}).get("blob"),
                                "url": (pallu or {}).get("url"),
                                "src_url": (pallu or {}).get("src_url"),
                            }
                            if pallu
                            else None
                        ),
                    }
                    conditioning_refs = {"composite": {"container": args.storage_container, "blob": comp_blob, "url": comp_url}}
                    target_ref = {"container": args.storage_container, "blob": target_blob, "url": target_url}
                    mask_refs = {
                        "template_id": tpl_id,
                        "storage_container": tpl_assets.storage_container,
                        "storage_prefix": tpl_assets.storage_prefix,
                        "manifest_json": tpl_assets.manifest_json,
                    }
                    labels_json = {"garment_type": "saree", "drape_style": args.drape_style, "template_id": tpl_id}
                    quality_json = {"debug": debug}
                    consent_json = {"synthetic": True, "usage_scope": args.usage_scope}

                    sha256_json = {
                        "person": _sha256_bytes(p_bytes),
                        "saree": _sha256_bytes(s_bytes),
                        "blouse": _sha256_bytes(b_bytes) if b_bytes else None,
                        "pallu": _sha256_bytes(pa_bytes) if pa_bytes else None,
                        "composite": _sha256_bytes(comp_bytes),
                        "target": _sha256_bytes(target_bytes),
                    }

                    await db_insert_example(
                        dataset_id=dataset_id,
                        template_id=tpl_id,
                        split=split,
                        task="saree_tryon",
                        person_ref=person_ref,
                        garment_refs=garment_refs,
                        conditioning_refs=conditioning_refs,
                        target_ref=target_ref,
                        mask_refs=mask_refs,
                        labels_json=labels_json,
                        quality_json=quality_json,
                        consent_json=consent_json,
                        dedup_hash=dedup_hash,
                        sha256_json=sha256_json,
                    )

                    # progress + checkpoint
                    refine_cost = 0.0
                    if args.enable_refine:
                        mp = _ceil_mp_1024(composite_img.size[0], composite_img.size[1])
                        refine_cost = _estimate_flux2_pro_edit_usd(mp_in=mp, mp_out=mp)

                    async with stats_lock:
                        nonlocal est_refine_usd
                        est_refine_usd += refine_cost

                        stats["inserted"] += 1
                        stats[split] += 1
                        inserted_now = stats["inserted"]

                    await _maybe_log_and_checkpoint(inserted_now)
                    return  # success

                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"

                    # Retry policy violations with safer prompt + new sample
                    if _is_content_policy_violation(e) and (attempt + 1) < int(args.max_attempts_per_example):
                        continue

                    async with stats_lock:
                        stats["failed"] += 1
                        if len(failures) < 200:
                            failures.append({"i": ix, "attempt": attempt, "err": last_err})
                    return

            # Should never hit here; but keep for safety
            async with stats_lock:
                stats["failed"] += 1
                if len(failures) < 200:
                    failures.append({"i": ix, "attempt": int(args.max_attempts_per_example), "err": last_err or "unknown"})

    await asyncio.gather(*[run_one(i) for i in range(int(args.num_examples))])

    await db_update_dataset_stats(
        dataset_id,
        {
            "dataset_id": dataset_id,
            "storage_container": args.storage_container,
            "storage_prefix": dataset_prefix,
            "counts": dict(stats),
            "failures_sample": failures[:30],
            "est_refine_usd": round(est_refine_usd, 4),
            "models": {"i2i_model_id": args.i2i_model_id, "fal_base_url": args.fal_base_url},
            "updated_at": _utc_now().isoformat(),
        },
    )

    print("✅ Dataset generation complete")
    print(
        json.dumps(
            {
                "dataset_id": dataset_id,
                "storage_prefix": dataset_prefix,
                "stats": stats,
                "failures_sample": failures[:10],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))