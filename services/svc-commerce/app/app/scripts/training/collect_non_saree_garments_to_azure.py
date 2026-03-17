#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from azure.storage.blob import BlobServiceClient, BlobSasPermissions, generate_blob_sas

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore

try:
    import torch
    from transformers import CLIPModel, CLIPProcessor
except Exception:  # pragma: no cover
    torch = None  # type: ignore
    CLIPModel = None  # type: ignore
    CLIPProcessor = None  # type: ignore


DEFAULT_SEEDS: List[Dict[str, Any]] = [
    {
        "seed_name": "hoodie",
        "garment_kind": "upper_body",
        "outfit_kind": "upper_body",
        "component_code": "hoodie",
        "queries": [
            "hoodie isolated white background",
            "zip hoodie product front",
            "pullover hoodie catalog",
            "hooded sweatshirt flat lay",
            "hoodie apparel front view",
        ],
    },
    {
        "seed_name": "blazer",
        "garment_kind": "upper_body",
        "outfit_kind": "upper_body",
        "component_code": "blazer",
        "queries": [
            "blazer isolated white background",
            "formal blazer product front",
            "women blazer catalog",
            "men blazer flat lay",
            "navy blazer front view",
        ],
    },
    {
        "seed_name": "jeans",
        "garment_kind": "lower_body",
        "outfit_kind": "lower_body",
        "component_code": "jeans",
        "queries": [
            "jeans isolated white background",
            "denim pants product front",
            "blue jeans catalog front",
            "jeans flat lay full length",
            "straight fit jeans product",
        ],
    },
    {
        "seed_name": "dress",
        "garment_kind": "dresses",
        "outfit_kind": "dresses",
        "component_code": "dress",
        "queries": [
            "dress isolated white background",
            "one piece dress product front",
            "maxi dress catalog front",
            "dress flat lay full length",
            "dress apparel front view",
        ],
    },
    {
        "seed_name": "kurta",
        "garment_kind": "kurta_pyjama",
        "outfit_kind": "kurta_pyjama",
        "component_code": "kurta",
        "queries": [
            "mens kurta isolated",
            "indian kurta product front",
            "kurta catalog white background",
            "kurta flat lay",
            "cotton kurta front view",
        ],
    },
    {
        "seed_name": "salwar_suit",
        "garment_kind": "salwar_suit",
        "outfit_kind": "salwar_suit",
        "component_code": "salwar_suit",
        "queries": [
            "salwar suit isolated",
            "shalwar kameez product front",
            "salwar kameez catalog",
            "salwar suit white background",
            "embroidered salwar suit product",
        ],
    },
    {
        "seed_name": "lehenga",
        "garment_kind": "lehenga_set",
        "outfit_kind": "lehenga_set",
        "component_code": "lehenga",
        "queries": [
            "lehenga choli isolated",
            "bridal lehenga product front",
            "lehenga catalog white background",
            "lehenga flat lay",
            "festive lehenga product",
        ],
    },
    {
        "seed_name": "sherwani",
        "garment_kind": "sherwani",
        "outfit_kind": "sherwani",
        "component_code": "sherwani",
        "queries": [
            "sherwani isolated",
            "wedding sherwani catalog",
            "sherwani product front",
            "mens sherwani front view",
            "embroidered sherwani product",
        ],
    },
]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"

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

_FAMILY_EXTRA_NEGATIVE_PROMPTS: Dict[str, List[str]] = {
    "hoodie": ["a standalone product photo of a blazer jacket", "a standalone product photo of jeans denim pants"],
    "blazer": ["a standalone product photo of a sherwani coat", "a standalone product photo of a hoodie sweatshirt"],
    "jeans": ["a standalone product photo of dress gown", "a standalone product photo of kurta tunic"],
    "dress": ["a standalone product photo of lehenga choli", "a standalone product photo of salwar suit shalwar kameez"],
    "kurta": ["a standalone product photo of shirt", "a standalone product photo of sherwani coat"],
    "salwar_suit": ["a standalone product photo of lehenga choli", "a standalone product photo of dress gown"],
    "lehenga": ["a standalone product photo of dress gown", "a standalone product photo of salwar suit shalwar kameez"],
    "sherwani": ["a standalone product photo of blazer jacket", "a standalone product photo of kurta tunic"],
}

# Softer first-pass thresholds. We want raw-source recall here, not final perfection.
_FAMILY_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "hoodie": {"min_product_like": 0.35, "min_score": 0.10, "max_person_like": 0.18, "max_editorial_like": 0.10},
    "blazer": {"min_product_like": 0.30, "min_score": 0.08, "max_person_like": 0.18, "max_editorial_like": 0.10},
    "jeans": {"min_product_like": 0.28, "min_score": 0.05, "max_person_like": 0.20, "max_editorial_like": 0.10},
    "dress": {"min_product_like": 0.30, "min_score": 0.08, "max_person_like": 0.14, "max_editorial_like": 0.08},
    "kurta": {"min_product_like": 0.35, "min_score": 0.10, "max_person_like": 0.12, "max_editorial_like": 0.06},
    "salwar_suit": {"min_product_like": 0.35, "min_score": 0.10, "max_person_like": 0.12, "max_editorial_like": 0.06},
    "lehenga": {"min_product_like": 0.35, "min_score": 0.10, "max_person_like": 0.12, "max_editorial_like": 0.06},
    "sherwani": {"min_product_like": 0.35, "min_score": 0.10, "max_person_like": 0.12, "max_editorial_like": 0.06},
}


_TEXT_HARD_REJECT_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"\b(man|men|woman|women|girl|boy|person|people|couple|bride|groom|model|portrait)\b", re.I),
    re.compile(r"\b(smiling|posing|standing|sitting|holding|wearing)\b", re.I),
    re.compile(r"\b(wedding|bridal|groom|bride|ceremony|palace|outdoors|indoors|lahore|ludhiana)\b", re.I),
    re.compile(r"\b(bouquet|flag|flags|laptop|bag|mask|notes)\b", re.I),
    re.compile(r"\b(feather|feathers|texture|fabric|swatch|close[- ]?up|detail view)\b", re.I),
]

_TEXT_ALLOW_HINTS = (
    "isolated",
    "white background",
    "catalog",
    "product",
    "front view",
    "flat lay",
    "mannequin",
)

def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _utc_stamp() -> str:
    return _utc_now().strftime("%Y%m%d_%H%M%S")


def _mkdirp(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _slug(s: str, max_len: int = 120) -> str:
    s2 = re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "").strip()).strip("._-")
    return s2[:max_len] or "item"


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


def _write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False, default=str)


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


def _url_key(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(str(url).strip())
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, "", ""))
    except Exception:
        return str(url).strip()


def _text_reject_reason(*, family: str, title: str, query: str) -> Optional[str]:
    text = f"{title} {query}".strip().lower()
    if not text:
        return None

    for pat in _TEXT_HARD_REJECT_PATTERNS:
        if pat.search(text):
            return "text_editorial_or_person"

    # For complex Indian sets, be extra strict for now.
    if family in {"kurta", "salwar_suit", "lehenga", "sherwani"}:
        if not any(h in text for h in _TEXT_ALLOW_HINTS):
            return "needs_product_style_text"

    return None


def _download_one(url: str, dst: str, timeout_s: int = 90) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
    if not data or len(data) < 1024:
        raise RuntimeError(f"download too small ({len(data) if data else 0} bytes) from {url}")
    with open(dst, "wb") as f:
        f.write(data)


def _download(urls: Sequence[str], dst: str, timeout_s: int = 90, retries: int = 3) -> str:
    if not urls:
        raise RuntimeError("no download URLs provided")
    last_err: Optional[Exception] = None
    for url in urls:
        for i in range(retries):
            try:
                _download_one(url, dst, timeout_s=timeout_s)
                return url
            except Exception as e:
                last_err = e
                time.sleep(1.0 * (i + 1))
    raise RuntimeError(f"download failed after retries; last_err={last_err}")


def _parse_conn_str(conn_str: str) -> Dict[str, str]:
    parts: Dict[str, str] = {}
    for seg in conn_str.split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k.strip()] = v.strip()
    return parts


def _get_blob_service_client() -> BlobServiceClient:
    conn = (
        os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        or os.environ.get("AZURE_BLOB_CONNECTION_STRING")
        or os.environ.get("AZURE_STORAGE_CONN_STR")
    )
    if conn:
        return BlobServiceClient.from_connection_string(conn)
    account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL") or os.environ.get("AZURE_BLOB_ACCOUNT_URL")
    credential = (
        os.environ.get("AZURE_STORAGE_KEY")
        or os.environ.get("AZURE_STORAGE_SAS_TOKEN")
        or os.environ.get("AZURE_BLOB_KEY")
        or os.environ.get("AZURE_BLOB_SAS_TOKEN")
    )
    if account_url and credential:
        return BlobServiceClient(account_url=account_url, credential=credential)
    raise RuntimeError("Azure credentials not found. Set AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL + key/SAS.")


def _make_read_sas_url(*, account_name: str, account_key: str, container: str, blob_name: str, ttl_days: int = 14) -> str:
    sas = generate_blob_sas(
        account_name=account_name,
        container_name=container,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=_utc_now() + dt.timedelta(days=ttl_days),
    )
    return f"https://{account_name}.blob.core.windows.net/{container}/{blob_name}?{sas}"


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


def _az_uri_to_sas(az_uri: str, ttl_days: int = 14) -> str:
    if not az_uri.startswith("az://"):
        return az_uri
    rest = az_uri[len("az://"):]
    container, blob_name = rest.split("/", 1)
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not conn_str:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING required to convert az:// to SAS")
    conn = _parse_conn_str(conn_str)
    account_name = conn.get("AccountName")
    account_key = conn.get("AccountKey")
    if not account_name or not account_key:
        raise RuntimeError("Could not parse AccountName/AccountKey from AZURE_STORAGE_CONNECTION_STRING")
    return _make_read_sas_url(account_name=account_name, account_key=account_key, container=container, blob_name=blob_name, ttl_days=ttl_days)


def _wikimedia_api_json(params: Dict[str, Any]) -> Dict[str, Any]:
    base = "https://commons.wikimedia.org/w/api.php"
    qs = urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(f"{base}?{qs}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _license_ok(license_name: str, usage_terms: str) -> bool:
    s = (license_name or "").lower()
    t = (usage_terms or "").lower()
    blob = f"{s} {t}"
    return any(x in blob for x in ["cc", "creative commons", "public domain", "gfdl", "pd", "cc-by", "cc by", "cc-by-sa"])


def _wikimedia_search_files(query: str, *, limit: int = 18) -> List[Dict[str, Any]]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": 6,
        "gsrlimit": limit,
        "gsrsearch": query,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|size",
        "iiurlwidth": 2200,
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
        width = int(imageinfo.get("width") or 0)
        height = int(imageinfo.get("height") or 0)
        if not source_url:
            continue
        if not _license_ok(license_name, usage_terms):
            continue
        out.append({
            "title": title,
            "image_url": source_url,
            "description_url": description_url,
            "license_name": license_name,
            "usage_terms": usage_terms,
            "artist": artist,
            "credit": credit,
            "query": query,
            "width": width,
            "height": height,
            "provider": "wikimedia",
        })
    return out


def _pexels_api_json(api_key: str, url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Authorization": api_key})
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _pexels_search_photos(api_key: str, query: str, *, per_page: int = 18, page: int = 1) -> List[Dict[str, Any]]:
    qs = urllib.parse.urlencode({"query": query, "per_page": per_page, "page": page, "size": "large"})
    url = f"https://api.pexels.com/v1/search?{qs}"
    data = _pexels_api_json(api_key, url)
    out: List[Dict[str, Any]] = []
    for photo in data.get("photos") or []:
        src = _as_dict_loose(photo.get("src"))
        image_url = str(src.get("large2x") or src.get("large") or src.get("original") or "").strip()
        if not image_url:
            continue
        out.append({
            "title": str(photo.get("alt") or query).strip() or query,
            "image_url": image_url,
            "description_url": str(photo.get("url") or "").strip(),
            "license_name": "Pexels License",
            "usage_terms": "pexels_commercial_ok_verify_brand_person_rights",
            "artist": str(photo.get("photographer") or ""),
            "credit": str(photo.get("photographer") or ""),
            "query": query,
            "width": int(photo.get("width") or 0),
            "height": int(photo.get("height") or 0),
            "provider": "pexels",
        })
    return out


class _ClipScorer:
    _model: Any = None
    _processor: Any = None
    _device: str = "cpu"
    _model_id: Optional[str] = None

    @classmethod
    def available(cls) -> bool:
        return CLIPModel is not None and CLIPProcessor is not None and torch is not None and Image is not None

    @classmethod
    def _ensure_loaded(cls, model_id: str) -> None:
        if not cls.available():
            raise RuntimeError("transformers/torch/PIL not available")
        if cls._model is not None and cls._processor is not None and cls._model_id == model_id:
            return
        cache_dir = os.environ.get("HF_HUB_CACHE") or os.environ.get("HF_HOME") or "/tmp/huggingface"
        os.makedirs(cache_dir, exist_ok=True)
        try:
            cls._processor = CLIPProcessor.from_pretrained(model_id, cache_dir=cache_dir, local_files_only=True)
            cls._model = CLIPModel.from_pretrained(model_id, cache_dir=cache_dir, local_files_only=True, use_safetensors=True, low_cpu_mem_usage=True)
        except Exception:
            cls._processor = CLIPProcessor.from_pretrained(model_id, cache_dir=cache_dir)
            cls._model = CLIPModel.from_pretrained(model_id, cache_dir=cache_dir, use_safetensors=True, low_cpu_mem_usage=True)
        cls._model.eval()
        cls._device = "cuda" if torch.cuda.is_available() else "cpu"
        cls._model.to(cls._device)
        cls._model_id = model_id

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


def _family_prompts(family: str) -> List[str]:
    fam_text = _FAMILY_TEXT[family]
    return [
        f"a standalone product photo of a single {fam_text} on a plain background",
        f"a clean catalog product image of a single {fam_text}",
        f"a flat lay product photo of a single {fam_text}",
        f"a mannequin wearing a single {fam_text}",
        f"a studio photo of one person wearing a {fam_text}",
        "an editorial lifestyle fashion photo with a person",
        "a couple or wedding scene with people wearing clothing",
        "a collage of multiple garments",
        "a close-up fabric texture or feathers",
        "a clothing product hanging on a hanger",
    ]


def _raw_candidate_clip_score(local_path: str, family: str, model_id: str) -> Dict[str, Any]:
    if not _ClipScorer.available():
        return {"available": False}

    prompts = _family_prompts(family)
    try:
        with Image.open(local_path) as im:  # type: ignore
            img = im.convert("RGB")
            probs = _ClipScorer.score_prompts(model_id=model_id, image=img, prompts=prompts)
    except Exception as e:
        return {"available": False, "error": str(e)}

    product_plain = float(probs[0])
    catalog = float(probs[1])
    flat_lay = float(probs[2])
    mannequin = float(probs[3])
    person_wearing = float(probs[4])
    editorial = float(probs[5])
    couple_scene = float(probs[6])
    collage = float(probs[7])
    texture = float(probs[8])
    hanger = float(probs[9])

    product_like = max(product_plain, catalog, flat_lay, mannequin)
    non_product_like = max(person_wearing, editorial, couple_scene, collage, texture, hanger)

    score = (
        1.25 * product_like
        - 0.90 * person_wearing
        - 0.90 * editorial
        - 1.10 * couple_scene
        - 1.10 * collage
        - 1.10 * texture
        - 0.50 * hanger
    )

    return {
        "available": True,
        "score": score,
        "product_like_score": product_like,
        "person_wearing_score": person_wearing,
        "editorial_score": editorial,
        "couple_scene_score": couple_scene,
        "collage_score": collage,
        "texture_score": texture,
        "hanger_score": hanger,
        "catalog_score": catalog,
        "flat_lay_score": flat_lay,
        "mannequin_score": mannequin,
    }


def _image_meta(local_path: str) -> Dict[str, Any]:
    if Image is None:
        return {"width": 0, "height": 0}
    with Image.open(local_path) as im:  # type: ignore
        w, h = im.size
    return {"width": int(w), "height": int(h), "aspect_ratio": float(w) / float(h or 1)}


def _candidate_priority(provider: str) -> int:
    order = {"pexels": 0, "wikimedia": 1}
    return order.get(provider, 9)


def _family_filter(seeds: Sequence[Dict[str, Any]], families: Sequence[str]) -> List[Dict[str, Any]]:
    wanted = {str(x).strip().lower() for x in families if str(x).strip()}
    if not wanted:
        return list(seeds)
    return [s for s in seeds if str(s["seed_name"]).strip().lower() in wanted]


def _search_candidates_for_seed(*, seed: Dict[str, Any], providers: Sequence[str], pexels_api_key: str, per_query: int, pexels_pages: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    family = str(seed["seed_name"])
    for query in seed.get("queries") or []:
        for provider in providers:
            items: List[Dict[str, Any]] = []
            try:
                if provider == "pexels" and pexels_api_key:
                    for page in range(1, max(1, pexels_pages) + 1):
                        items.extend(_pexels_search_photos(pexels_api_key, query, per_page=per_query, page=page))
                elif provider == "wikimedia":
                    items = _wikimedia_search_files(query, limit=max(per_query, 18))
            except Exception as e:
                print(f"[warn] search provider={provider} family={family} query={query!r} failed: {e}")
                items = []
            print(f"[search-provider] family={family} provider={provider} query={query!r} hits={len(items)}")
            for item in items:
                url = str(item.get("image_url") or "")
                if not url:
                    continue
                k = _url_key(url)
                if k in seen:
                    continue
                seen.add(k)
                out.append({
                    "seed_name": seed["seed_name"],
                    "garment_kind": seed["garment_kind"],
                    "outfit_kind": seed["outfit_kind"],
                    "component_code": seed["component_code"],
                    **item,
                })
    return out


def _score_candidate_for_keep(cand: Dict[str, Any]) -> float:
    provider = str(cand.get("provider") or "")
    clip = _as_dict_loose(cand.get("clip"))
    meta = _as_dict_loose(cand.get("meta"))
    area = int(meta.get("width") or 0) * int(meta.get("height") or 0)
    score = float(clip.get("score") or 0.0)
    fam_match = float(clip.get("family_match_score") or 0.0)
    catalog = float(clip.get("catalog_score") or 0.0)
    worn = float(clip.get("worn_score") or 0.0)
    provider_bonus = 0.10 if provider == "pexels" else 0.0
    res_bonus = min(0.25, area / float(2200 * 2200))
    return score + 0.55 * fam_match + 0.25 * catalog - 0.15 * worn + provider_bonus + res_bonus


def collect_and_stage(
    *,
    output_manifest_path: str,
    azure_container: str,
    azure_prefix: str,
    run_dir: str,
    families: Sequence[str],
    max_per_family: int,
    per_query: int,
    providers: Sequence[str],
    pexels_api_key: str,
    min_width: int,
    min_height: int,
    clip_model_id: str,
    upload_manifest: bool,
    pexels_pages: int,
    raw_pool_multiplier: int,
) -> Dict[str, Any]:
    _mkdirp(run_dir)
    downloads_dir = os.path.join(run_dir, "downloads")
    _mkdirp(downloads_dir)

    print(f"[provider] pexels enabled={'yes' if ('pexels' in providers and bool(pexels_api_key)) else 'no'} key_present={'yes' if bool(pexels_api_key) else 'no'}")
    print(f"[provider] wikimedia enabled={'yes' if 'wikimedia' in providers else 'no'}")

    seeds = _family_filter(DEFAULT_SEEDS, families)
    selected: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    rejected_reason_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for seed in seeds:
        family = seed["seed_name"]
        print(f"\n=== COLLECT {family} ===")
        raw_candidates = _search_candidates_for_seed(
            seed=seed,
            providers=providers,
            pexels_api_key=pexels_api_key,
            per_query=per_query,
            pexels_pages=pexels_pages,
        )
        print(f"[search] family={family} raw_candidates={len(raw_candidates)}")
        staged_pool: List[Dict[str, Any]] = []
        family_seen_urls: Set[str] = set()

        max_stage = max(max_per_family * max(4, raw_pool_multiplier), 16)

        for idx, cand in enumerate(raw_candidates):
            if len(staged_pool) >= max_stage:
                break
            url = str(cand.get("image_url") or "")
            if not url or _url_key(url) in family_seen_urls:
                continue
            family_seen_urls.add(_url_key(url))
            ext = _guess_ext_from_url(url, ".jpg")

            text_reason = _text_reject_reason(
                family=family,
                title=str(cand.get("title") or ""),
                query=str(cand.get("query") or ""),
            )
            if text_reason:
                rejected.append({**cand, "reason": text_reason})
                rejected_reason_counts[family][text_reason] += 1
                continue

            local_path = os.path.join(downloads_dir, f"{family}_{idx:03d}{ext}")

            try:
                used_url = _download([url], local_path, timeout_s=90, retries=2)
                meta = _image_meta(local_path)
                if int(meta.get("width") or 0) < min_width or int(meta.get("height") or 0) < min_height:
                    reason = "too_small"
                    rejected.append({**cand, "reason": reason, "meta": meta})
                    rejected_reason_counts[family][reason] += 1
                    continue

                clip_info = _raw_candidate_clip_score(local_path, family=family, model_id=clip_model_id)
                if clip_info.get("available"):
                    th = _FAMILY_THRESHOLDS.get(
                        family,
                        {"min_product_like": 0.30, "min_score": 0.08, "max_person_like": 0.18, "max_editorial_like": 0.10},
                    )

                    product_like = float(clip_info.get("product_like_score") or 0.0)
                    score = float(clip_info.get("score") or 0.0)
                    person_like = float(clip_info.get("person_wearing_score") or 0.0)
                    editorial = float(clip_info.get("editorial_score") or 0.0)
                    couple_scene = float(clip_info.get("couple_scene_score") or 0.0)
                    collage = float(clip_info.get("collage_score") or 0.0)
                    texture = float(clip_info.get("texture_score") or 0.0)
                    hanger = float(clip_info.get("hanger_score") or 0.0)

                    if couple_scene > 0.08:
                        reason = "couple_or_wedding_like"
                        rejected.append({**cand, "reason": reason, "clip": clip_info, "meta": meta})
                        rejected_reason_counts[family][reason] += 1
                        continue
                    if collage > 0.18:
                        reason = "collage_like"
                        rejected.append({**cand, "reason": reason, "clip": clip_info, "meta": meta})
                        rejected_reason_counts[family][reason] += 1
                        continue
                    if texture > 0.15:
                        reason = "texture_like"
                        rejected.append({**cand, "reason": reason, "clip": clip_info, "meta": meta})
                        rejected_reason_counts[family][reason] += 1
                        continue
                    if hanger > 0.45:
                        reason = "hanger_dominant"
                        rejected.append({**cand, "reason": reason, "clip": clip_info, "meta": meta})
                        rejected_reason_counts[family][reason] += 1
                        continue
                    if person_like > float(th["max_person_like"]):
                        reason = "person_wearing_like"
                        rejected.append({**cand, "reason": reason, "clip": clip_info, "meta": meta})
                        rejected_reason_counts[family][reason] += 1
                        continue
                    if editorial > float(th["max_editorial_like"]):
                        reason = "too_editorial"
                        rejected.append({**cand, "reason": reason, "clip": clip_info, "meta": meta})
                        rejected_reason_counts[family][reason] += 1
                        continue
                    if product_like < float(th["min_product_like"]):
                        reason = "product_like_too_low"
                        rejected.append({**cand, "reason": reason, "clip": clip_info, "meta": meta})
                        rejected_reason_counts[family][reason] += 1
                        continue
                    if score < float(th["min_score"]):
                        reason = "raw_score_too_low"
                        rejected.append({**cand, "reason": reason, "clip": clip_info, "meta": meta})
                        rejected_reason_counts[family][reason] += 1
                        continue

                staged_pool.append({
                    **cand,
                    "download_url": used_url,
                    "local_path": local_path,
                    "meta": meta,
                    "clip": clip_info,
                })
            except Exception as e:
                reason = f"download_error:{type(e).__name__}"
                rejected.append({**cand, "reason": reason, "error": str(e)})
                rejected_reason_counts[family][reason] += 1
                continue

        staged_pool.sort(
            key=lambda x: (
                _candidate_priority(str(x.get("provider") or "")),
                -_score_candidate_for_keep(x),
                -(int(_as_dict_loose(x.get("meta")).get("width") or 0) * int(_as_dict_loose(x.get("meta")).get("height") or 0)),
                str(x.get("title") or ""),
            )
        )

        family_keep = staged_pool[:max_per_family]
        print(f"[keep] family={family} kept={len(family_keep)}")
        if rejected_reason_counts.get(family):
            print(f"[reject-summary] family={family} reasons={dict(rejected_reason_counts[family])}")

        for rank, item in enumerate(family_keep, start=1):
            ext = _guess_ext_from_url(str(item.get("download_url") or item.get("image_url") or ""), ".jpg")
            blob_name = f"{azure_prefix}/raw_garments/{family}/{family}_{rank:03d}{ext}"
            az_uri = _upload_file_to_azure(local_path=str(item["local_path"]), container=azure_container, blob_name=blob_name, overwrite=True)
            sas_url = _az_uri_to_sas(az_uri, ttl_days=14)
            provider = str(item.get("provider") or "unknown")
            selected.append({
                "seed_name": family,
                "garment_kind": item["garment_kind"],
                "outfit_kind": item["outfit_kind"],
                "component_code": item["component_code"],
                "image_url": sas_url,
                "azure_uri": az_uri,
                "title": item.get("title") or f"{family}_{rank:03d}",
                "brand": item.get("provider") or "",
                "vendor_sku": f"AUTO-{family.upper()}-{rank:03d}",
                "license_name": item.get("license_name") or "",
                "usage_terms": item.get("usage_terms") or "",
                "provider": provider,
                "source_type": f"internet_{provider}",
                "source_origin_url": item.get("description_url") or item.get("download_url") or item.get("image_url"),
                "raw_image_url": item.get("download_url") or item.get("image_url"),
                "artist": item.get("artist") or "",
                "credit": item.get("credit") or "",
                "query": item.get("query") or "",
                "meta": {
                    "provider": provider,
                    "clip": item.get("clip"),
                    "image_meta": item.get("meta"),
                    "rank_score": _score_candidate_for_keep(item),
                    "collected_at": _utc_now().isoformat(),
                },
            })

    manifest = {"items": selected}
    _write_json(output_manifest_path, manifest)
    remote_manifest_uri = ""
    if upload_manifest:
        remote_manifest_uri = _upload_json_to_azure(
            obj=manifest,
            container=azure_container,
            blob_name=f"{azure_prefix}/manifests/non_saree_garments.json",
            overwrite=True,
        )

    rejected_summary = {fam: dict(counts) for fam, counts in rejected_reason_counts.items()}
    summary = {
        "created_at": _utc_now().isoformat(),
        "azure_container": azure_container,
        "azure_prefix": azure_prefix,
        "manifest_local_path": output_manifest_path,
        "manifest_remote_uri": remote_manifest_uri,
        "selected_count": len(selected),
        "rejected_count": len(rejected),
        "selected": selected,
        "rejected": rejected,
        "rejected_by_family": rejected_summary,
        "providers": list(providers),
        "families": [s["seed_name"] for s in seeds],
        "run_dir": run_dir,
    }
    summary_path = os.path.join(run_dir, "collect_summary.json")
    _write_json(summary_path, summary)
    print(f"\nDONE selected={len(selected)} rejected={len(rejected)}")
    print(f"LOCAL_MANIFEST={output_manifest_path}")
    if remote_manifest_uri:
        print(f"REMOTE_MANIFEST={remote_manifest_uri}")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--families", default="", help="comma-separated families; default all")
    ap.add_argument("--max-per-family", type=int, default=3)
    ap.add_argument("--per-query", type=int, default=12)
    ap.add_argument("--providers", default="pexels,wikimedia", help="ordered providers: pexels,wikimedia")
    ap.add_argument("--pexels-api-key", default=os.environ.get("PEXELS_API_KEY", ""))
    ap.add_argument("--pexels-pages", type=int, default=2, help="number of Pexels pages per query")
    ap.add_argument("--raw-pool-multiplier", type=int, default=8, help="kept raw pool target = max_per_family * multiplier")
    ap.add_argument("--azure-container", default=os.environ.get("COMMERCE_TRAINING_CONTAINER", "commerce-training"))
    ap.add_argument("--azure-prefix", default=f"training/non_saree_source_collect/{_utc_stamp()}")
    ap.add_argument("--output-manifest-path", default="")
    ap.add_argument("--run-dir", default="")
    ap.add_argument("--min-width", type=int, default=800)
    ap.add_argument("--min-height", type=int, default=800)
    ap.add_argument("--clip-model-id", default=os.environ.get("DF_NONSAREE_QC_CLIP_MODEL_ID", "openai/clip-vit-base-patch32"))
    ap.add_argument("--no-upload-manifest", action="store_true")
    args = ap.parse_args()

    families = [x.strip().lower() for x in args.families.split(",") if x.strip()]
    providers = [x.strip().lower() for x in args.providers.split(",") if x.strip()]
    run_dir = args.run_dir.strip() or f"/tmp/df_collect_non_saree_sources_{_utc_stamp()}"
    _mkdirp(run_dir)
    output_manifest_path = args.output_manifest_path.strip() or os.path.join(run_dir, "non_saree_garments.json")

    summary = collect_and_stage(
        output_manifest_path=output_manifest_path,
        azure_container=args.azure_container,
        azure_prefix=args.azure_prefix,
        run_dir=run_dir,
        families=families,
        max_per_family=max(1, args.max_per_family),
        per_query=max(1, args.per_query),
        providers=providers,
        pexels_api_key=args.pexels_api_key.strip(),
        min_width=max(256, args.min_width),
        min_height=max(256, args.min_height),
        clip_model_id=args.clip_model_id.strip(),
        upload_manifest=not args.no_upload_manifest,
        pexels_pages=max(1, args.pexels_pages),
        raw_pool_multiplier=max(4, args.raw_pool_multiplier),
    )
    print(json.dumps({
        "selected_count": summary["selected_count"],
        "rejected_count": summary["rejected_count"],
        "manifest_local_path": summary["manifest_local_path"],
        "manifest_remote_uri": summary["manifest_remote_uri"],
        "rejected_by_family": summary["rejected_by_family"],
    }, indent=2))


if __name__ == "__main__":
    main()
