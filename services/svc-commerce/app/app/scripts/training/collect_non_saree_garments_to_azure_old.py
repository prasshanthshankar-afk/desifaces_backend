#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
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
    {"seed_name": "hoodie", "garment_kind": "upper_body", "outfit_kind": "upper_body", "component_code": "hoodie", "queries": ["hoodie clothing isolated", "hooded sweatshirt garment isolated", "zip hoodie apparel"]},
    {"seed_name": "blazer", "garment_kind": "upper_body", "outfit_kind": "upper_body", "component_code": "blazer", "queries": ["blazer jacket isolated", "formal blazer apparel", "suit blazer clothing"]},
    {"seed_name": "jeans", "garment_kind": "lower_body", "outfit_kind": "lower_body", "component_code": "jeans", "queries": ["jeans isolated", "denim pants clothing isolated", "straight jeans apparel"]},
    {"seed_name": "dress", "garment_kind": "dresses", "outfit_kind": "dresses", "component_code": "dress", "queries": ["dress isolated clothing", "one piece dress garment", "dress apparel front view"]},
    {"seed_name": "kurta", "garment_kind": "kurta_pyjama", "outfit_kind": "kurta_pyjama", "component_code": "kurta", "queries": ["kurta clothing isolated", "mens kurta garment front", "indian kurta apparel"]},
    {"seed_name": "salwar_suit", "garment_kind": "salwar_suit", "outfit_kind": "salwar_suit", "component_code": "salwar_suit", "queries": ["salwar kameez clothing", "salwar suit apparel front", "shalwar kameez garment"]},
    {"seed_name": "lehenga", "garment_kind": "lehenga_set", "outfit_kind": "lehenga_set", "component_code": "lehenga", "queries": ["lehenga choli garment", "lehenga clothing front", "indian lehenga apparel"]},
    {"seed_name": "sherwani", "garment_kind": "sherwani", "outfit_kind": "sherwani", "component_code": "sherwani", "queries": ["sherwani apparel front", "sherwani garment", "wedding sherwani clothing"]},
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


def _wikimedia_search_files(query: str, *, limit: int = 12) -> List[Dict[str, Any]]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": 6,
        "gsrlimit": limit,
        "gsrsearch": query,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|size",
        "iiurlwidth": 2000,
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


def _pexels_search_photos(api_key: str, query: str, *, per_page: int = 15) -> List[Dict[str, Any]]:
    qs = urllib.parse.urlencode({"query": query, "per_page": per_page, "orientation": "portrait", "size": "large"})
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


def _raw_candidate_clip_score(local_path: str, family: str, model_id: str) -> Dict[str, Any]:
    if not _ClipScorer.available():
        return {"available": False}
    fam_text = _FAMILY_TEXT[family]
    prompts = [
        f"a standalone product photo of a {fam_text} on a plain background",
        f"a studio photo of a person wearing a {fam_text}",
        "a collage of multiple garments",
        "a folded garment on a table",
        "a product hanging on a hanger",
    ]
    try:
        with Image.open(local_path) as im:  # type: ignore
            img = im.convert("RGB")
            probs = _ClipScorer.score_prompts(model_id=model_id, image=img, prompts=prompts)
    except Exception as e:
        return {"available": False, "error": str(e)}
    packshot, worn, collage, folded, hanger = probs
    score = float(packshot - 0.5 * worn - 0.8 * collage - 0.6 * folded - 0.4 * hanger)
    return {
        "available": True,
        "score": score,
        "packshot_score": float(packshot),
        "worn_score": float(worn),
        "collage_score": float(collage),
        "folded_score": float(folded),
        "hanger_score": float(hanger),
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


def _search_candidates_for_seed(*, seed: Dict[str, Any], providers: Sequence[str], pexels_api_key: str, per_query: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for query in seed.get("queries") or []:
        for provider in providers:
            try:
                if provider == "pexels" and pexels_api_key:
                    items = _pexels_search_photos(pexels_api_key, query, per_page=per_query)
                elif provider == "wikimedia":
                    items = _wikimedia_search_files(query, limit=per_query)
                else:
                    items = []
            except Exception as e:
                print(f"[warn] search provider={provider} query={query!r} failed: {e}")
                items = []
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


def collect_and_stage(*, output_manifest_path: str, azure_container: str, azure_prefix: str, run_dir: str, families: Sequence[str], max_per_family: int, per_query: int, providers: Sequence[str], pexels_api_key: str, min_width: int, min_height: int, clip_model_id: str, upload_manifest: bool) -> Dict[str, Any]:
    _mkdirp(run_dir)
    downloads_dir = os.path.join(run_dir, "downloads")
    _mkdirp(downloads_dir)

    seeds = _family_filter(DEFAULT_SEEDS, families)
    selected: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for seed in seeds:
        family = seed["seed_name"]
        print(f"\n=== COLLECT {family} ===")
        raw_candidates = _search_candidates_for_seed(seed=seed, providers=providers, pexels_api_key=pexels_api_key, per_query=per_query)
        print(f"[search] family={family} raw_candidates={len(raw_candidates)}")
        staged_pool: List[Dict[str, Any]] = []
        family_seen_urls: Set[str] = set()

        for idx, cand in enumerate(raw_candidates):
            if len(staged_pool) >= max(max_per_family * 4, 10):
                break
            url = str(cand.get("image_url") or "")
            if not url or _url_key(url) in family_seen_urls:
                continue
            family_seen_urls.add(_url_key(url))
            ext = _guess_ext_from_url(url, ".jpg")
            local_path = os.path.join(downloads_dir, f"{family}_{idx:03d}{ext}")
            try:
                used_url = _download([url], local_path, timeout_s=90, retries=2)
                meta = _image_meta(local_path)
                if int(meta.get("width") or 0) < min_width or int(meta.get("height") or 0) < min_height:
                    rejected.append({**cand, "reason": "too_small", "meta": meta})
                    continue
                clip_info = _raw_candidate_clip_score(local_path, family=family, model_id=clip_model_id)
                if clip_info.get("available"):
                    if float(clip_info.get("packshot_score") or 0.0) < 0.18:
                        rejected.append({**cand, "reason": "low_packshot_score", "clip": clip_info, "meta": meta})
                        continue
                    if float(clip_info.get("worn_score") or 0.0) > float(clip_info.get("packshot_score") or 0.0):
                        rejected.append({**cand, "reason": "looks_worn_not_packshot", "clip": clip_info, "meta": meta})
                        continue
                staged_pool.append({**cand, "download_url": used_url, "local_path": local_path, "meta": meta, "clip": clip_info})
            except Exception as e:
                rejected.append({**cand, "reason": f"download_error:{type(e).__name__}", "error": str(e)})
                continue

        staged_pool.sort(key=lambda x: (_candidate_priority(str(x.get("provider") or "")), -(float(_as_dict_loose(x.get("clip")).get("score") or 0.0)), -(int(_as_dict_loose(x.get("meta")).get("width") or 0) * int(_as_dict_loose(x.get("meta")).get("height") or 0)), str(x.get("title") or "")))
        family_keep = staged_pool[:max_per_family]
        print(f"[keep] family={family} kept={len(family_keep)}")

        for rank, item in enumerate(family_keep, start=1):
            ext = _guess_ext_from_url(str(item.get("download_url") or item.get("image_url") or ""), ".jpg")
            blob_name = f"{azure_prefix}/raw_garments/{family}/{family}_{rank:03d}{ext}"
            az_uri = _upload_file_to_azure(local_path=str(item["local_path"]), container=azure_container, blob_name=blob_name, overwrite=True)
            sas_url = _az_uri_to_sas(az_uri, ttl_days=14)
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
                "source_type": f"internet_{item.get('provider') or 'unknown'}",
                "source_origin_url": item.get("description_url") or item.get("download_url") or item.get("image_url"),
                "raw_image_url": item.get("download_url") or item.get("image_url"),
                "artist": item.get("artist") or "",
                "credit": item.get("credit") or "",
                "query": item.get("query") or "",
                "meta": {"provider": item.get("provider"), "clip": item.get("clip"), "image_meta": item.get("meta"), "collected_at": _utc_now().isoformat()},
            })

    manifest = {"items": selected}
    _write_json(output_manifest_path, manifest)
    remote_manifest_uri = ""
    if upload_manifest:
        remote_manifest_uri = _upload_json_to_azure(obj=manifest, container=azure_container, blob_name=f"{azure_prefix}/manifests/non_saree_garments.json", overwrite=True)
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
    ap.add_argument("--per-query", type=int, default=10)
    ap.add_argument("--providers", default="pexels,wikimedia", help="ordered providers: pexels,wikimedia")
    ap.add_argument("--pexels-api-key", default=os.environ.get("PEXELS_API_KEY", ""))
    ap.add_argument("--azure-container", default=os.environ.get("COMMERCE_TRAINING_CONTAINER", "commerce-training"))
    ap.add_argument("--azure-prefix", default=f"training/non_saree_source_collect/{_utc_stamp()}")
    ap.add_argument("--output-manifest-path", default="")
    ap.add_argument("--run-dir", default="")
    ap.add_argument("--min-width", type=int, default=1000)
    ap.add_argument("--min-height", type=int, default=1000)
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
    )
    print(json.dumps({
        "selected_count": summary["selected_count"],
        "rejected_count": summary["rejected_count"],
        "manifest_local_path": summary["manifest_local_path"],
        "manifest_remote_uri": summary["manifest_remote_uri"],
    }, indent=2))


if __name__ == "__main__":
    main()
