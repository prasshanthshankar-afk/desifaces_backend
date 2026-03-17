#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import uuid4

import asyncpg
from azure.storage.blob import BlobServiceClient, BlobSasPermissions, generate_blob_sas

from app.db import get_pool
from app.services.catalog.platform_model_selector import get_platform_model_selector

try:
    from PIL import Image, ImageStat
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    ImageStat = None  # type: ignore

try:
    import torch
    from transformers import CLIPModel, CLIPProcessor
except Exception:  # pragma: no cover
    torch = None  # type: ignore
    CLIPModel = None  # type: ignore
    CLIPProcessor = None  # type: ignore

try:
    from app.services.training.training_dataset_service import TrainingDatasetService
except Exception:
    TrainingDatasetService = None  # type: ignore

try:
    from app.services.azure_storage_service import AzureStorageService
except Exception:
    AzureStorageService = None  # type: ignore


# ---------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------

DEFAULT_SEEDS: List[Dict[str, Any]] = [
    {
        "seed_name": "hoodie",
        "garment_kind": "upper_body",
        "outfit_kind": "upper_body",
        "component_code": "hoodie",
        "queries": [
            "hoodie clothing",
            "hooded sweatshirt garment",
            "hoodie fashion isolated",
        ],
    },
    {
        "seed_name": "blazer",
        "garment_kind": "upper_body",
        "outfit_kind": "upper_body",
        "component_code": "blazer",
        "queries": [
            "blazer jacket clothing",
            "suit blazer garment",
            "blazer fashion isolated",
        ],
    },
    {
        "seed_name": "jeans",
        "garment_kind": "lower_body",
        "outfit_kind": "lower_body",
        "component_code": "jeans",
        "queries": [
            "jeans clothing",
            "denim pants garment",
            "trousers clothing isolated",
        ],
    },
    {
        "seed_name": "dress",
        "garment_kind": "dresses",
        "outfit_kind": "dresses",
        "component_code": "dress",
        "queries": [
            "dress clothing",
            "gown fashion dress",
            "dress isolated clothing",
        ],
    },
    {
        "seed_name": "kurta",
        "garment_kind": "kurta_pyjama",
        "outfit_kind": "kurta_pyjama",
        "component_code": "kurta",
        "queries": [
            "kurta pyjama",
            "men kurta clothing",
            "kurta garment india",
        ],
    },
    {
        "seed_name": "salwar_suit",
        "garment_kind": "salwar_suit",
        "outfit_kind": "salwar_suit",
        "component_code": "salwar_suit",
        "queries": [
            "salwar kameez",
            "salwar suit clothing",
            "shalwar kameez dress",
        ],
    },
    {
        "seed_name": "lehenga",
        "garment_kind": "lehenga_set",
        "outfit_kind": "lehenga_set",
        "component_code": "lehenga",
        "queries": [
            "lehenga choli",
            "lehenga clothing india",
            "bridal lehenga",
        ],
    },
    {
        "seed_name": "sherwani",
        "garment_kind": "sherwani",
        "outfit_kind": "sherwani",
        "component_code": "sherwani",
        "queries": [
            "sherwani men clothing",
            "sherwani india garment",
            "wedding sherwani",
        ],
    },
]

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"

_ALLOWED_FAMILIES: Tuple[str, ...] = (
    "hoodie",
    "blazer",
    "jeans",
    "dress",
    "kurta",
    "salwar_suit",
    "lehenga",
    "sherwani",
)

_FAMILY_ALIASES: Dict[str, str] = {
    "hoodie": "hoodie",
    "blazer": "blazer",
    "jeans": "jeans",
    "dress": "dress",
    "dresses": "dress",
    "kurta": "kurta",
    "kurta_pyjama": "kurta",
    "salwar": "salwar_suit",
    "salwar suit": "salwar_suit",
    "salwar_suit": "salwar_suit",
    "shalwar kameez": "salwar_suit",
    "lehenga": "lehenga",
    "lehenga_set": "lehenga",
    "sherwani": "sherwani",
}

_FAMILY_TEXT: Dict[str, str] = {
    "hoodie": "hoodie sweatshirt",
    "blazer": "blazer jacket",
    "jeans": "jeans denim pants",
    "dress": "dress gown",
    "kurta": "kurta tunic",
    "salwar_suit": "salwar suit shalwar kameez",
    "lehenga": "lehenga choli",
    "sherwani": "sherwani coat",
}

_MALE_ONLY_FAMILIES = {"kurta", "sherwani"}
_FEMALE_ONLY_FAMILIES = {"dress", "salwar_suit", "lehenga"}


_SOURCE_TEXT_HARD_REJECT_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"\b(man|men|woman|women|girl|boy|person|people|couple|bride|groom|model|portrait)\b", re.I),
    re.compile(r"\b(smiling|posing|standing|sitting|holding|wearing)\b", re.I),
    re.compile(r"\b(wedding|bridal|ceremony|palace|outdoors|indoors|lahore|ludhiana)\b", re.I),
    re.compile(r"\b(bouquet|flag|flags|laptop|bag|mask|notes)\b", re.I),
    re.compile(r"\b(feather|feathers|texture|fabric|swatch|close[- ]?up|detail view)\b", re.I),
]


def _source_text_reject_reason(*, title: str, query: str) -> Optional[str]:
    text = f"{title} {query}".strip().lower()
    for pat in _SOURCE_TEXT_HARD_REJECT_PATTERNS:
        if pat.search(text):
            return "source_text_editorial_or_person"
    return None


# ---------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _utc_stamp() -> str:
    return _utc_now().strftime("%Y%m%d_%H%M%S")


def _mkdirp(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _slug(s: str, max_len: int = 120) -> str:
    s2 = re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "").strip()).strip("._-")
    return s2[:max_len] or "item"


def _stable_hash_int(s: str) -> int:
    return int(hashlib.sha256(s.encode("utf-8")).hexdigest(), 16)


def _stable_hash_hex(obj: Dict[str, Any]) -> str:
    s = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _deterministic_split(key: str) -> str:
    bucket = _stable_hash_int(key) % 1000
    if bucket < 890:
        return "train"
    if bucket < 970:
        return "val"
    return "test"


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def _as_dict_loose(x: Any) -> Dict[str, Any]:
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


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False, default=str)


def _b64url_decode(s: str) -> bytes:
    s2 = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s2.encode("utf-8"))


def _jwt_claims(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _jwt_sub(token: str) -> Optional[str]:
    payload = _jwt_claims(token)
    sub = payload.get("sub")
    return str(sub) if sub else None


def _jwt_exp(token: str) -> int:
    payload = _jwt_claims(token)
    try:
        return int(payload.get("exp") or 0)
    except Exception:
        return 0


def _guess_ext_from_url(url: str, fallback: str = ".jpg") -> str:
    try:
        path = urllib.parse.urlparse(url).path
        base = os.path.basename(path)
        if "." in base:
            ext = "." + base.split(".")[-1].lower()
            if 1 <= len(ext) <= 8:
                return ext
    except Exception:
        pass
    return fallback


def _strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _run(cmd: List[str], *, capture: bool = True) -> Tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    return p.returncode, p.stdout or "", p.stderr or ""


def _source_garment_clip_preflight(local_path: str, family: str, model_id: str) -> Dict[str, Any]:
    if not _ClipScorer.available() or Image is None:
        return {"available": False}

    fam_text = _FAMILY_TEXT.get(family, family)
    prompts = [
        f"a standalone product photo of a single {fam_text} on a plain background",
        f"a clean catalog product image of a single {fam_text}",
        f"a flat lay product photo of a single {fam_text}",
        f"a mannequin wearing a single {fam_text}",
        f"a studio photo of one person wearing a {fam_text}",
        "an editorial lifestyle fashion photo with a person",
        "a couple or wedding scene with people wearing clothing",
        "a collage of multiple garments",
        "a close-up fabric texture or feathers",
    ]

    try:
        with Image.open(local_path) as im:  # type: ignore
            img = im.convert("RGB")
            probs = _ClipScorer.score_prompts(model_id=model_id, image=img, prompts=prompts)
    except Exception as e:
        return {"available": False, "error": str(e)}

    product_like = max(float(probs[0]), float(probs[1]), float(probs[2]), float(probs[3]))
    person_like = float(probs[4])
    editorial = float(probs[5])
    couple_scene = float(probs[6])
    collage = float(probs[7])
    texture = float(probs[8])

    return {
        "available": True,
        "product_like_score": product_like,
        "person_like_score": person_like,
        "editorial_score": editorial,
        "couple_scene_score": couple_scene,
        "collage_score": collage,
        "texture_score": texture,
    }


def _source_garment_reject_reason(
    *,
    seed_name: str,
    garment_title: str,
    garment_query: str,
    local_garment_path: str,
    clip_model_id: str,
) -> Tuple[Optional[str], Dict[str, Any]]:
    text_reason = _source_text_reject_reason(title=garment_title, query=garment_query)
    if text_reason:
        return text_reason, {"available": False, "source": "text"}

    clip_info = _source_garment_clip_preflight(local_garment_path, family=seed_name, model_id=clip_model_id)
    if not clip_info.get("available"):
        return None, clip_info

    if float(clip_info.get("couple_scene_score") or 0.0) > 0.08:
        return "source_couple_or_wedding_like", clip_info
    if float(clip_info.get("collage_score") or 0.0) > 0.18:
        return "source_collage_like", clip_info
    if float(clip_info.get("texture_score") or 0.0) > 0.15:
        return "source_texture_like", clip_info
    if float(clip_info.get("person_like_score") or 0.0) > 0.18:
        return "source_person_wearing_like", clip_info
    if float(clip_info.get("editorial_score") or 0.0) > 0.10:
        return "source_too_editorial", clip_info
    if float(clip_info.get("product_like_score") or 0.0) < 0.28:
        return "source_product_like_too_low", clip_info

    return None, clip_info


def _curl_json(cmd: List[str], *, raw_out_path: str = "") -> Dict[str, Any]:
    cmd2 = list(cmd) + ["-w", "\n__HTTP_CODE__:%{http_code}"]
    rc, out, err = _run(cmd2, capture=True)

    if raw_out_path:
        Path(raw_out_path).write_text(out, encoding="utf-8")

    if rc != 0:
        raise RuntimeError(
            f"curl failed rc={rc}\nCMD: {' '.join(cmd2)}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
        )

    marker = "\n__HTTP_CODE__:"
    body = out
    http_code: Optional[int] = None
    if marker in out:
        body, tail = out.rsplit(marker, 1)
        tail = tail.strip()
        if tail.isdigit():
            http_code = int(tail)

    body = body.strip()

    if http_code is not None and not (200 <= http_code < 300):
        raise RuntimeError(
            f"HTTP {http_code}\nCMD: {' '.join(cmd)}\nBODY:\n{body}\nSTDERR:\n{err}"
        )

    try:
        return json.loads(body)
    except Exception as e:
        raise RuntimeError(
            f"curl returned non-JSON: {e}\nHTTP={http_code}\nBODY:\n{body}\nSTDERR:\n{err}"
        )


def _download_one(url: str, dst: str, timeout_s: int = 90) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
    if not data or len(data) < 1024:
        raise RuntimeError(f"download too small ({len(data) if data else 0} bytes) from {url}")
    with open(dst, "wb") as f:
        f.write(data)


def _download(urls: Any, dst: str, timeout_s: int = 90, retries: int = 4) -> str:
    candidates = [str(u).strip() for u in _as_list(urls) if str(u).strip()]
    if not candidates:
        raise RuntimeError("no download URLs provided")

    last_err: Optional[Exception] = None
    for url in candidates:
        for i in range(retries):
            try:
                _download_one(url, dst, timeout_s=timeout_s)
                return url
            except Exception as e:
                last_err = e
                time.sleep(1.25 * (i + 1))
    raise RuntimeError(f"download failed after retries; last_err={last_err}")


def _probe_url_head(url: str) -> Dict[str, Any]:
    rc, out, err = _run(["curl", "-sS", "-I", "-L", url], capture=True)
    if rc != 0:
        return {"ok": False, "http_code": None, "content_type": None, "err": (err or out)[:400]}
    http_code: Optional[int] = None
    content_type: Optional[str] = None
    for line in out.splitlines():
        s = line.strip()
        if s.lower().startswith("http/"):
            parts = s.split()
            if len(parts) >= 2 and parts[1].isdigit():
                http_code = int(parts[1])
        if s.lower().startswith("content-type:"):
            content_type = s.split(":", 1)[1].strip()
    ok = http_code is not None and 200 <= http_code < 400
    return {"ok": ok, "http_code": http_code, "content_type": content_type}


def _collect_urls(obj: Any, *, limit: int = 100) -> List[str]:
    out: List[str] = []

    def rec(x: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(x, str):
            s = x.strip()
            if s.startswith("http://") or s.startswith("https://"):
                out.append(s)
            return
        if isinstance(x, dict):
            for v in x.values():
                rec(v)
            return
        if isinstance(x, list) or isinstance(x, tuple):
            for v in x:
                rec(v)
            return

    rec(obj)
    uniq: List[str] = []
    seen = set()
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _truthy_env(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default)).strip().lower() in {"1", "true", "yes", "y", "on"}


def _url_key(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(str(url).strip())
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, "", ""))
    except Exception:
        return str(url).strip()


def _normalize_http_urls(x: Any) -> List[str]:
    vals: List[str] = []
    if isinstance(x, str):
        s = x.strip()
        if s:
            vals = [s]
    elif isinstance(x, list):
        vals = [str(v).strip() for v in x if isinstance(v, str) and str(v).strip()]

    out: List[str] = []
    seen = set()
    for u in vals:
        if not u.lower().startswith(("http://", "https://")):
            continue
        k = _url_key(u)
        if k in seen:
            continue
        seen.add(k)
        out.append(u)
    return out


def _extract_urls_from_variant_list(items: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if isinstance(it, str):
            if it.lower().startswith(("http://", "https://")):
                out.append(it.strip())
            continue
        if isinstance(it, dict):
            for key in ("url", "image_url", "asset_url", "preview_url", "output_url", "blob_url", "sas_url"):
                v = it.get(key)
                if isinstance(v, str) and v.strip().lower().startswith(("http://", "https://")):
                    out.append(v.strip())
    return out


def _extract_candidate_output_urls(status_obj: Dict[str, Any]) -> List[str]:
    status_d = _as_dict_loose(status_obj)
    payload = _as_dict_loose(status_d.get("payload_json"))
    computed = _as_dict_loose(status_d.get("computed"))
    payload_computed = _as_dict_loose(payload.get("computed"))
    provider_meta = _as_dict_loose(computed.get("provider_meta"))
    provider_result = _as_dict_loose(provider_meta.get("result"))
    provider_output = _as_dict_loose(provider_meta.get("output"))
    provider_outputs = provider_meta.get("outputs")

    candidates: List[str] = []

    for src in (
        status_d.get("urls"),
        computed.get("urls"),
        payload_computed.get("urls"),
        provider_result.get("urls"),
        provider_output.get("urls"),
        computed.get("output_urls"),
        computed.get("result_urls"),
        payload_computed.get("output_urls"),
        payload_computed.get("result_urls"),
        provider_result.get("output_urls"),
        provider_output.get("output_urls"),
    ):
        candidates.extend(_normalize_http_urls(src))

    for src in (
        status_d.get("variants"),
        status_d.get("images"),
        status_d.get("outputs"),
        status_d.get("results"),
        computed.get("variants"),
        computed.get("images"),
        computed.get("outputs"),
        computed.get("results"),
        payload_computed.get("variants"),
        payload_computed.get("images"),
        payload_computed.get("outputs"),
        payload_computed.get("results"),
        provider_outputs,
        provider_result.get("variants"),
        provider_result.get("images"),
        provider_result.get("outputs"),
        provider_output.get("variants"),
        provider_output.get("images"),
        provider_output.get("outputs"),
    ):
        candidates.extend(_extract_urls_from_variant_list(src))

    if not candidates:
        candidates.extend(_collect_urls(status_d, limit=200))

    uniq: List[str] = []
    seen = set()
    for u in candidates:
        if not isinstance(u, str):
            continue
        s = u.strip()
        if not s.lower().startswith(("http://", "https://")):
            continue
        k = _url_key(s)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)

    def _score(u: str) -> int:
        lu = u.lower()
        score = 0
        if "/commerce/vton/" in lu:
            score += 100
        if "/commerce-output/" in lu:
            score += 40
        if "tryon" in lu or "vton" in lu or "result" in lu or "variant" in lu:
            score += 20
        if "/commerce_assets/" in lu:
            score -= 100
        if "/platform_models/" in lu:
            score -= 100
        if "/inputs/garments/" in lu:
            score -= 100
        return score

    uniq.sort(key=lambda u: (-_score(u), u))
    return uniq


def _choose_output_urls(
    status_obj: Dict[str, Any],
    max_outputs: int,
    *,
    exclude_urls: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    exclude_keys = {_url_key(u) for u in (exclude_urls or []) if isinstance(u, str) and u.strip()}
    candidates = _extract_candidate_output_urls(status_obj)

    out: List[Dict[str, Any]] = []
    seen = set()

    for u in candidates:
        lu = u.lower()
        uk = _url_key(u)

        if uk in seen:
            continue
        seen.add(uk)

        if uk in exclude_keys:
            continue

        if "/commerce_assets/" in lu or "/platform_models/" in lu or "/inputs/garments/" in lu:
            continue

        if not (".png" in lu or ".jpg" in lu or ".jpeg" in lu or ".webp" in lu):
            continue

        probe = _probe_url_head(u)
        ct = (probe.get("content_type") or "").lower()
        if not probe.get("ok"):
            continue
        if ct and "image" not in ct:
            continue

        out.append({"url": u, "probe": probe})
        if len(out) >= max_outputs:
            break

    return out


def _canon_family(x: str) -> str:
    s = str(x or "").strip().lower().replace("-", "_")
    return _FAMILY_ALIASES.get(s, s)


def _expected_family(seed_name: str, component_code: str, garment_kind: str) -> str:
    for v in (component_code, seed_name, garment_kind):
        c = _canon_family(v)
        if c in _ALLOWED_FAMILIES:
            return c
    return _canon_family(component_code or seed_name or garment_kind)


# ---------------------------------------------------------------------
# garment manifest helpers
# ---------------------------------------------------------------------


def _seed_index() -> Dict[str, Dict[str, Any]]:
    return {str(s["seed_name"]): dict(s) for s in DEFAULT_SEEDS}


def _normalize_manifest_record(raw: Dict[str, Any], seed_idx: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    rec = _as_dict_loose(raw)
    seed_name = _canon_family(
        str(rec.get("seed_name") or rec.get("family") or rec.get("component_code") or rec.get("name") or "")
    )
    if seed_name not in seed_idx:
        raise RuntimeError(f"manifest item missing/unsupported family: {raw}")

    seed = seed_idx[seed_name]
    image_url = str(
        rec.get("image_url")
        or rec.get("url")
        or rec.get("primary_image_url")
        or rec.get("garment_image_url")
        or ""
    ).strip()
    if not image_url:
        raise RuntimeError(f"manifest item missing image url: {raw}")

    title = str(rec.get("title") or rec.get("display_name") or rec.get("name") or os.path.basename(image_url)).strip()
    license_name = str(rec.get("license_name") or rec.get("license") or rec.get("usage_terms") or "owned_or_vendor").strip()
    usage_terms = str(rec.get("usage_terms") or rec.get("license_name") or rec.get("source_type") or "owned_or_vendor").strip()
    description_url = str(rec.get("description_url") or rec.get("source_url") or rec.get("page_url") or "").strip()
    source_type = str(rec.get("source_type") or rec.get("source") or "manifest").strip() or "manifest"

    item = {
        "seed_name": seed_name,
        "garment_kind": str(rec.get("garment_kind") or seed["garment_kind"]),
        "outfit_kind": str(rec.get("outfit_kind") or seed["outfit_kind"]),
        "component_code": str(rec.get("component_code") or seed["component_code"]),
        "title": title,
        "image_url": image_url,
        "description_url": description_url,
        "license_name": license_name,
        "usage_terms": usage_terms,
        "artist": str(rec.get("artist") or rec.get("brand") or rec.get("vendor") or ""),
        "credit": str(rec.get("credit") or rec.get("brand") or rec.get("vendor") or ""),
        "query": str(rec.get("query") or rec.get("vendor_sku") or "manifest"),
        "vendor_sku": str(rec.get("vendor_sku") or rec.get("sku") or ""),
        "brand": str(rec.get("brand") or rec.get("vendor") or ""),
        "source_type": source_type,
        "provider": str(rec.get("provider") or ""),
        "source_origin_url": str(rec.get("source_origin_url") or rec.get("description_url") or ""),
        "raw_image_url": str(rec.get("raw_image_url") or rec.get("image_url") or ""),
        "meta": _as_dict_loose(rec.get("meta")),
        "azure_uri": str(rec.get("azure_uri") or ""),
    }
    return item


def _load_manifest_records(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"garment manifest not found: {path}")

    seed_idx = _seed_index()
    items: List[Dict[str, Any]] = []
    suffix = p.suffix.lower()

    if suffix == ".jsonl":
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                items.append(_normalize_manifest_record(json.loads(s), seed_idx))
    elif suffix == ".csv":
        with p.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                items.append(_normalize_manifest_record(dict(row), seed_idx))
    else:
        raw = _read_json(str(p))
        arr: Iterable[Any]
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            arr = raw["items"]
        elif isinstance(raw, list):
            arr = raw
        else:
            raise RuntimeError('manifest JSON must be a list or {"items": [...]} ')
        for row in arr:
            items.append(_normalize_manifest_record(_as_dict_loose(row), seed_idx))

    return items


def _dedupe_garment_candidates(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[Tuple[str, str]] = set()
    out: List[Dict[str, Any]] = []
    for item in items:
        k = (str(item.get("seed_name") or ""), _url_key(str(item.get("image_url") or "")))
        if k in seen:
            continue
        seen.add(k)
        out.append(dict(item))
    return out


# ---------------------------------------------------------------------
# QC helpers
# ---------------------------------------------------------------------

@dataclass
class QCConfig:
    min_width: int = 384
    min_height: int = 384
    min_entropy: float = 2.5
    min_brightness_stddev: float = 12.0

    reject_if_target_matches_garment_hash_le: int = 6
    reject_if_target_matches_model_hash_le: int = 6
    reject_if_target_matches_garment_hist_ge: float = 0.985
    reject_if_target_matches_model_hist_ge: float = 0.985

    require_clip_classifier: bool = True
    clip_model_id: str = "openai/clip-vit-base-patch32"
    clip_min_expected_wearing_score: float = 0.22
    clip_min_expected_margin_over_product: float = 0.06
    clip_min_expected_margin_over_other_family: float = 0.04
    allow_without_clip: bool = False

    @classmethod
    def from_env(cls) -> "QCConfig":
        def _i(name: str, default: int) -> int:
            try:
                return int(float(os.getenv(name, str(default))))
            except Exception:
                return default

        def _f(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)))
            except Exception:
                return default

        def _b(name: str, default: bool) -> bool:
            return _truthy_env(name, "1" if default else "0")

        return cls(
            min_width=_i("DF_NONSAREE_QC_MIN_WIDTH", 384),
            min_height=_i("DF_NONSAREE_QC_MIN_HEIGHT", 384),
            min_entropy=_f("DF_NONSAREE_QC_MIN_ENTROPY", 2.5),
            min_brightness_stddev=_f("DF_NONSAREE_QC_MIN_BRIGHTNESS_STDDEV", 12.0),
            reject_if_target_matches_garment_hash_le=_i("DF_NONSAREE_QC_REJECT_GARMENT_HASH_LE", 6),
            reject_if_target_matches_model_hash_le=_i("DF_NONSAREE_QC_REJECT_MODEL_HASH_LE", 6),
            reject_if_target_matches_garment_hist_ge=_f("DF_NONSAREE_QC_REJECT_GARMENT_HIST_GE", 0.985),
            reject_if_target_matches_model_hist_ge=_f("DF_NONSAREE_QC_REJECT_MODEL_HIST_GE", 0.985),
            require_clip_classifier=_b("DF_NONSAREE_QC_REQUIRE_CLIP", True),
            clip_model_id=(os.getenv("DF_NONSAREE_QC_CLIP_MODEL_ID") or "openai/clip-vit-base-patch32").strip(),
            clip_min_expected_wearing_score=_f("DF_NONSAREE_QC_CLIP_MIN_WEARING_SCORE", 0.22),
            clip_min_expected_margin_over_product=_f("DF_NONSAREE_QC_CLIP_MIN_MARGIN_PRODUCT", 0.06),
            clip_min_expected_margin_over_other_family=_f("DF_NONSAREE_QC_CLIP_MIN_MARGIN_OTHER", 0.04),
            allow_without_clip=_b("DF_NONSAREE_QC_ALLOW_WITHOUT_CLIP", False),
        )


@dataclass
class QCDecision:
    accepted: bool
    status: str
    expected_family: str
    predicted_family: Optional[str]
    reasons: List[str]
    metrics: Dict[str, Any]
    clip: Dict[str, Any]
    context: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "expected_family": self.expected_family,
            "predicted_family": self.predicted_family,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "clip": dict(self.clip),
            "context": dict(self.context),
        }


def _image_entropy(img: Any) -> float:
    hist = img.convert("L").histogram()
    total = float(sum(hist) or 1.0)
    entropy = 0.0
    for h in hist:
        if h <= 0:
            continue
        p = h / total
        entropy -= p * math.log2(p)
    return float(entropy)


def _flattened_pixels(im: Any) -> List[int]:
    if hasattr(im, "get_flattened_data"):
        return list(im.get_flattened_data())
    return list(im.getdata())


def _avg_hash(img: Any, size: int = 16) -> int:
    im = img.convert("L").resize((size, size))
    pixels = _flattened_pixels(im.getchannel(0))
    avg = sum(pixels) / float(len(pixels) or 1)
    bits = 0
    for i, px in enumerate(pixels):
        if px >= avg:
            bits |= 1 << i
    return bits


def _hamming_distance(a: int, b: int) -> int:
    return int((a ^ b).bit_count())


def _hist_cosine_similarity(a: Any, b: Any) -> float:
    ha = a.convert("RGB").resize((128, 128)).histogram()
    hb = b.convert("RGB").resize((128, 128)).histogram()
    dot = 0.0
    na = 0.0
    nb = 0.0
    for xa, xb in zip(ha, hb):
        fa = float(xa)
        fb = float(xb)
        dot += fa * fb
        na += fa * fa
        nb += fb * fb
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(dot / math.sqrt(na * nb))


def _image_stats(img: Any) -> Dict[str, Any]:
    gray = img.convert("L")
    stat = ImageStat.Stat(gray)
    stddev = float(stat.stddev[0] if stat.stddev else 0.0)
    mean = float(stat.mean[0] if stat.mean else 0.0)
    return {
        "width": int(img.width),
        "height": int(img.height),
        "entropy": _image_entropy(img),
        "brightness_mean": mean,
        "brightness_stddev": stddev,
        "avg_hash": _avg_hash(img),
    }


class _ClipScorer:
    _model: Any = None
    _processor: Any = None
    _device: str = "cpu"
    _model_id: Optional[str] = None
    _cache_dir: Optional[str] = None

    @classmethod
    def available(cls) -> bool:
        return CLIPModel is not None and CLIPProcessor is not None and torch is not None

    @classmethod
    def _resolve_cache_dir(cls) -> str:
        cache_dir = (
            os.environ.get("HF_HUB_CACHE")
            or os.environ.get("HF_HOME")
            or "/tmp/huggingface"
        )
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir

    @classmethod
    def _ensure_loaded(cls, model_id: str) -> None:
        if not cls.available():
            raise RuntimeError("transformers/torch not available")

        cache_dir = cls._resolve_cache_dir()
        if (
            cls._model is not None
            and cls._processor is not None
            and cls._model_id == model_id
            and cls._cache_dir == cache_dir
        ):
            return

        try:
            cls._processor = CLIPProcessor.from_pretrained(
                model_id,
                cache_dir=cache_dir,
                local_files_only=True,
            )
            cls._model = CLIPModel.from_pretrained(
                model_id,
                cache_dir=cache_dir,
                local_files_only=True,
                use_safetensors=True,
                low_cpu_mem_usage=True,
            )
        except Exception:
            cls._processor = CLIPProcessor.from_pretrained(
                model_id,
                cache_dir=cache_dir,
            )
            cls._model = CLIPModel.from_pretrained(
                model_id,
                cache_dir=cache_dir,
                use_safetensors=True,
                low_cpu_mem_usage=True,
            )

        cls._model.eval()
        cls._device = "cuda" if torch.cuda.is_available() else "cpu"
        cls._model.to(cls._device)
        cls._model_id = model_id
        cls._cache_dir = cache_dir

    @classmethod
    def score_prompts(cls, *, model_id: str, image: Any, prompts: List[str]) -> List[float]:
        cls._ensure_loaded(model_id)
        inputs = cls._processor(text=prompts, images=image, return_tensors="pt", padding=True)
        for k, v in list(inputs.items()):
            if hasattr(v, "to"):
                inputs[k] = v.to(cls._device)

        with torch.no_grad():
            outputs = cls._model(**inputs)
            probs = outputs.logits_per_image[0].softmax(dim=0).detach().cpu().tolist()
        return [float(x) for x in probs]


class NonSareeQCGate:
    def __init__(self, config: Optional[QCConfig] = None) -> None:
        self.config = config or QCConfig.from_env()

    @classmethod
    def from_env(cls) -> "NonSareeQCGate":
        return cls(QCConfig.from_env())

    def _family_prompts(self) -> Tuple[List[str], List[Tuple[str, str]]]:
        prompts: List[str] = []
        mapping: List[Tuple[str, str]] = []

        for fam in _ALLOWED_FAMILIES:
            txt = _FAMILY_TEXT[fam]
            prompts.append(f"a studio photo of a person wearing a {txt}")
            mapping.append((fam, "wearing"))

        for fam in _ALLOWED_FAMILIES:
            txt = _FAMILY_TEXT[fam]
            prompts.append(f"a standalone product photo of a {txt} on a plain background")
            mapping.append((fam, "product"))

        prompts.append("a studio photo of a single person wearing clothing")
        mapping.append(("generic_person", "wearing"))
        prompts.append("a standalone product photo of clothing with no person")
        mapping.append(("generic_product", "product"))
        return prompts, mapping

    def _clip_family_check(self, *, expected_family: str, target_img: Any) -> Dict[str, Any]:
        if not _ClipScorer.available():
            return {"available": False, "reason": "clip_not_available"}

        try:
            prompts, mapping = self._family_prompts()
            probs = _ClipScorer.score_prompts(
                model_id=self.config.clip_model_id,
                image=target_img.convert("RGB"),
                prompts=prompts,
            )
        except Exception as e:
            return {"available": False, "reason": f"clip_runtime_error:{type(e).__name__}:{e}"}

        wearing_scores: Dict[str, float] = {}
        product_scores: Dict[str, float] = {}

        for (fam, kind), score in zip(mapping, probs):
            if kind == "wearing":
                wearing_scores[fam] = float(score)
            else:
                product_scores[fam] = float(score)

        expected_wearing = float(wearing_scores.get(expected_family, 0.0))
        expected_product = float(product_scores.get(expected_family, 0.0))

        predicted_family: Optional[str] = None
        predicted_family_score = -1.0
        other_family: Optional[str] = None
        other_family_score = 0.0

        for fam, score in wearing_scores.items():
            if fam == "generic_person":
                continue
            if score > predicted_family_score:
                predicted_family = fam
                predicted_family_score = float(score)

        for fam, score in wearing_scores.items():
            if fam in {expected_family, "generic_person"}:
                continue
            if score > other_family_score:
                other_family_score = float(score)
                other_family = fam

        return {
            "available": True,
            "predicted_family": predicted_family,
            "predicted_family_score": predicted_family_score,
            "expected_wearing_score": expected_wearing,
            "expected_product_score": expected_product,
            "margin_over_product": expected_wearing - expected_product,
            "margin_over_other_family": expected_wearing - other_family_score,
            "other_family": other_family,
            "other_family_score": other_family_score,
            "human_vs_product_margin": float(wearing_scores.get("generic_person", 0.0)) - float(product_scores.get("generic_product", 0.0)),
            "all_wearing_scores": wearing_scores,
            "all_product_scores": product_scores,
        }

    def evaluate(
        self,
        *,
        expected_family: str,
        source_garment_path: str,
        source_model_path: str,
        target_path: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> QCDecision:
        context = dict(context or {})
        expected = _canon_family(expected_family)

        if Image is None or ImageStat is None:
            return QCDecision(
                accepted=False,
                status="review",
                expected_family=expected,
                predicted_family=None,
                reasons=["pillow_not_available"],
                metrics={},
                clip={},
                context=context,
            )

        if expected not in _ALLOWED_FAMILIES:
            return QCDecision(
                accepted=False,
                status="review",
                expected_family=expected,
                predicted_family=None,
                reasons=[f"unsupported_expected_family:{expected}"],
                metrics={},
                clip={},
                context=context,
            )

        with Image.open(source_garment_path) as gi:
            garment_img = gi.convert("RGB")
        with Image.open(source_model_path) as mi:
            model_img = mi.convert("RGB")
        with Image.open(target_path) as ti:
            target_img = ti.convert("RGB")

        garment_stats = _image_stats(garment_img)
        model_stats = _image_stats(model_img)
        target_stats = _image_stats(target_img)

        garment_hash_dist = _hamming_distance(int(target_stats["avg_hash"]), int(garment_stats["avg_hash"]))
        model_hash_dist = _hamming_distance(int(target_stats["avg_hash"]), int(model_stats["avg_hash"]))
        garment_hist = _hist_cosine_similarity(target_img, garment_img)
        model_hist = _hist_cosine_similarity(target_img, model_img)

        reasons: List[str] = []

        if int(target_stats["width"]) < self.config.min_width or int(target_stats["height"]) < self.config.min_height:
            reasons.append("target_too_small")

        if float(target_stats["entropy"]) < self.config.min_entropy:
            reasons.append("target_low_entropy")

        if float(target_stats["brightness_stddev"]) < self.config.min_brightness_stddev:
            reasons.append("target_low_brightness_stddev")

        if (
            garment_hash_dist <= self.config.reject_if_target_matches_garment_hash_le
            and garment_hist >= self.config.reject_if_target_matches_garment_hist_ge
        ):
            reasons.append("target_matches_garment_input")

        if (
            model_hash_dist <= self.config.reject_if_target_matches_model_hash_le
            and model_hist >= self.config.reject_if_target_matches_model_hist_ge
        ):
            reasons.append("target_matches_model_input")

        clip_info = self._clip_family_check(expected_family=expected, target_img=target_img)
        predicted_family = None

        if clip_info.get("available"):
            predicted_family = clip_info.get("predicted_family")
            expected_wearing_score = float(clip_info.get("expected_wearing_score") or 0.0)
            margin_over_product = float(clip_info.get("margin_over_product") or 0.0)
            margin_over_other_family = float(clip_info.get("margin_over_other_family") or 0.0)
            human_vs_product_margin = float(clip_info.get("human_vs_product_margin") or 0.0)

            if predicted_family != expected:
                reasons.append(f"predicted_family_mismatch:{predicted_family}")

            if expected_wearing_score < self.config.clip_min_expected_wearing_score:
                reasons.append("expected_family_wearing_score_too_low")

            if margin_over_product < self.config.clip_min_expected_margin_over_product:
                reasons.append("expected_family_not_stronger_than_product_only")

            if margin_over_other_family < self.config.clip_min_expected_margin_over_other_family:
                reasons.append("expected_family_not_stronger_than_other_family")

            if human_vs_product_margin <= 0.0:
                reasons.append("target_looks_like_product_only")
        else:
            if self.config.require_clip_classifier and not self.config.allow_without_clip:
                return QCDecision(
                    accepted=False,
                    status="review",
                    expected_family=expected,
                    predicted_family=None,
                    reasons=["clip_required_but_unavailable"],
                    metrics={
                        "target": target_stats,
                        "source_garment": garment_stats,
                        "source_model": model_stats,
                        "garment_hash_distance": garment_hash_dist,
                        "model_hash_distance": model_hash_dist,
                        "garment_hist_similarity": garment_hist,
                        "model_hist_similarity": model_hist,
                    },
                    clip=clip_info,
                    context=context,
                )

        soft_clip_reasons = {
            "expected_family_wearing_score_too_low",
            "expected_family_not_stronger_than_product_only",
            "expected_family_not_stronger_than_other_family",
            "target_looks_like_product_only",
        }

        hard_reasons = [
            r for r in reasons
            if not (r in soft_clip_reasons)
            and not r.startswith("predicted_family_mismatch:")
        ]

        predicted_matches = (predicted_family == expected)

        if reasons and not hard_reasons and predicted_matches:
            return QCDecision(
                accepted=False,
                status="review",
                expected_family=expected,
                predicted_family=predicted_family,
                reasons=reasons,
                metrics={
                    "target": target_stats,
                    "source_garment": garment_stats,
                    "source_model": model_stats,
                    "garment_hash_distance": garment_hash_dist,
                    "model_hash_distance": model_hash_dist,
                    "garment_hist_similarity": garment_hist,
                    "model_hist_similarity": model_hist,
                },
                clip=clip_info,
                context=context,
            )

        accepted = len(reasons) == 0
        return QCDecision(
            accepted=accepted,
            status="accepted" if accepted else "rejected",
            expected_family=expected,
            predicted_family=predicted_family,
            reasons=reasons,
            metrics={
                "target": target_stats,
                "source_garment": garment_stats,
                "source_model": model_stats,
                "garment_hash_distance": garment_hash_dist,
                "model_hash_distance": model_hash_dist,
                "garment_hist_similarity": garment_hist,
                "model_hist_similarity": model_hist,
            },
            clip=clip_info,
            context=context,
        )


# ---------------------------------------------------------------------
# azure helpers
# ---------------------------------------------------------------------


def _parse_conn_str(conn_str: str) -> Dict[str, str]:
    parts: Dict[str, str] = {}
    for seg in conn_str.split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k.strip()] = v.strip()
    return parts


def _parse_az_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("az://"):
        raise RuntimeError(f"not an az:// URI: {uri}")
    rest = uri[len("az://") :]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        raise RuntimeError(f"invalid az:// URI: {uri}")
    return parts[0], parts[1]


def _get_blob_service_client() -> BlobServiceClient:
    conn = (
        os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        or os.environ.get("AZURE_BLOB_CONNECTION_STRING")
        or os.environ.get("AZURE_STORAGE_CONN_STR")
    )
    if conn:
        return BlobServiceClient.from_connection_string(conn)

    account_url = (
        os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
        or os.environ.get("AZURE_BLOB_ACCOUNT_URL")
    )
    credential = (
        os.environ.get("AZURE_STORAGE_KEY")
        or os.environ.get("AZURE_STORAGE_SAS_TOKEN")
        or os.environ.get("AZURE_BLOB_KEY")
        or os.environ.get("AZURE_BLOB_SAS_TOKEN")
    )
    if account_url and credential:
        return BlobServiceClient(account_url=account_url, credential=credential)

    raise RuntimeError(
        "Azure credentials not found. Set AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL + key/SAS."
    )


def _make_read_sas_url(
    *,
    account_name: str,
    account_key: str,
    container: str,
    blob_name: str,
    ttl_days: int = 7,
) -> str:
    sas = generate_blob_sas(
        account_name=account_name,
        container_name=container,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=_utc_now() + dt.timedelta(days=ttl_days),
    )
    return f"https://{account_name}.blob.core.windows.net/{container}/{blob_name}?{sas}"


def _resolve_model_url_for_request(url: str, ttl_days: int = 7) -> str:
    if not url:
        return ""
    s = str(url).strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if not s.startswith("az://"):
        raise RuntimeError(f"unsupported model URL scheme: {s}")

    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not conn_str:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is required to convert az:// to SAS")

    conn = _parse_conn_str(conn_str)
    account_name = conn.get("AccountName")
    account_key = conn.get("AccountKey")
    if not account_name or not account_key:
        raise RuntimeError("could not parse AccountName/AccountKey from AZURE_STORAGE_CONNECTION_STRING")

    container, blob_name = _parse_az_uri(s)
    return _make_read_sas_url(
        account_name=account_name,
        account_key=account_key,
        container=container,
        blob_name=blob_name,
        ttl_days=ttl_days,
    )


def _upload_file_to_azure(*, local_path: str, container: str, blob_name: str, overwrite: bool = True) -> str:
    bsc = _get_blob_service_client()
    bc = bsc.get_blob_client(container=container, blob=blob_name)
    with open(local_path, "rb") as f:
        bc.upload_blob(f, overwrite=overwrite)
    return f"az://{container}/{blob_name}"


def _upload_json_to_azure(*, obj: Any, container: str, blob_name: str, overwrite: bool = True) -> str:
    bsc = _get_blob_service_client()
    bc = bsc.get_blob_client(container=container, blob=blob_name)
    raw = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    bc.upload_blob(raw, overwrite=overwrite, content_type="application/json")
    return f"az://{container}/{blob_name}"


# ---------------------------------------------------------------------
# public fallback (disabled by default)
# ---------------------------------------------------------------------


def _wikimedia_api_json(params: Dict[str, Any]) -> Dict[str, Any]:
    base = "https://commons.wikimedia.org/w/api.php"
    qs = urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(f"{base}?{qs}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _env_csv(name: str) -> List[str]:
    return [x.strip() for x in str(os.getenv(name, "")).split(",") if x.strip()]


def _license_ok(license_name: str, usage_terms: str) -> bool:
    s = (license_name or "").lower()
    t = (usage_terms or "").lower()
    blob = f"{s} {t}"
    return any(x in blob for x in ["cc", "creative commons", "public domain", "gfdl", "pd", "cc-by", "cc by", "cc-by-sa"])


def _wikimedia_search_files(query: str, *, limit: int = 12) -> List[Dict[str, Any]]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": 6,
        "gsrlimit": limit,
        "gsrsearch": query,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": 1600,
    }
    data = _wikimedia_api_json(params)
    pages = (data.get("query") or {}).get("pages") or {}
    out: List[Dict[str, Any]] = []

    for _, page in pages.items():
        title = str(page.get("title") or "").strip()
        if not title.lower().startswith("file:"):
            continue
        imageinfo = (page.get("imageinfo") or [{}])[0] or {}
        extmeta = imageinfo.get("extmetadata") or {}
        license_name = _strip_html(((extmeta.get("LicenseShortName") or {}).get("value") or ""))
        usage_terms = _strip_html(((extmeta.get("UsageTerms") or {}).get("value") or ""))
        artist = _strip_html(((extmeta.get("Artist") or {}).get("value") or ""))
        credit = _strip_html(((extmeta.get("Credit") or {}).get("value") or ""))
        source_url = str(imageinfo.get("thumburl") or imageinfo.get("url") or "").strip()
        description_url = str(imageinfo.get("descriptionurl") or "").strip()

        if not source_url:
            continue
        if not source_url.lower().split("?", 1)[0].endswith(IMAGE_EXTS):
            continue
        if not _license_ok(license_name, usage_terms):
            continue

        out.append(
            {
                "title": title,
                "image_url": source_url,
                "description_url": description_url,
                "license_name": license_name,
                "usage_terms": usage_terms,
                "artist": artist,
                "credit": credit,
                "query": query,
                "source_type": "wikimedia_fallback",
            }
        )

    out.sort(key=lambda x: x["title"])
    return out


def _gather_garment_candidates_from_public(
    *,
    seeds: Sequence[Dict[str, Any]],
    max_per_seed: int,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()

    for seed in seeds:
        found: List[Dict[str, Any]] = []
        for q in seed.get("queries") or []:
            results = _wikimedia_search_files(q, limit=max(6, max_per_seed * 3))
            for r in results:
                k = (seed["seed_name"], r["title"], r["image_url"])
                if k in seen:
                    continue
                seen.add(k)
                found.append(
                    {
                        "seed_name": seed["seed_name"],
                        "garment_kind": seed["garment_kind"],
                        "outfit_kind": seed["outfit_kind"],
                        "component_code": seed["component_code"],
                        **r,
                    }
                )
                if len(found) >= max_per_seed:
                    break
            if len(found) >= max_per_seed:
                break
        out.extend(found)

    return out


# ---------------------------------------------------------------------
# auth + commerce API helpers
# ---------------------------------------------------------------------

@dataclass
class AuthSession:
    core_url: str
    email: str
    password: str
    token: str = ""
    user_id: str = ""
    exp_ts: int = 0

    def login(self) -> Tuple[str, str]:
        body = {"email": self.email, "password": self.password}
        cmd = [
            "curl", "-sS",
            "-X", "POST", f"{self.core_url.rstrip('/')}/api/auth/login",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(body),
        ]
        auth_out = _curl_json(cmd)
        token = auth_out.get("access_token") or auth_out.get("token") or ""
        if not token:
            raise RuntimeError(f"login failed: missing access_token. response={auth_out}")
        user_id = auth_out.get("user_id") or auth_out.get("x_user_id") or _jwt_sub(token)
        if not user_id:
            raise RuntimeError(f"login response missing user_id and JWT sub not parseable: {auth_out}")
        self.token = str(token)
        self.user_id = str(user_id)
        self.exp_ts = _jwt_exp(self.token)
        return self.token, self.user_id

    def ensure(self, skew_s: int = 120) -> Tuple[str, str]:
        now = int(time.time())
        if not self.token or not self.user_id or not self.exp_ts or self.exp_ts <= now + skew_s:
            return self.login()
        return self.token, self.user_id


def _with_auth_retry(auth: AuthSession, req_fn):
    auth.ensure()
    try:
        return req_fn(auth.token, auth.user_id)
    except Exception as e:
        if "HTTP 401" not in str(e):
            raise
        auth.login()
        return req_fn(auth.token, auth.user_id)


def _upload_asset(commerce_url: str, auth: AuthSession, role: str, file_path: str) -> Dict[str, Any]:
    def _req(token: str, user_id: str) -> Dict[str, Any]:
        cmd = [
            "curl", "-sS",
            "-X", "POST", f"{commerce_url.rstrip('/')}/api/commerce/assets/upload",
            "-H", f"Authorization: Bearer {token}",
            "-H", f"X-User-Id: {user_id}",
            "-F", f"role={role}",
            "-F", f"file=@{file_path}",
        ]
        return _curl_json(cmd)

    return _with_auth_retry(auth, _req)


def _preferred_gender_for_seed(seed_name: str) -> Optional[str]:
    fam = _canon_family(seed_name)
    if fam in _MALE_ONLY_FAMILIES:
        return "male"
    if fam in _FEMALE_ONLY_FAMILIES:
        return "female"
    return None


def _people_for_seed_or_gender(seed_name: str, gender: str) -> List[str]:
    pref = _preferred_gender_for_seed(seed_name)
    g = (pref or str(gender or "")).strip().lower()
    if g == "male":
        return ["solo_male"]
    if g == "female":
        return ["solo_female"]
    return ["solo"]


def _preferred_provider_for_seed(seed_name: str) -> Optional[str]:
    fam = _canon_family(seed_name)

    specific_env = os.getenv(f"DF_NONSAREE_PROVIDER_{fam.upper()}", "").strip()
    if specific_env:
        return specific_env

    default_env = os.getenv("DF_NONSAREE_PROVIDER_DEFAULT", "").strip()
    if default_env:
        return default_env

    # sensible defaults for Indian garments
    if fam in {"salwar_suit", "lehenga", "kurta", "sherwani"}:
        return "imageapps_v2"

    if fam in {"hoodie", "blazer", "jeans", "dress"}:
        return ""

    return ""



def _build_quote_body(
    *,
    seed_name: str,
    garment_preview_url: str,
    component_code: str,
    garment_kind: str,
    outfit_kind: str,
    model_url: str,
    model_gender: str,
    preferred_tags: Optional[List[str]] = None,
    meta_extra: Optional[Dict[str, Any]] = None,
    num_images: int = 2,
) -> Dict[str, Any]:
    fam = _canon_family(seed_name)
    preferred_provider = _preferred_provider_for_seed(seed_name)
    is_indian_ethnic = fam in {"salwar_suit", "lehenga", "kurta", "sherwani"}

    item_meta: Dict[str, Any] = {
        "source": "bootstrap_non_saree_vton_dataset",
        "views": ["full_body"],
        "family_hint": fam,
        "outfit_kind": outfit_kind,
        "component_code": component_code,
    }

    meta: Dict[str, Any] = {
        "source": "bootstrap_non_saree_vton_dataset",
        "family_hint": fam,
        "outfit_kind": outfit_kind,
        "component_code": component_code,
    }

    if is_indian_ethnic:
        meta["style_family"] = "indian_ethnic"
        meta["ethnic_mode"] = True
        item_meta["style_family"] = "indian_ethnic"
        item_meta["ethnic_mode"] = True

    if preferred_provider:
        meta["provider_hints"] = {
            "preferred_provider": preferred_provider,
            "provider": preferred_provider,
        }
        item_meta["preferred_provider"] = preferred_provider
        item_meta["provider_hint"] = preferred_provider

    body: Dict[str, Any] = {
        "mode": "platform_models",
        "provider_kind": "platform_models",
        "product_type": "apparel",
        "look_set_ids": [],
        "product_ids": [],
        "outfit_kind": outfit_kind,
        "garment_kind": garment_kind,
        "outputs": {"num_images": max(1, num_images), "num_videos": 0},
        "views": {"full_body": True, "half_body": False},
        "people": _people_for_seed_or_gender(seed_name, model_gender),
        "drape_styles": [],
        "channels": [],
        "marketplaces": [],
        "resolution": "hd",
        "template_pack": "indian_ethnic" if is_indian_ethnic else "default",
        "language": "en",
        "cta": {"type": "whatsapp", "value": None},
        "provider_policy": "auto",
        "currency_hint": "USD",
        "product_assets": {
            "items": [
                {
                    "component_code": component_code,
                    "garment_kind": garment_kind,
                    "kind": "garment",
                    "image_url": garment_preview_url,
                    "image_urls": [],
                    "is_primary": True,
                    "dominance_rank": 0,
                    "display_name": None,
                    "vendor_sku": None,
                    "meta": item_meta,
                }
            ],
            "product_type": None,
            "cloth_type": outfit_kind if is_indian_ethnic else None,
            "dominant_component_code": component_code,
            "garment_image_url": garment_preview_url,
            "primary_image_url": garment_preview_url,
            "product_image_url": None,
            "saree_image_url": None,
            "blouse_image_url": None,
            "meta": {
                "family_hint": fam,
                "outfit_kind": outfit_kind,
                "ethnic_mode": is_indian_ethnic,
                "preferred_provider": preferred_provider or None,
            },
        },
        "model_ref": {
            "image_url": None,
            "human_image_url": model_url,
            "asset_id": None,
            "platform_model_id": None,
            "url": model_url,
            "ref_url": None,
            "photo_url": None,
            "meta": {
                "views": ["full_body"],
                "family_hint": fam,
                "ethnic_mode": is_indian_ethnic,
            },
        },
        "meta": meta,
    }

    if preferred_tags:
        body["meta"]["preferred_tags"] = preferred_tags

    if meta_extra:
        body["meta"].update(meta_extra)

    return body



def _quote(
    commerce_url: str,
    auth: AuthSession,
    *,
    quote_body: Dict[str, Any],
    raw_out_path: str = "",
) -> Dict[str, Any]:
    def _req(token: str, user_id: str) -> Dict[str, Any]:
        cmd = [
            "curl", "-sS",
            "-X", "POST", f"{commerce_url.rstrip('/')}/api/commerce/quote",
            "-H", "Content-Type: application/json",
            "-H", f"Authorization: Bearer {token}",
            "-H", f"X-User-Id: {user_id}",
            "-d", json.dumps(quote_body),
        ]
        return _curl_json(cmd, raw_out_path=raw_out_path)

    return _with_auth_retry(auth, _req)


async def _enqueue_direct_job_from_quote(
    *,
    quote_id: str,
    user_id: str,
    idempotency_key: str = "",
) -> Dict[str, Any]:
    pool = await get_pool()
    campaign_id = str(uuid4())
    studio_job_id = str(uuid4())

    async with pool.acquire() as con:
        async with con.transaction():
            q = await con.fetchrow(
                """
                select id, user_id, request_json, mode, resolution, status
                from public.commerce_quotes
                where id = $1::uuid and user_id = $2::uuid
                for update
                """,
                quote_id,
                user_id,
            )
            if not q:
                raise RuntimeError(
                    f"direct enqueue fallback: quote not found quote_id={quote_id} user_id={user_id}"
                )

            request_json = _as_dict_loose(q["request_json"])
            mode = str(request_json.get("mode") or q["mode"] or "platform_models").strip() or "platform_models"
            product_type = str(request_json.get("product_type") or "apparel").strip() or "apparel"
            resolution = str(request_json.get("resolution") or q["resolution"] or "hd").strip() or "hd"

            existing_campaign = await con.fetchrow(
                """
                select id
                from public.commerce_campaigns
                where user_id = $1::uuid and quote_id = $2::uuid
                order by created_at desc
                limit 1
                """,
                user_id,
                quote_id,
            )

            campaign_meta = {
                "source": "bootstrap_direct_enqueue",
                "quote_id": str(quote_id),
                "mode": mode,
                "product_type": product_type,
                "resolution": resolution,
                "idempotency_key": idempotency_key or None,
                "bootstrap_direct_enqueue": True,
            }

            if existing_campaign:
                campaign_id = str(existing_campaign["id"])
                await con.execute(
                    """
                    update public.commerce_campaigns
                    set mode = $3,
                        product_type = $4,
                        status = 'queued',
                        input_json = $5::jsonb,
                        meta_json = coalesce(meta_json, '{}'::jsonb) || $6::jsonb,
                        updated_at = now()
                    where id = $1::uuid and user_id = $2::uuid
                    """,
                    campaign_id,
                    user_id,
                    mode,
                    product_type,
                    json.dumps(request_json),
                    json.dumps(campaign_meta),
                )
            else:
                await con.execute(
                    """
                    insert into public.commerce_campaigns(
                        id, user_id, mode, product_type, status, quote_id, input_json, meta_json, created_at, updated_at
                    )
                    values(
                        $1::uuid, $2::uuid, $3, $4, 'queued', $5::uuid, $6::jsonb, $7::jsonb, now(), now()
                    )
                    """,
                    campaign_id,
                    user_id,
                    mode,
                    product_type,
                    quote_id,
                    json.dumps(request_json),
                    json.dumps(campaign_meta),
                )

            payload = {
                "quote_id": str(quote_id),
                "input": {"quote_id": str(quote_id)},
                "campaign_id": str(campaign_id),
                "quote_request": request_json,
                "request": request_json,
                "request_json": request_json,
                "resolved": {},
                "bootstrap_direct_enqueue": True,
            }
            meta = {
                "request_type": "commerce_confirm_bootstrap_direct",
                "campaign_id": str(campaign_id),
                "quote_id": str(quote_id),
                "mode": mode,
                "product_type": product_type,
                "resolution": resolution,
                "idempotency_key": idempotency_key or None,
                "bootstrap_direct_enqueue": True,
            }

            request_hash = _stable_hash_hex(
                {
                    "kind": "bootstrap_direct_enqueue",
                    "quote_id": str(quote_id),
                    "campaign_id": str(campaign_id),
                    "studio_job_id": str(studio_job_id),
                }
            )

            await con.execute(
                """
                insert into public.studio_jobs(
                    id, studio_type, status, request_hash, payload_json, meta_json, user_id, created_at, updated_at, next_run_at
                )
                values(
                    $1::uuid, 'commerce', 'queued', $2, $3::jsonb, $4::jsonb, $5::uuid, now(), now(), now()
                )
                """,
                studio_job_id,
                request_hash,
                json.dumps(payload),
                json.dumps(meta),
                user_id,
            )

            await con.execute(
                """
                update public.commerce_quotes
                set status = 'confirmed',
                    updated_at = now()
                where id = $1::uuid and user_id = $2::uuid
                """,
                quote_id,
                user_id,
            )

            await con.execute(
                """
                update public.commerce_campaigns
                set meta_json = coalesce(meta_json,'{}'::jsonb) || $2::jsonb,
                    status = 'queued',
                    updated_at = now()
                where id = $1::uuid
                """,
                campaign_id,
                json.dumps(
                    {
                        "studio_job_id": str(studio_job_id),
                        "quote_id": str(quote_id),
                        "mode": mode,
                        "resolution": resolution,
                        "bootstrap_direct_enqueue": True,
                    }
                ),
            )

    return {
        "campaign_id": str(campaign_id),
        "studio_job_id": str(studio_job_id),
        "status": "queued",
        "_bootstrap_confirm_variant": "direct_db_enqueue",
    }


async def _confirm(
    commerce_url: str,
    auth: AuthSession,
    quote_id: str,
    *,
    quote_request: Dict[str, Any],
    raw_out_path: str = "",
) -> Dict[str, Any]:
    payloads: List[Tuple[str, Dict[str, Any]]] = [
        (
            "quote_id_mode_product_resolution_quote_request",
            {
                "quote_id": quote_id,
                "mode": quote_request.get("mode"),
                "product_type": quote_request.get("product_type"),
                "resolution": quote_request.get("resolution", "hd"),
                "quote_request": quote_request,
            },
        ),
        (
            "quote_id_mode_product_resolution_request",
            {
                "quote_id": quote_id,
                "mode": quote_request.get("mode"),
                "product_type": quote_request.get("product_type"),
                "resolution": quote_request.get("resolution", "hd"),
                "request": quote_request,
            },
        ),
        (
            "quote_id_quote_request",
            {
                "quote_id": quote_id,
                "quote_request": quote_request,
            },
        ),
        (
            "quote_id_request",
            {
                "quote_id": quote_id,
                "request": quote_request,
            },
        ),
        (
            "quote_id_only",
            {
                "quote_id": quote_id,
            },
        ),
    ]

    attempt_errors: List[Dict[str, str]] = []

    for idx, (variant_name, body) in enumerate(payloads, start=1):
        attempt_raw = f"{raw_out_path}.attempt{idx}.txt" if raw_out_path else ""
        try:
            def _req(token: str, user_id: str) -> Dict[str, Any]:
                cmd = [
                    "curl", "-sS",
                    "-X", "POST", f"{commerce_url.rstrip('/')}/api/commerce/confirm",
                    "-H", "Content-Type: application/json",
                    "-H", f"Authorization: Bearer {token}",
                    "-H", f"X-User-Id: {user_id}",
                    "-d", json.dumps(body),
                ]
                return _curl_json(cmd, raw_out_path=attempt_raw)

            out = _with_auth_retry(auth, _req)
            if isinstance(out, dict):
                out = dict(out)
                out["_bootstrap_confirm_variant"] = variant_name
            return out
        except Exception as e:
            attempt_errors.append({"variant": variant_name, "error": str(e)})

    if raw_out_path:
        Path(f"{raw_out_path}.fallback_errors.json").write_text(
            json.dumps(attempt_errors, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    auth.ensure()
    fallback = await _enqueue_direct_job_from_quote(
        quote_id=str(quote_id),
        user_id=str(auth.user_id),
        idempotency_key=f"bootstrap:{quote_id}",
    )
    fallback["_bootstrap_confirm_http_errors"] = attempt_errors
    return fallback


def _status(
    commerce_url: str,
    auth: AuthSession,
    studio_job_id: str,
    include_payload: int = 1,
    raw_out_path: str = "",
) -> Dict[str, Any]:
    url = f"{commerce_url.rstrip('/')}/api/commerce/jobs/{studio_job_id}/status?include_payload={include_payload}"

    def _req(token: str, user_id: str) -> Dict[str, Any]:
        cmd = [
            "curl", "-sS",
            "-X", "GET", url,
            "-H", f"Authorization: Bearer {token}",
            "-H", f"X-User-Id: {user_id}",
        ]
        return _curl_json(cmd, raw_out_path=raw_out_path)

    return _with_auth_retry(auth, _req)


# ---------------------------------------------------------------------
# DB schema-adaptive helpers
# ---------------------------------------------------------------------


def _qid(s: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
        raise ValueError(f"unsafe identifier: {s}")
    return f'"{s}"'


async def _table_meta(conn: asyncpg.Connection, table_name: str) -> Dict[str, Dict[str, str]]:
    rows = await conn.fetch(
        """
        select
            column_name,
            data_type,
            udt_name,
            is_nullable,
            column_default
        from information_schema.columns
        where table_name = $1
        """,
        table_name,
    )
    return {
        str(r["column_name"]): {
            "data_type": str(r["data_type"]),
            "udt_name": str(r["udt_name"]),
            "is_nullable": str(r["is_nullable"]),
            "column_default": "" if r["column_default"] is None else str(r["column_default"]),
        }
        for r in rows
    }


def _fill_required_dataset_defaults(
    *,
    row: Dict[str, Any],
    table_meta: Dict[str, Dict[str, str]],
    training_container: str,
    prefix: str,
    dataset_id: str,
    dataset_name: str,
) -> Dict[str, Any]:
    out = dict(row)
    now = _utc_now()

    manifest_rel = f"{prefix}/manifest.json"
    summary_rel = f"{prefix}/summary.json"

    explicit_defaults: Dict[str, Any] = {
        "storage_container": training_container,
        "container": training_container,
        "storage_prefix": prefix,
        "blob_prefix": prefix,
        "dataset_prefix": prefix,
        "prefix": prefix,
        "root_prefix": prefix,
        "manifest_blob_name": manifest_rel,
        "manifest_blob_path": manifest_rel,
        "manifest_path": manifest_rel,
        "summary_blob_name": summary_rel,
        "summary_blob_path": summary_rel,
        "summary_path": summary_rel,
        "status": "draft",
        "is_frozen": False,
        "frozen_at": None,
        "example_count": 0,
        "train_count": 0,
        "val_count": 0,
        "test_count": 0,
        "meta_json": out.get("meta_json", {}),
        "manifest_json": out.get("manifest_json", {}),
        "stats_json": out.get("stats_json", {}),
        "created_at": now,
        "updated_at": now,
        "dataset_id": dataset_id,
        "name": dataset_name,
    }

    for col, meta in table_meta.items():
        if col in out and out[col] is not None:
            continue

        is_nullable = (meta.get("is_nullable") or "").upper()
        has_default = bool((meta.get("column_default") or "").strip())
        data_type = (meta.get("data_type") or "").lower()

        if col in explicit_defaults:
            out[col] = explicit_defaults[col]
            continue

        if is_nullable == "NO" and not has_default:
            if data_type in {"json", "jsonb"}:
                out[col] = {}
            elif data_type == "boolean":
                out[col] = False
            elif "timestamp" in data_type:
                out[col] = now
            elif data_type in {"character varying", "text"}:
                lc = col.lower()
                if "container" in lc:
                    out[col] = training_container
                elif "prefix" in lc or "path" in lc:
                    out[col] = prefix
                elif lc == "status":
                    out[col] = "draft"
                else:
                    out[col] = ""
            elif data_type in {"integer", "bigint", "smallint", "numeric", "double precision", "real"}:
                out[col] = 0

    return out


def _build_example_dedup_hash(row: Dict[str, Any]) -> str:
    payload = {
        "dataset_id": str(row.get("dataset_id") or ""),
        "example_key": str(row.get("example_key") or ""),
        "provider_kind": str(row.get("provider_kind") or ""),
        "provider_job_id": str(row.get("provider_job_id") or ""),
        "source_model_url": str(row.get("source_model_url") or ""),
        "source_garment_url": str(row.get("source_garment_url") or ""),
        "target_image_url": str(
            row.get("target_image_url")
            or row.get("output_url")
            or row.get("artifact_url")
            or ""
        ),
    }
    return _stable_hash_hex(payload)


def _fill_required_training_example_defaults(
    *,
    row: Dict[str, Any],
    table_meta: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    out = dict(row)
    now = _utc_now()

    if "dedup_hash" in table_meta and not out.get("dedup_hash"):
        out["dedup_hash"] = _build_example_dedup_hash(out)

    if "created_at" in table_meta and out.get("created_at") is None:
        out["created_at"] = now
    if "updated_at" in table_meta and out.get("updated_at") is None:
        out["updated_at"] = now

    if "status" in table_meta and not out.get("status"):
        out["status"] = "ready"

    for col, meta in table_meta.items():
        if col in out and out[col] is not None:
            continue

        is_nullable = (meta.get("is_nullable") or "").upper()
        has_default = bool((meta.get("column_default") or "").strip())
        data_type = (meta.get("data_type") or "").lower()

        if is_nullable == "NO" and not has_default:
            if data_type in {"json", "jsonb"}:
                out[col] = {}
            elif data_type == "boolean":
                out[col] = False
            elif "timestamp" in data_type:
                out[col] = now
            elif data_type in {"character varying", "text"}:
                if col == "dedup_hash":
                    out[col] = _build_example_dedup_hash(out)
                elif col == "status":
                    out[col] = "ready"
                else:
                    out[col] = ""
            elif data_type in {"integer", "bigint", "smallint", "numeric", "double precision", "real"}:
                out[col] = 0

    return out


def _db_placeholder(idx: int, col_meta: Dict[str, str]) -> str:
    dt_name = (col_meta.get("data_type") or "").lower()
    udt_name = (col_meta.get("udt_name") or "").lower()

    if dt_name in {"json", "jsonb"}:
        return f"${idx}"
    if dt_name == "uuid":
        return f"${idx}::uuid"
    if "timestamp" in dt_name:
        return f"${idx}::timestamptz"
    if dt_name == "date":
        return f"${idx}::date"
    if dt_name == "ARRAY" or udt_name.startswith("_"):
        return f"${idx}"
    return f"${idx}"


def _db_value(val: Any, col_meta: Dict[str, str]) -> Any:
    dt_name = (col_meta.get("data_type") or "").lower()
    if val is None:
        return None

    if dt_name in {"json", "jsonb"}:
        # Keep dict/list as native Python structures so asyncpg stores JSON objects,
        # not JSON strings embedded inside jsonb.
        if isinstance(val, (dict, list, int, float, bool)):
            return val
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return {}
            try:
                parsed = json.loads(s)
                return parsed
            except Exception:
                return val
        return val

    return val


async def _dynamic_insert(
    conn: asyncpg.Connection,
    *,
    table_name: str,
    row: Dict[str, Any],
    table_meta: Dict[str, Dict[str, str]],
) -> None:
    payload = {k: v for k, v in row.items() if k in table_meta}
    if not payload:
        raise RuntimeError(f"no matching columns for insert into {table_name}")

    cols = list(payload.keys())
    vals = [_db_value(payload[c], table_meta[c]) for c in cols]
    cols_sql = ", ".join(_qid(c) for c in cols)
    vals_sql = ", ".join(_db_placeholder(i + 1, table_meta[cols[i]]) for i in range(len(cols)))
    sql = f"insert into {_qid(table_name)} ({cols_sql}) values ({vals_sql})"
    await conn.execute(sql, *vals)


async def _dynamic_update(
    conn: asyncpg.Connection,
    *,
    table_name: str,
    where_col: str,
    where_val: Any,
    updates: Dict[str, Any],
    table_meta: Dict[str, Dict[str, str]],
) -> None:
    payload = {k: v for k, v in updates.items() if k in table_meta and k != where_col}
    if not payload:
        return

    cols = list(payload.keys())
    vals = [_db_value(payload[c], table_meta[c]) for c in cols]
    set_sql = ", ".join(
        f"{_qid(cols[i])} = {_db_placeholder(i + 1, table_meta[cols[i]])}" for i in range(len(cols))
    )
    where_idx = len(cols) + 1
    sql = f"update {_qid(table_name)} set {set_sql} where {_qid(where_col)} = {_db_placeholder(where_idx, table_meta[where_col])}"
    vals.append(_db_value(where_val, table_meta[where_col]))
    await conn.execute(sql, *vals)


# ---------------------------------------------------------------------
# dataset helpers
# ---------------------------------------------------------------------

def _zero_summary_counts() -> Dict[str, int]:
    return {
        "total": 0,
        "train": 0,
        "val": 0,
        "test": 0,
        "rejected": 0,
        "review": 0,
        "failed_jobs": 0,
        "jobs_started": 0,
        "generated_images_est": 0,
        "budget_exhausted": 0,
        "skipped_pairs": 0,
    }


def _initial_dataset_stats(*, recipe_json: Dict[str, Any]) -> Dict[str, Any]:
    budget_cfg = _as_dict_loose(_as_dict_loose(recipe_json).get("budget"))
    counts = _zero_summary_counts()
    budget = {
        "budget_usd_cap": _safe_float(budget_cfg.get("budget_usd_cap"), 0.0),
        "estimated_cost_per_generated_image_usd": _safe_float(
            budget_cfg.get("estimated_cost_per_generated_image_usd"), 0.0
        ),
        "estimated_images_generated": 0,
        "estimated_total_cost_usd": 0.0,
    }
    out: Dict[str, Any] = dict(counts)
    out["counts"] = dict(counts)
    out["budget"] = budget
    out["status"] = "draft"
    out["updated_at"] = _utc_now().isoformat()
    return out


def _build_dataset_stats(*, summary: Dict[str, Any]) -> Dict[str, Any]:
    counts_in = _as_dict_loose(summary.get("counts"))
    budget_in = _as_dict_loose(summary.get("budget"))

    counts = {
        "total": _safe_int(counts_in.get("total")),
        "train": _safe_int(counts_in.get("train")),
        "val": _safe_int(counts_in.get("val")),
        "test": _safe_int(counts_in.get("test")),
        "rejected": _safe_int(counts_in.get("rejected")),
        "review": _safe_int(counts_in.get("review")),
        "failed_jobs": _safe_int(counts_in.get("failed_jobs")),
        "jobs_started": _safe_int(counts_in.get("jobs_started")),
        "generated_images_est": _safe_int(counts_in.get("generated_images_est")),
        "budget_exhausted": _safe_int(counts_in.get("budget_exhausted")),
        "skipped_pairs": _safe_int(counts_in.get("skipped_pairs")),
    }

    budget = {
        "budget_usd_cap": _safe_float(budget_in.get("budget_usd_cap")),
        "estimated_cost_per_generated_image_usd": _safe_float(
            budget_in.get("estimated_cost_per_generated_image_usd")
        ),
        "estimated_images_generated": _safe_int(budget_in.get("estimated_images_generated")),
        "estimated_total_cost_usd": _safe_float(budget_in.get("estimated_total_cost_usd")),
    }

    out: Dict[str, Any] = dict(counts)
    out["counts"] = dict(counts)
    out["budget"] = budget
    out["cases_count"] = len(_as_list(summary.get("cases")))
    out["review_cases_count"] = len(_as_list(summary.get("review_cases")))
    out["source_rejected_count"] = len(_as_list(summary.get("source_rejected")))
    out["status"] = "frozen"
    out["updated_at"] = _utc_now().isoformat()
    return out

async def _create_dataset(
    *,
    pool: asyncpg.Pool,
    dataset_name: str,
    training_container: str,
    recipe_json: Dict[str, Any],
) -> Tuple[str, str]:
    stamp = _utc_stamp()

    use_dataset_service = (
        TrainingDatasetService is not None
        and AzureStorageService is not None
        and _truthy_env("DF_USE_TRAINING_DATASET_SERVICE", "0")
    )

    if use_dataset_service:
        try:
            svc = TrainingDatasetService(
                pool=pool,
                storage=AzureStorageService(),
                training_container=training_container,
            )
            dataset_id, prefix = await svc.create_dataset(
                name=dataset_name,
                kind="synthetic",
                usage_scope="commercial_ok",
                recipe_json=recipe_json,
            )
            prefix_s = str(prefix)

            try:
                async with pool.acquire() as conn:
                    meta = await _table_meta(conn, "training_datasets")
                    await _dynamic_update(
                        conn,
                        table_name="training_datasets",
                        where_col="id",
                        where_val=dataset_id,
                        updates={
                            "stats_json": _initial_dataset_stats(recipe_json=recipe_json),
                            "updated_at": _utc_now(),
                        },
                        table_meta=meta,
                    )
            except Exception as e:
                print(f"[warn] dataset service create succeeded but initial stats_json update failed: {e}")

            return str(dataset_id), prefix_s
        except Exception as e:
            print(f"[warn] TrainingDatasetService.create_dataset failed; falling back to SQL: {e}")

    dataset_id = str(uuid4())
    prefix = f"training/non_saree_vton/{stamp}/{dataset_id}"
    now = _utc_now()

    async with pool.acquire() as conn:
        meta = await _table_meta(conn, "training_datasets")

        base_row = {
            "id": dataset_id,
            "name": dataset_name,
            "kind": "synthetic",
            "usage_scope": "commercial_ok",
            "status": "draft",
            "storage_container": training_container,
            "storage_prefix": prefix,
            "blob_prefix": prefix,
            "dataset_prefix": prefix,
            "recipe_json": recipe_json,
            "meta_json": {
                "source": "bootstrap_non_saree_vton_dataset",
                "prefix": prefix,
                "storage_container": training_container,
                "created_at": now.isoformat(),
            },
            "manifest_json": {
                "source": "bootstrap_non_saree_vton_dataset",
                "prefix": prefix,
                "storage_container": training_container,
            },
            "stats_json": _initial_dataset_stats(recipe_json=recipe_json),
            "is_frozen": False,
            "frozen_at": None,
            "example_count": 0,
            "train_count": 0,
            "val_count": 0,
            "test_count": 0,
            "created_at": now,
            "updated_at": now,
        }

        row = _fill_required_dataset_defaults(
            row=base_row,
            table_meta=meta,
            training_container=training_container,
            prefix=prefix,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
        )

        await _dynamic_insert(conn, table_name="training_datasets", row=row, table_meta=meta)

    return dataset_id, prefix


async def _insert_training_example(
    *,
    pool: asyncpg.Pool,
    example_row: Dict[str, Any],
) -> None:
    async with pool.acquire() as conn:
        meta = await _table_meta(conn, "training_examples")
        row = _fill_required_training_example_defaults(row=example_row, table_meta=meta)
        await _dynamic_insert(conn, table_name="training_examples", row=row, table_meta=meta)


async def _freeze_dataset(
    *,
    pool: asyncpg.Pool,
    dataset_id: str,
    dataset_summary: Dict[str, Any],
    summary: Dict[str, Any],
) -> None:
    counts = _as_dict_loose(summary.get("counts"))
    stats_json = _build_dataset_stats(summary=summary)
    now = _utc_now()

    async with pool.acquire() as conn:
        meta = await _table_meta(conn, "training_datasets")

        existing_meta_json: Dict[str, Any] = {}
        if "meta_json" in meta:
            try:
                row = await conn.fetchrow(
                    f"select {_qid('meta_json')} from {_qid('training_datasets')} where {_qid('id')} = $1::uuid",
                    dataset_id,
                )
                if row:
                    existing_meta_json = _as_dict_loose(row["meta_json"])
            except Exception:
                existing_meta_json = {}

        dataset_prefix = str(dataset_summary.get("dataset_prefix") or existing_meta_json.get("dataset_prefix") or "")
        merged_meta_json = dict(existing_meta_json)
        merged_meta_json.update(
            {
                "source": "bootstrap_non_saree_vton_dataset",
                "dataset_id": dataset_id,
                "dataset_prefix": dataset_prefix or None,
                "dataset_summary": dataset_summary,
                "stats_json": stats_json,
                "finalized_at": now.isoformat(),
            }
        )
        if dataset_prefix:
            merged_meta_json["manifest_blob_name"] = f"{dataset_prefix}/manifest.json"
            merged_meta_json["summary_blob_name"] = f"{dataset_prefix}/summary.json"

        updates = {
            "status": "frozen",
            "is_frozen": True,
            "frozen_at": now,
            "updated_at": now,
            "example_count": _safe_int(counts.get("total")),
            "train_count": _safe_int(counts.get("train")),
            "val_count": _safe_int(counts.get("val")),
            "test_count": _safe_int(counts.get("test")),
            "manifest_json": dataset_summary,
            "meta_json": merged_meta_json,
            "stats_json": stats_json,
        }
        try:
            await _dynamic_update(
                conn,
                table_name="training_datasets",
                where_col="id",
                where_val=dataset_id,
                updates=updates,
                table_meta=meta,
            )
        except Exception:
            safe_updates = dict(updates)
            safe_updates.pop("status", None)
            await _dynamic_update(
                conn,
                table_name="training_datasets",
                where_col="id",
                where_val=dataset_id,
                updates=safe_updates,
                table_meta=meta,
            )


# ---------------------------------------------------------------------
# checkpoint / selection helpers
# ---------------------------------------------------------------------


def _load_checkpoint(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {
            "completed_pair_outputs": [],
            "skipped_pairs": [],
            "rejected_pair_outputs": [],
            "counts": {},
        }
    return _as_dict_loose(_read_json(path))


def _save_checkpoint(path: str, cp: Dict[str, Any]) -> None:
    _write_json(path, cp)


def _cp_add(cp: Dict[str, Any], key: str, value: str) -> None:
    arr = list(cp.get(key) or [])
    if value not in arr:
        arr.append(value)
    cp[key] = arr


def _filter_seeds(all_seeds: Sequence[Dict[str, Any]], only_families: Sequence[str]) -> List[Dict[str, Any]]:
    wanted = {_canon_family(x.strip()) for x in only_families if x.strip()}
    if not wanted:
        return list(all_seeds)
    return [s for s in all_seeds if _canon_family(str(s.get("seed_name") or "")) in wanted]


def _eligible_model_for_family(model: Dict[str, Any], seed_name: str) -> bool:
    pref = _preferred_gender_for_seed(seed_name)
    if not pref:
        return True
    return str(model.get("gender") or "").strip().lower() == pref


def _pick_models_for_garment(
    *,
    garment_kind: str,
    seed_name: str,
    selector,
    preferred_tags: Sequence[str],
    max_models: int,
    rotation_key: str,
) -> List[Dict[str, Any]]:
    candidates = selector.list_eligible_models(
        garment_kind=garment_kind,
        preferred_tags=list(preferred_tags) or None,
    )
    if not candidates:
        return []

    allow_codes = set(_env_csv("DF_NONSAREE_MODEL_WHITELIST"))
    deny_codes = set(_env_csv("DF_NONSAREE_MODEL_BLACKLIST"))

    if allow_codes:
        allowed = [c for c in candidates if str(c.get("model_code") or "") in allow_codes]
        if allowed:
            candidates = allowed

    if deny_codes:
        filtered = [c for c in candidates if str(c.get("model_code") or "") not in deny_codes]
        if filtered:
            candidates = filtered

    fam_candidates = [c for c in candidates if _eligible_model_for_family(c, seed_name)]
    if fam_candidates:
        candidates = fam_candidates

    def _pose_rank(c: Dict[str, Any]) -> Tuple[int, float]:
        framing = str(c.get("framing") or "").strip().lower()
        pose = str(c.get("pose") or "").strip().lower()
        quality = float(c.get("quality_score") or 0.0)
        framing_ok = 1 if "full" in framing else 0
        pose_ok = 1 if pose in {"front", "straight", "neutral", "standing", "standing_front"} else 0
        return (framing_ok + pose_ok, quality)

    candidates = sorted(
        candidates,
        key=lambda c: (
            -_pose_rank(c)[0],
            -_pose_rank(c)[1],
            str(c.get("model_code") or ""),
        ),
    )

    max_models = max(1, min(max_models, len(candidates)))
    start = _stable_hash_int(rotation_key) % len(candidates)

    out: List[Dict[str, Any]] = []
    seen_codes: Set[str] = set()
    for i in range(len(candidates)):
        idx = (start + i) % len(candidates)
        c = candidates[idx]
        code = str(c.get("model_code") or "")
        if code and code in seen_codes:
            continue
        if code:
            seen_codes.add(code)
        out.append(c)
        if len(out) >= max_models:
            break
    return out


# ---------------------------------------------------------------------
# main workflow
# ---------------------------------------------------------------------

async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core-url", default=os.environ.get("CORE_URL", "http://localhost:8000"))
    ap.add_argument("--commerce-url", default=os.environ.get("COMMERCE_URL", "http://localhost:8008"))
    ap.add_argument("--email", default=os.environ.get("DF_EMAIL", ""))
    ap.add_argument("--password", default=os.environ.get("DF_PASSWORD", ""))
    ap.add_argument("--dataset-name", default=f"non_saree_vton_bootstrap_{_utc_stamp()}")
    ap.add_argument("--training-container", default=os.environ.get("COMMERCE_TRAINING_CONTAINER", "commerce-training"))
    ap.add_argument("--platform-models-manifest", default=os.environ.get("COMMERCE_PLATFORM_MODELS_MANIFEST", ""))
    ap.add_argument("--preferred-tags", default="", help="comma-separated selector tags, e.g. maharashtra,studio")
    ap.add_argument("--only-families", default="", help="comma-separated families to run")
    ap.add_argument("--garment-manifest", default=os.environ.get("DF_NONSAREE_GARMENT_MANIFEST", ""), help="JSON/JSONL/CSV manifest of owned/vendor garments")
    ap.add_argument("--allow-public-fallback", action="store_true", help="Allow Wikimedia fallback if manifest coverage is missing")
    ap.add_argument("--max-garments-per-family", type=int, default=8)
    ap.add_argument("--max-models-per-garment", type=int, default=2)
    ap.add_argument("--num-images-per-job", type=int, default=2)
    ap.add_argument("--max-outputs-per-job", type=int, default=1)
    ap.add_argument("--max-total-jobs", type=int, default=256)
    ap.add_argument("--max-total-generations", type=int, default=384)
    ap.add_argument("--max-total-accepts", type=int, default=128)
    ap.add_argument("--estimated-cost-per-generated-image-usd", type=float, default=0.04)
    ap.add_argument("--budget-usd-cap", type=float, default=100.0)
    ap.add_argument("--poll-timeout-s", type=int, default=420)
    ap.add_argument("--poll-interval-s", type=int, default=5)
    ap.add_argument("--force-refresh-manifest", action="store_true")
    ap.add_argument("--run-dir", default="")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    if not args.email or not args.password:
        raise SystemExit("ERROR: set DF_EMAIL/DF_PASSWORD or pass --email/--password")

    if not args.garment_manifest and not args.allow_public_fallback:
        raise SystemExit("ERROR: pass --garment-manifest for owned/vendor garments, or explicitly opt into --allow-public-fallback")

    preferred_tags = [x.strip() for x in args.preferred_tags.split(",") if x.strip()]
    only_families = [x.strip() for x in args.only_families.split(",") if x.strip()]

    run_dir = args.run_dir.strip() or f"/tmp/df_bootstrap_non_saree_vton_{_utc_stamp()}"
    garments_dir = os.path.join(run_dir, "garments")
    models_dir = os.path.join(run_dir, "models")
    outputs_dir = os.path.join(run_dir, "outputs")
    _mkdirp(run_dir)
    _mkdirp(garments_dir)
    _mkdirp(models_dir)
    _mkdirp(outputs_dir)
    checkpoint_path = os.path.join(run_dir, "checkpoint.json")

    print("RUN_DIR=", run_dir)

    auth = AuthSession(core_url=args.core_url, email=args.email, password=args.password)
    token, user_id = auth.login()
    _write_json(os.path.join(run_dir, "auth.json"), {"access_token": token, "user_id": user_id, "exp_ts": auth.exp_ts})
    print("Auth OK. X_USER_ID=", user_id)

    os.environ.setdefault("HF_HOME", "/tmp/huggingface")
    os.environ.setdefault("HF_HUB_CACHE", "/tmp/huggingface/hub")

    qc_gate = NonSareeQCGate.from_env()
    pool = await get_pool()

    recipe_json = {
        "family": "non_saree",
        "task": "vton_non_saree_tryon",
        "version": "v2_manifest_first",
        "source_models": "azure_platform_model_selector",
        "source_garments": "manifest_first",
        "garment_manifest": args.garment_manifest or None,
        "allow_public_fallback": bool(args.allow_public_fallback),
        "provider": "svc-commerce platform_models",
        "script": "bootstrap_non_saree_vton_dataset.py",
        "preferred_tags": preferred_tags,
        "budget": {
            "budget_usd_cap": float(args.budget_usd_cap),
            "estimated_cost_per_generated_image_usd": float(args.estimated_cost_per_generated_image_usd),
            "max_total_jobs": int(args.max_total_jobs),
            "max_total_generations": int(args.max_total_generations),
            "max_total_accepts": int(args.max_total_accepts),
        },
        "created_at": _utc_now().isoformat(),
        "qc": {
            "type": "garment_family_strict",
            "require_clip_classifier": qc_gate.config.require_clip_classifier,
            "clip_model_id": qc_gate.config.clip_model_id,
            "allow_without_clip": qc_gate.config.allow_without_clip,
        },
    }

    dataset_id, dataset_prefix = await _create_dataset(
        pool=pool,
        dataset_name=args.dataset_name,
        training_container=args.training_container,
        recipe_json=recipe_json,
    )
    print("DATASET_ID=", dataset_id)
    print("DATASET_PREFIX=", dataset_prefix)

    selector = get_platform_model_selector(
        manifest_uri=args.platform_models_manifest or None,
    )
    if args.force_refresh_manifest:
        try:
            selector.refresh_manifest()
        except Exception as e:
            print(f"[warn] selector.refresh_manifest failed: {e}")

    seeds = _filter_seeds(DEFAULT_SEEDS, only_families)

    garments: List[Dict[str, Any]] = []
    if args.garment_manifest:
        all_manifest_items = _load_manifest_records(args.garment_manifest)
        all_manifest_items = _dedupe_garment_candidates(all_manifest_items)
        allowed_fams = {_canon_family(str(s["seed_name"])) for s in seeds}
        by_family: Dict[str, List[Dict[str, Any]]] = {fam: [] for fam in allowed_fams}
        for item in all_manifest_items:
            fam = _canon_family(str(item.get("seed_name") or ""))
            if fam not in allowed_fams:
                continue
            by_family.setdefault(fam, []).append(item)
        for seed in seeds:
            fam = _canon_family(str(seed["seed_name"]))
            garments.extend(by_family.get(fam, [])[: max(1, args.max_garments_per_family)])

    missing_fams = []
    selected_fams = {_canon_family(str(g.get("seed_name") or "")) for g in garments}
    for seed in seeds:
        fam = _canon_family(str(seed["seed_name"]))
        if fam not in selected_fams:
            missing_fams.append(seed)

    if missing_fams and args.allow_public_fallback:
        print("[warn] manifest missing coverage for:", ", ".join(str(s["seed_name"]) for s in missing_fams))
        garments.extend(
            _gather_garment_candidates_from_public(
                seeds=missing_fams,
                max_per_seed=max(1, args.max_garments_per_family),
            )
        )

    garments = _dedupe_garment_candidates(garments)
    _write_json(os.path.join(run_dir, "garment_candidates.json"), {"items": garments})
    print("GARMENT_CANDIDATES=", len(garments))

    cp = _load_checkpoint(checkpoint_path) if args.resume else {
        "completed_pair_outputs": [],
        "skipped_pairs": [],
        "rejected_pair_outputs": [],
        "counts": {},
    }
    completed_pair_outputs = set(cp.get("completed_pair_outputs") or [])
    rejected_pair_outputs = set(cp.get("rejected_pair_outputs") or [])
    skipped_pairs = set(cp.get("skipped_pairs") or [])

    summary: Dict[str, Any] = {
        "run_dir": run_dir,
        "dataset_id": dataset_id,
        "dataset_prefix": dataset_prefix,
        "dataset_name": args.dataset_name,
        "core_url": args.core_url,
        "commerce_url": args.commerce_url,
        "preferred_tags": preferred_tags,
        "garment_manifest": args.garment_manifest or None,
        "cases": [],
        "rejected": [],
        "review_cases": [],
        "counts": _zero_summary_counts(),
        "budget": {
            "budget_usd_cap": float(args.budget_usd_cap),
            "estimated_cost_per_generated_image_usd": float(args.estimated_cost_per_generated_image_usd),
            "estimated_images_generated": 0,
            "estimated_total_cost_usd": 0.0,
        },
    }

    inserted_examples: List[Dict[str, Any]] = []

    upload_cache_path = os.path.join(run_dir, "upload_cache.json")
    upload_cache = _read_json(upload_cache_path) if (args.resume and os.path.exists(upload_cache_path)) else {}

    def _estimated_total_cost() -> float:
        return float(summary["budget"]["estimated_images_generated"]) * float(args.estimated_cost_per_generated_image_usd)

    def _budget_guard(next_images: int = 0) -> bool:
        projected_images = int(summary["budget"]["estimated_images_generated"]) + int(next_images)
        projected_cost = projected_images * float(args.estimated_cost_per_generated_image_usd)
        if projected_images > int(args.max_total_generations):
            return False
        if projected_cost > float(args.budget_usd_cap):
            return False
        return True

    for idx, garment in enumerate(garments):
        if int(summary["counts"]["total"]) >= int(args.max_total_accepts):
            print("[stop] max accepted examples reached")
            break
        if int(summary["counts"]["jobs_started"]) >= int(args.max_total_jobs):
            print("[stop] max total jobs reached")
            break
        if not _budget_guard(0):
            print("[stop] budget exhausted before starting next garment")
            summary["counts"]["budget_exhausted"] += 1
            break

        seed_name = str(garment["seed_name"])
        garment_kind = str(garment["garment_kind"])
        outfit_kind = str(garment["outfit_kind"])
        component_code = str(garment["component_code"])
        title = str(garment["title"])
        garment_source_url = str(garment["image_url"])
        license_name = str(garment.get("license_name") or "")
        usage_terms = str(garment.get("usage_terms") or "")
        description_url = str(garment.get("description_url") or "")
        source_type = str(garment.get("source_type") or "manifest")

        case_slug = _slug(f"{seed_name}_{idx}_{title}", max_len=100)
        ext = _guess_ext_from_url(garment_source_url, ".jpg")
        local_garment_path = os.path.join(garments_dir, f"{case_slug}{ext}")

        print(f"\n=== GARMENT {idx+1}/{len(garments)}: {seed_name} | {title} ===")
        used_download_url = _download(garment_source_url, local_garment_path)
        print("[garment] downloaded:", used_download_url)

        source_reject_reason, source_clip_info = _source_garment_reject_reason(
            seed_name=seed_name,
            garment_title=title,
            garment_query=str(garment.get("query") or ""),
            local_garment_path=local_garment_path,
            clip_model_id=qc_gate.config.clip_model_id,
        )

        _write_json(
            os.path.join(run_dir, f"{case_slug}_source_preflight.json"),
            {
                "seed_name": seed_name,
                "title": title,
                "garment_source_url": garment_source_url,
                "reject_reason": source_reject_reason,
                "clip": source_clip_info,
            },
        )

        if source_reject_reason:
            print(f"[source-preflight] rejected garment={case_slug} reason={source_reject_reason}")
            summary["counts"]["skipped_pairs"] += 1
            summary.setdefault("source_rejected", []).append(
                {
                    "seed_name": seed_name,
                    "garment_kind": garment_kind,
                    "outfit_kind": outfit_kind,
                    "component_code": component_code,
                    "garment_title": title,
                    "garment_source_url": garment_source_url,
                    "reason": source_reject_reason,
                    "clip": source_clip_info,
                }
            )
            _cp_add(cp, "skipped_pairs", case_slug)
            skipped_pairs.add(case_slug)
            _save_checkpoint(checkpoint_path, cp)
            continue

        garment_dataset_blob = f"{dataset_prefix}/inputs/garments/{garment_kind}/{case_slug}{ext}"
        garment_dataset_az = _upload_file_to_azure(
            local_path=local_garment_path,
            container=args.training_container,
            blob_name=garment_dataset_blob,
            overwrite=True,
        )

        cache_key = f"upload:{_url_key(garment_source_url)}"
        upload_resp = _as_dict_loose(upload_cache.get(cache_key))
        garment_preview_url = (
            upload_resp.get("preview_url")
            or upload_resp.get("url")
            or upload_resp.get("asset_url")
            or upload_resp.get("blob_url")
            or ""
        )
        if not garment_preview_url:
            upload_resp = _upload_asset(args.commerce_url, auth, "garment_full", local_garment_path)
            upload_cache[cache_key] = upload_resp
            _write_json(upload_cache_path, upload_cache)
            _write_json(os.path.join(run_dir, f"{case_slug}_upload.json"), upload_resp)
            garment_preview_url = (
                upload_resp.get("preview_url")
                or upload_resp.get("url")
                or upload_resp.get("asset_url")
                or upload_resp.get("blob_url")
                or ""
            )
        if not garment_preview_url:
            print("[warn] upload missing preview URL; skipping garment")
            summary["counts"]["failed_jobs"] += 1
            continue

        picked_models = _pick_models_for_garment(
            garment_kind=garment_kind,
            seed_name=seed_name,
            selector=selector,
            preferred_tags=preferred_tags,
            max_models=max(1, args.max_models_per_garment),
            rotation_key=f"{dataset_id}:{case_slug}:{garment_kind}",
        )

        if not picked_models:
            print("[warn] no eligible models; skipping garment")
            summary["counts"]["failed_jobs"] += 1
            continue

        for _model_idx, model in enumerate(picked_models):
            if int(summary["counts"]["total"]) >= int(args.max_total_accepts):
                break
            if int(summary["counts"]["jobs_started"]) >= int(args.max_total_jobs):
                break
            if not _budget_guard(int(args.num_images_per_job)):
                print("[stop] budget exhausted before starting next pair")
                summary["counts"]["budget_exhausted"] += 1
                break

            model_code = str(model["model_code"])
            model_catalog_url = str(model["primary_asset_url"])
            model_request_url = _resolve_model_url_for_request(model_catalog_url)
            model_gender = str(model.get("gender") or "")

            model_ext = _guess_ext_from_url(model_request_url, ".jpg")
            local_model_path = os.path.join(models_dir, f"{model_code}{model_ext}")
            if not os.path.exists(local_model_path):
                _download(model_request_url, local_model_path)

            pair_key = f"{dataset_id}:{case_slug}:{model_code}"
            pair_slug = _slug(f"{case_slug}_{model_code}", max_len=120)
            if pair_slug in skipped_pairs:
                summary["counts"]["skipped_pairs"] += 1
                continue
            print(f"[pair] model={model_code}")

            quote_meta = {
                "dataset_id": dataset_id,
                "dataset_prefix": dataset_prefix,
                "seed_name": seed_name,
                "garment_title": title,
                "model_code": model_code,
                "source_type": source_type,
            }

            quote_body = _build_quote_body(
                seed_name=seed_name,
                garment_preview_url=garment_preview_url,
                component_code=component_code,
                garment_kind=garment_kind,
                outfit_kind=outfit_kind,
                model_url=model_request_url,
                model_gender=model_gender,
                preferred_tags=preferred_tags,
                meta_extra=quote_meta,
                num_images=max(1, args.num_images_per_job),
            )
            _write_json(os.path.join(run_dir, f"{pair_slug}_quote_request.json"), quote_body)

            q = _quote(
                args.commerce_url,
                auth,
                quote_body=quote_body,
                raw_out_path=os.path.join(run_dir, f"{pair_slug}_quote_raw.txt"),
            )
            _write_json(os.path.join(run_dir, f"{pair_slug}_quote.json"), q)

            quote_id = q.get("quote_id")
            if not quote_id:
                print("[warn] quote missing quote_id; skipping pair")
                summary["counts"]["failed_jobs"] += 1
                _cp_add(cp, "skipped_pairs", pair_slug)
                skipped_pairs.add(pair_slug)
                _save_checkpoint(checkpoint_path, cp)
                continue

            conf = await _confirm(
                args.commerce_url,
                auth,
                str(quote_id),
                quote_request=quote_body,
                raw_out_path=os.path.join(run_dir, f"{pair_slug}_confirm_raw"),
            )
            _write_json(os.path.join(run_dir, f"{pair_slug}_confirm.json"), conf)

            studio_job_id = conf.get("studio_job_id") or conf.get("job_id")
            if not studio_job_id:
                print("[warn] confirm missing studio_job_id; skipping pair")
                summary["counts"]["failed_jobs"] += 1
                _cp_add(cp, "skipped_pairs", pair_slug)
                skipped_pairs.add(pair_slug)
                _save_checkpoint(checkpoint_path, cp)
                continue

            summary["counts"]["jobs_started"] += 1
            summary["budget"]["estimated_images_generated"] += int(args.num_images_per_job)
            summary["counts"]["generated_images_est"] = int(summary["budget"]["estimated_images_generated"])
            summary["budget"]["estimated_total_cost_usd"] = round(_estimated_total_cost(), 4)
            _save_checkpoint(checkpoint_path, cp)

            t0 = time.time()
            last: Dict[str, Any] = {}
            while True:
                st = _status(
                    args.commerce_url,
                    auth,
                    str(studio_job_id),
                    include_payload=1,
                    raw_out_path=os.path.join(run_dir, f"{pair_slug}_status_raw.txt"),
                )
                last = st if isinstance(st, dict) else {"raw": st}
                status = str(last.get("status") or last.get("stage") or "").lower()
                print("[poll]", status)
                if status in {"succeeded", "failed", "aborted", "canceled", "cancelled"}:
                    break
                if time.time() - t0 > args.poll_timeout_s:
                    last["status"] = "failed"
                    last["failure_reason"] = f"timeout>{args.poll_timeout_s}s"
                    break
                time.sleep(args.poll_interval_s)

            _write_json(os.path.join(run_dir, f"{pair_slug}_status.json"), last)

            final_status = str(last.get("status") or last.get("stage") or "").lower()
            if final_status != "succeeded":
                print("[warn] pair failed")
                summary["counts"]["failed_jobs"] += 1
                summary["cases"].append(
                    {
                        "pair_slug": pair_slug,
                        "status": final_status,
                        "seed_name": seed_name,
                        "garment_kind": garment_kind,
                        "outfit_kind": outfit_kind,
                        "component_code": component_code,
                        "garment_title": title,
                        "model_code": model_code,
                        "model_gender": model_gender,
                        "quote_id": quote_id,
                        "studio_job_id": studio_job_id,
                        "confirm_variant": conf.get("_bootstrap_confirm_variant"),
                        "failure_excerpt": json.dumps(last, default=str)[:2000],
                    }
                )
                _cp_add(cp, "skipped_pairs", pair_slug)
                skipped_pairs.add(pair_slug)
                _save_checkpoint(checkpoint_path, cp)
                continue

            exclude_urls = [
                garment_source_url,
                garment_preview_url,
                model_catalog_url,
                model_request_url,
                str(upload_resp.get("preview_url") or ""),
                str(upload_resp.get("url") or ""),
                str(upload_resp.get("asset_url") or ""),
                str(upload_resp.get("blob_url") or ""),
                str(quote_body.get("model_ref", {}).get("human_image_url") or ""),
                str(quote_body.get("model_ref", {}).get("url") or ""),
                str(quote_body.get("product_assets", {}).get("garment_image_url") or ""),
                str(quote_body.get("product_assets", {}).get("primary_image_url") or ""),
            ]

            chosen_outputs = _choose_output_urls(
                last,
                max_outputs=max(1, args.max_outputs_per_job),
                exclude_urls=exclude_urls,
            )
            if not chosen_outputs:
                print("[warn] no usable generated output URLs after success")
                summary["counts"]["failed_jobs"] += 1
                summary["cases"].append(
                    {
                        "pair_slug": pair_slug,
                        "status": "failed_output_selection",
                        "seed_name": seed_name,
                        "garment_kind": garment_kind,
                        "outfit_kind": outfit_kind,
                        "component_code": component_code,
                        "garment_title": title,
                        "model_code": model_code,
                        "model_gender": model_gender,
                        "quote_id": quote_id,
                        "studio_job_id": studio_job_id,
                        "confirm_variant": conf.get("_bootstrap_confirm_variant"),
                        "candidate_urls": _extract_candidate_output_urls(last)[:20],
                    }
                )
                _cp_add(cp, "skipped_pairs", pair_slug)
                skipped_pairs.add(pair_slug)
                _save_checkpoint(checkpoint_path, cp)
                continue

            pair_accepted = False
            for out_rank, out_item in enumerate(chosen_outputs):
                pair_output_key = f"{pair_slug}:r{out_rank}"
                if pair_output_key in completed_pair_outputs or pair_output_key in rejected_pair_outputs:
                    continue
                output_url = str(out_item["url"])
                output_probe = out_item["probe"]

                local_target_path = os.path.join(outputs_dir, f"{pair_slug}_r{out_rank}.png")
                _download(output_url, local_target_path)

                expected_family = _expected_family(seed_name, component_code, garment_kind)
                qc_decision = qc_gate.evaluate(
                    expected_family=expected_family,
                    source_garment_path=local_garment_path,
                    source_model_path=local_model_path,
                    target_path=local_target_path,
                    context={
                        "dataset_id": dataset_id,
                        "dataset_prefix": dataset_prefix,
                        "pair_slug": pair_slug,
                        "seed_name": seed_name,
                        "garment_kind": garment_kind,
                        "outfit_kind": outfit_kind,
                        "component_code": component_code,
                        "model_code": model_code,
                        "quote_id": str(quote_id),
                        "studio_job_id": str(studio_job_id),
                    },
                )
                _write_json(
                    os.path.join(run_dir, f"{pair_slug}_r{out_rank}_qc.json"),
                    qc_decision.to_dict(),
                )

                if (not qc_decision.accepted) and qc_decision.status != "review":
                    artifact_blob = f"{dataset_prefix}/rejected/{garment_kind}/{pair_slug}_r{out_rank}.png"
                    artifact_az = _upload_file_to_azure(
                        local_path=local_target_path,
                        container=args.training_container,
                        blob_name=artifact_blob,
                        overwrite=True,
                    )

                    summary["counts"]["rejected"] += 1
                    summary["rejected"].append(
                        {
                            "pair_slug": pair_slug,
                            "seed_name": seed_name,
                            "garment_kind": garment_kind,
                            "outfit_kind": outfit_kind,
                            "component_code": component_code,
                            "expected_family": expected_family,
                            "model_code": model_code,
                            "quote_id": quote_id,
                            "studio_job_id": studio_job_id,
                            "rejected_az": artifact_az,
                            "original_output_url": output_url,
                            "qc": qc_decision.to_dict(),
                        }
                    )
                    print(f"[qc] rejected pair={pair_slug} expected={expected_family} reasons={qc_decision.reasons}")

                    _cp_add(cp, "rejected_pair_outputs", pair_output_key)
                    rejected_pair_outputs.add(pair_output_key)
                    _save_checkpoint(checkpoint_path, cp)
                    continue

                target_blob = f"{dataset_prefix}/targets/{garment_kind}/{pair_slug}_r{out_rank}.png"
                target_az = _upload_file_to_azure(
                    local_path=local_target_path,
                    container=args.training_container,
                    blob_name=target_blob,
                    overwrite=True,
                )

                split = _deterministic_split(f"{pair_key}:{out_rank}")
                example_id = str(uuid4())
                example_key = f"{pair_key}:{out_rank}"

                example_row = {
                    "id": example_id,
                    "dataset_id": dataset_id,
                    "split": split,
                    "task": "vton_non_saree_tryon",
                    "person_ref": {
                        "model_code": model_code,
                        "catalog_url": model_catalog_url,
                        "request_url": model_request_url,
                        "gender": model_gender,
                        "framing": model.get("framing"),
                        "pose": model.get("pose"),
                        "region": model.get("region"),
                        "region_tags": model.get("region_tags"),
                        "body_type": model.get("body_type"),
                        "style_tags": model.get("style_tags"),
                        "quality_score": model.get("quality_score"),
                    },
                    "garment_refs": {
                        "seed_name": seed_name,
                        "garment_kind": garment_kind,
                        "outfit_kind": outfit_kind,
                        "component_code": component_code,
                        "title": title,
                        "source_image_url": garment_source_url,
                        "dataset_az_url": garment_dataset_az,
                        "description_url": description_url,
                        "license_name": license_name,
                        "usage_terms": usage_terms,
                        "source_type": source_type,
                        "provider": garment.get("provider"),
                        "source_origin_url": garment.get("source_origin_url"),
                        "raw_image_url": garment.get("raw_image_url"),
                    },
                    "conditioning_refs": {
                        "quote_id": str(quote_id),
                        "studio_job_id": str(studio_job_id),
                        "confirm_variant": conf.get("_bootstrap_confirm_variant"),
                        "quote_request": quote_body,
                        "quote_response": q,
                        "confirm_response": conf,
                    },
                    "target_ref": {
                        "target_az_url": target_az,
                        "original_output_url": output_url,
                        "output_rank": out_rank,
                        "probe": output_probe,
                    },
                    "mask_refs": {},
                    "labels_json": {
                        "family": "non_saree",
                        "task": "vton_non_saree_tryon",
                        "garment_kind": garment_kind,
                        "outfit_kind": outfit_kind,
                        "component_code": component_code,
                        "model_code": model_code,
                        "seed_name": seed_name,
                        "license_name": license_name,
                        "usage_terms": usage_terms,
                        "expected_family": expected_family,
                        "predicted_family": qc_decision.predicted_family,
                        "source_type": source_type,
                    },
                    "quality_json": {
                        "status": qc_decision.status,
                        "accepted": qc_decision.accepted,
                        "qc": qc_decision.to_dict(),
                        "provider_probe": output_probe,
                    },
                    "consent_json": {
                        "usage_scope": "commercial_ok",
                        "source": "bootstrap_non_saree_vton_dataset",
                        "platform_model_catalog": True,
                    },
                    "dedup_hash": _stable_hash_hex(
                        {
                            "dataset_id": dataset_id,
                            "example_key": example_key,
                            "provider_kind": "platform_models",
                            "provider_job_id": str(studio_job_id),
                            "source_model_url": model_catalog_url,
                            "source_garment_url": garment_dataset_az,
                            "target_image_url": target_az,
                        }
                    ),
                    "sha256_json": {},
                    "created_at": _utc_now(),
                }

                await _insert_training_example(pool=pool, example_row=example_row)

                summary["counts"]["total"] += 1
                summary["counts"][split] += 1

                case_summary = {
                    "example_id": example_id,
                    "split": split,
                    "pair_slug": pair_slug,
                    "seed_name": seed_name,
                    "garment_kind": garment_kind,
                    "outfit_kind": outfit_kind,
                    "component_code": component_code,
                    "expected_family": expected_family,
                    "garment_title": title,
                    "garment_source_url": garment_source_url,
                    "garment_dataset_az": garment_dataset_az,
                    "model_code": model_code,
                    "model_gender": model_gender,
                    "model_catalog_url": model_catalog_url,
                    "quote_id": quote_id,
                    "studio_job_id": studio_job_id,
                    "confirm_variant": conf.get("_bootstrap_confirm_variant"),
                    "target_az": target_az,
                    "original_output_url": output_url,
                    "status": "ready",
                    "qc": qc_decision.to_dict(),
                }

                if qc_decision.status == "review":
                    summary["counts"]["review"] += 1
                    summary["review_cases"].append(case_summary)

                inserted_examples.append(case_summary)
                summary["cases"].append(case_summary)
                print(f"[example] inserted split={split} target={target_az}")
                _cp_add(cp, "completed_pair_outputs", pair_output_key)
                completed_pair_outputs.add(pair_output_key)
                _save_checkpoint(checkpoint_path, cp)
                pair_accepted = True
                break

            if not pair_accepted:
                _cp_add(cp, "skipped_pairs", pair_slug)
                skipped_pairs.add(pair_slug)
                summary["counts"]["skipped_pairs"] += 1
                _save_checkpoint(checkpoint_path, cp)

        if int(summary["counts"]["jobs_started"]) >= int(args.max_total_jobs):
            print("[stop] max total jobs reached")
            break
        if not _budget_guard(0):
            print("[stop] budget exhausted")
            summary["counts"]["budget_exhausted"] += 1
            break

    manifest = {
        "dataset_id": dataset_id,
        "dataset_name": args.dataset_name,
        "dataset_prefix": dataset_prefix,
        "created_at": _utc_now().isoformat(),
        "source": "bootstrap_non_saree_vton_dataset",
        "examples": inserted_examples,
        "rejected": summary["rejected"],
        "review_cases": summary.get("review_cases", []),
        "source_rejected": summary.get("source_rejected", []),
        "counts": summary["counts"],
        "budget": summary["budget"],
    }

    _write_json(os.path.join(run_dir, "summary.json"), summary)
    _write_json(os.path.join(run_dir, "manifest.json"), manifest)
    _save_checkpoint(checkpoint_path, cp)

    _upload_json_to_azure(
        obj=summary,
        container=args.training_container,
        blob_name=f"{dataset_prefix}/summary.json",
        overwrite=True,
    )
    _upload_json_to_azure(
        obj=manifest,
        container=args.training_container,
        blob_name=f"{dataset_prefix}/manifest.json",
        overwrite=True,
    )

    await _freeze_dataset(
        pool=pool,
        dataset_id=dataset_id,
        dataset_summary=manifest,
        summary=summary,
    )

    print("\nDONE")
    print("DATASET_ID=", dataset_id)
    print("DATASET_PREFIX=", dataset_prefix)
    print("COUNTS=", json.dumps(summary["counts"], indent=2))
    print("BUDGET=", json.dumps(summary["budget"], indent=2))


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
