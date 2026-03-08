#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import dataclasses
import io
import json
import math
import mimetypes
import os
import re
import sys
import time
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from PIL import Image, ImageFilter, ImageStat

try:
    from azure.storage.blob import BlobServiceClient, ContentSettings
except Exception as e:  # pragma: no cover
    raise SystemExit(
        "azure-storage-blob is required for this script.\n"
        "Install it in the svc-commerce environment before running.\n"
        f"Import error: {e}"
    )


# -------------------------------------------------------------------
# small helpers
# -------------------------------------------------------------------

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
JSON_EXTS = {".json"}


def _utc_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def _mkdirp(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _read_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_file(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, sort_keys=True, default=str)


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default


def _normalize_text(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, (dict, list, tuple)):
        return _json_dumps(x).lower()
    return str(x).strip().lower()


def _guess_content_type(name: str) -> str:
    ct, _ = mimetypes.guess_type(name)
    return ct or "application/octet-stream"


def _parse_az_uri(uri: str) -> Tuple[str, str]:
    """
    az://container/blob/path
    """
    if not uri.startswith("az://"):
        raise ValueError(f"Not an az:// URI: {uri}")
    rest = uri[len("az://") :]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid az:// URI: {uri}")
    return parts[0], parts[1]


def _make_az_uri(container: str, blob_name: str) -> str:
    return f"az://{container}/{blob_name}"


def _download_http_bytes(url: str, timeout_s: int = 120) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _sha_seed(s: str) -> int:
    # stable enough for deterministic ordering without importing hashlib here repeatedly
    import hashlib

    return int(hashlib.sha256(s.encode("utf-8")).hexdigest(), 16)


# -------------------------------------------------------------------
# Azure helpers
# -------------------------------------------------------------------

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
        "Azure credentials not found. Set one of:\n"
        "- AZURE_STORAGE_CONNECTION_STRING\n"
        "- AZURE_BLOB_CONNECTION_STRING\n"
        "- AZURE_STORAGE_ACCOUNT_URL + AZURE_STORAGE_KEY/SAS"
    )


def _download_az_bytes(bsc: BlobServiceClient, az_uri: str) -> bytes:
    container, blob_name = _parse_az_uri(az_uri)
    bc = bsc.get_blob_client(container=container, blob=blob_name)
    return bc.download_blob().readall()


def _download_bytes(bsc: BlobServiceClient, uri: str) -> bytes:
    if uri.startswith("az://"):
        return _download_az_bytes(bsc, uri)
    if uri.startswith("http://") or uri.startswith("https://"):
        return _download_http_bytes(uri)
    raise ValueError(f"Unsupported URI: {uri}")


def _upload_bytes(
    bsc: BlobServiceClient,
    *,
    container: str,
    blob_name: str,
    data: bytes,
    content_type: str,
    overwrite: bool = True,
) -> str:
    bc = bsc.get_blob_client(container=container, blob=blob_name)
    bc.upload_blob(
        data,
        overwrite=overwrite,
        content_settings=ContentSettings(content_type=content_type),
    )
    return _make_az_uri(container, blob_name)


def _upload_text_json(
    bsc: BlobServiceClient,
    *,
    container: str,
    blob_name: str,
    obj: Any,
    overwrite: bool = True,
) -> str:
    data = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return _upload_bytes(
        bsc,
        container=container,
        blob_name=blob_name,
        data=data,
        content_type="application/json",
        overwrite=overwrite,
    )


def _blob_exists(bsc: BlobServiceClient, *, container: str, blob_name: str) -> bool:
    try:
        return bsc.get_blob_client(container=container, blob=blob_name).exists()
    except Exception:
        return False


def _download_text_if_exists(bsc: BlobServiceClient, *, container: str, blob_name: str) -> Optional[str]:
    if not _blob_exists(bsc, container=container, blob_name=blob_name):
        return None
    try:
        data = bsc.get_blob_client(container=container, blob=blob_name).download_blob().readall()
        return data.decode("utf-8")
    except Exception:
        return None


# -------------------------------------------------------------------
# data models
# -------------------------------------------------------------------

@dataclasses.dataclass
class Candidate:
    source_container: str
    source_blob_name: str
    source_az_uri: str

    width: int
    height: int
    content_type: str
    size_bytes: int

    gender: str
    framing: str
    pose: str
    region: str
    age_band: str
    body_type: str
    skin_tone: str

    is_active: bool
    style_tags: List[str]
    allowed_garment_kinds: List[str]
    preferred_garment_kinds: List[str]

    source_meta: Dict[str, Any]
    source_text: str
    qc: Dict[str, Any]
    score: float
    confidence: float
    bucket: str
    review_reasons: List[str]
    reject_reasons: List[str]
    alt_asset_uris: List[str]


# -------------------------------------------------------------------
# inference / scoring
# -------------------------------------------------------------------

GENDER_HINTS = {
    "female": [
        "female",
        "woman",
        "women",
        "girl",
        "lady",
        "ladies",
        "bride",
    ],
    "male": [
        "male",
        "man",
        "men",
        "boy",
        "gent",
        "gents",
        "groom",
    ],
}

FRAMING_HINTS = {
    "full_body": [
        "full body",
        "full-body",
        "fullbody",
        "head to toe",
        "head-to-toe",
        "feet visible",
    ],
    "three_quarter": [
        "three quarter",
        "three-quarter",
        "3/4",
        "3q",
        "waist up",
        "knee up",
        "mid length body",
    ],
}

POSE_HINTS = {
    "front": [
        "front",
        "front-facing",
        "forward facing",
        "straight-on",
        "center pose",
    ],
    "side": [
        "side",
        "profile",
    ],
}


def _bucket_key(gender: str, framing: str, pose: str) -> str:
    return f"{gender}/{framing}/{pose}"


def _framing_code(framing: str) -> str:
    return {
        "full_body": "fb",
        "three_quarter": "3q",
    }.get(framing, "uk")


def _gender_code(gender: str) -> str:
    return {
        "female": "f",
        "male": "m",
    }.get(gender, "u")


def _pose_code(pose: str) -> str:
    return {
        "front": "fr",
        "side": "sd",
    }.get(pose, "uk")


def _score_soft_portrait_ratio(width: int, height: int) -> float:
    if width <= 0 or height <= 0:
        return 0.0
    ratio = width / float(height)
    # portrait-ish around 0.6 to 0.8 is generally good
    center = 0.67
    dist = abs(ratio - center)
    return max(0.0, 1.0 - min(1.0, dist / 0.5))


def _score_resolution(width: int, height: int) -> float:
    shorter = min(width, height)
    if shorter >= 1024:
        return 1.0
    if shorter >= 896:
        return 0.9
    if shorter >= 768:
        return 0.75
    if shorter >= 640:
        return 0.5
    return 0.15


def _probe_image_quality(data: bytes) -> Tuple[Dict[str, Any], Optional[Image.Image]]:
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        width, height = img.size

        gray = img.convert("L")
        stat = ImageStat.Stat(gray)
        gray_std = float(stat.stddev[0]) if stat.stddev else 0.0

        edges = gray.filter(ImageFilter.FIND_EDGES)
        edge_stat = ImageStat.Stat(edges)
        edge_mean = float(edge_stat.mean[0]) if edge_stat.mean else 0.0
        edge_std = float(edge_stat.stddev[0]) if edge_stat.stddev else 0.0

        portrait_score = _score_soft_portrait_ratio(width, height)
        resolution_score = _score_resolution(width, height)

        qc = {
            "width": width,
            "height": height,
            "portrait_score": round(portrait_score, 4),
            "resolution_score": round(resolution_score, 4),
            "gray_std": round(gray_std, 4),
            "edge_mean": round(edge_mean, 4),
            "edge_std": round(edge_std, 4),
            "face_ok": None,  # visual face detection intentionally not guessed here
            "hands_ok": None,
            "clean_bg": None,
            "full_body_visible": None,
        }
        return qc, img
    except Exception as e:
        return {"probe_error": str(e)}, None


def _extract_sidecar_meta(
    bsc: BlobServiceClient,
    *,
    container: str,
    blob_name: str,
) -> Dict[str, Any]:
    """
    Tries same-stem .json next to the image.
    Example:
      foo/bar/image_0001.png -> foo/bar/image_0001.json
    """
    stem, _ext = os.path.splitext(blob_name)
    sidecar_blob = f"{stem}.json"
    raw = _download_text_if_exists(bsc, container=container, blob_name=sidecar_blob)
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _extract_alt_asset_uris(sidecar: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for key in ("alt_asset_uris", "alt_urls", "alternate_assets", "alternate_urls"):
        vals = sidecar.get(key)
        for v in _as_list(vals):
            if isinstance(v, str) and (v.startswith("az://") or v.startswith("http://") or v.startswith("https://")):
                out.append(v)
    # de-dupe
    seen = set()
    uniq: List[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:3]


def _merge_source_text(blob_name: str, blob_metadata: Dict[str, str], sidecar: Dict[str, Any]) -> str:
    texts = [blob_name]
    for k, v in (blob_metadata or {}).items():
        texts.append(f"{k}:{v}")
    for k in ("prompt", "caption", "title", "description", "gender", "framing", "pose", "region", "tags"):
        if k in sidecar:
            texts.append(f"{k}:{sidecar.get(k)}")
    return " | ".join(_normalize_text(x) for x in texts if x is not None)


def _infer_from_metadata(meta: Dict[str, Any], key_candidates: Sequence[str]) -> str:
    for k in key_candidates:
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return ""


def _infer_gender(source_text: str, sidecar: Dict[str, Any]) -> Tuple[str, float]:
    direct = _infer_from_metadata(sidecar, ("gender", "target_gender", "sex"))
    if direct in ("female", "male"):
        return direct, 1.0

    for gender, hints in GENDER_HINTS.items():
        for h in hints:
            if h in source_text:
                return gender, 0.75

    return "unknown", 0.0


def _infer_framing(source_text: str, sidecar: Dict[str, Any], width: int, height: int) -> Tuple[str, float]:
    direct = _infer_from_metadata(sidecar, ("framing", "shot_type", "crop_type"))
    if direct in ("full_body", "three_quarter"):
        return direct, 1.0

    for framing, hints in FRAMING_HINTS.items():
        for h in hints:
            if h in source_text:
                return framing, 0.8

    # soft fallback by portrait ratio only
    ratio = width / float(height or 1)
    if ratio <= 0.8 and height >= int(width * 1.25):
        return "full_body", 0.35
    if ratio <= 0.95:
        return "three_quarter", 0.2

    return "unknown", 0.0


def _infer_pose(source_text: str, sidecar: Dict[str, Any]) -> Tuple[str, float]:
    direct = _infer_from_metadata(sidecar, ("pose", "view"))
    if direct in ("front", "side"):
        return direct, 1.0

    for pose, hints in POSE_HINTS.items():
        for h in hints:
            if h in source_text:
                return pose, 0.8

    # default cautiously to front with low confidence
    return "front", 0.25


def _infer_region(source_text: str, sidecar: Dict[str, Any], default_region: str) -> str:
    direct = _infer_from_metadata(sidecar, ("region", "country", "market"))
    if direct:
        return direct
    if "india" in source_text or "indian" in source_text:
        return "india"
    return default_region


def _infer_style_tags(source_text: str, sidecar: Dict[str, Any]) -> List[str]:
    tags = []
    for t in _as_list(sidecar.get("style_tags")):
        if isinstance(t, str) and t.strip():
            tags.append(t.strip().lower())

    if "catalog" in source_text:
        tags.append("catalog")
    if "clean" in source_text and "background" in source_text:
        tags.append("clean_bg")
    if "ethnic" in source_text or "indian" in source_text:
        tags.append("ethnic_friendly")

    out: List[str] = []
    seen = set()
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _infer_allowed_garments(gender: str, sidecar: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    allowed = []
    preferred = []

    for x in _as_list(sidecar.get("allowed_garment_kinds")):
        if isinstance(x, str) and x.strip():
            allowed.append(x.strip())
    for x in _as_list(sidecar.get("preferred_garment_kinds")):
        if isinstance(x, str) and x.strip():
            preferred.append(x.strip())

    if allowed:
        return allowed, preferred

    if gender == "female":
        allowed = ["salwar_suit", "lehenga_set"]
        preferred = ["salwar_suit"]
    elif gender == "male":
        allowed = ["kurta_pyjama", "dhoti_kurta", "sherwani"]
        preferred = ["kurta_pyjama"]
    return allowed, preferred


def _score_candidate(
    *,
    width: int,
    height: int,
    qc: Dict[str, Any],
    gender_conf: float,
    framing_conf: float,
    pose_conf: float,
    source_text: str,
    style_tags: List[str],
) -> Tuple[float, float, List[str], List[str]]:
    """
    Returns: (score_0_100, confidence_0_1, review_reasons, reject_reasons)
    """
    review_reasons: List[str] = []
    reject_reasons: List[str] = []

    resolution_score = _safe_float(qc.get("resolution_score"))
    portrait_score = _safe_float(qc.get("portrait_score"))
    gray_std = _safe_float(qc.get("gray_std"))
    edge_mean = _safe_float(qc.get("edge_mean"))
    edge_std = _safe_float(qc.get("edge_std"))

    visual_score = (
        (resolution_score * 35.0)
        + (portrait_score * 15.0)
        + min(gray_std / 45.0, 1.0) * 15.0
        + min(edge_mean / 22.0, 1.0) * 15.0
        + min(edge_std / 25.0, 1.0) * 10.0
    )

    meta_score = (
        gender_conf * 15.0
        + framing_conf * 10.0
        + pose_conf * 5.0
    )

    if "clean_bg" in style_tags:
        meta_score += 3.0
    if "catalog" in style_tags:
        meta_score += 2.0

    score = round(min(100.0, visual_score + meta_score), 3)
    confidence = round(min(1.0, (gender_conf + framing_conf + pose_conf) / 3.0), 4)

    if min(width, height) < 640:
        reject_reasons.append("resolution_too_low")
    elif min(width, height) < 768:
        review_reasons.append("resolution_borderline")

    if gray_std < 18.0:
        review_reasons.append("low_tonal_variation")
    if edge_mean < 6.0:
        review_reasons.append("possibly_blurry_or_flat")
    if portrait_score < 0.25:
        review_reasons.append("non_portrait_aspect")

    if gender_conf < 0.5:
        review_reasons.append("gender_low_confidence")
    if framing_conf < 0.5:
        review_reasons.append("framing_low_confidence")
    if pose_conf < 0.3:
        review_reasons.append("pose_low_confidence")

    text = source_text
    if "side" in text or "profile" in text:
        review_reasons.append("possibly_side_pose")
    if "cropped" in text:
        review_reasons.append("possibly_cropped")

    return score, confidence, review_reasons, reject_reasons


# -------------------------------------------------------------------
# scan / selection
# -------------------------------------------------------------------

def _discover_person_prefixes(
    bsc: BlobServiceClient,
    *,
    container: str,
    seed_prefix: str = "pools/",
    max_prefixes: int = 200,
) -> List[str]:
    """
    Auto-discovers prefixes like:
      pools/<batch>/persons/
    """
    cc = bsc.get_container_client(container)
    found: List[str] = []
    seen = set()

    for blob in cc.list_blobs(name_starts_with=seed_prefix):
        name = blob.name
        m = re.match(r"^(pools/[^/]+/persons/)", name)
        if m:
            p = m.group(1)
            if p not in seen:
                seen.add(p)
                found.append(p)
                if len(found) >= max_prefixes:
                    break

    return sorted(found)


def _iter_image_blobs(
    bsc: BlobServiceClient,
    *,
    container: str,
    prefixes: Sequence[str],
    max_images: int = 0,
) -> Iterable[Any]:
    cc = bsc.get_container_client(container)
    count = 0
    seen = set()

    for prefix in prefixes:
        for blob in cc.list_blobs(name_starts_with=prefix):
            name = blob.name
            ext = os.path.splitext(name)[1].lower()
            if ext not in IMAGE_EXTS:
                continue
            if name in seen:
                continue
            seen.add(name)
            yield blob
            count += 1
            if max_images and count >= max_images:
                return


def _build_candidate(
    bsc: BlobServiceClient,
    *,
    container: str,
    blob: Any,
    default_region: str,
) -> Candidate:
    source_blob_name = blob.name
    source_az_uri = _make_az_uri(container, source_blob_name)
    blob_metadata = getattr(blob, "metadata", {}) or {}

    sidecar = _extract_sidecar_meta(
        bsc,
        container=container,
        blob_name=source_blob_name,
    )
    source_text = _merge_source_text(source_blob_name, blob_metadata, sidecar)

    data = bsc.get_blob_client(container=container, blob=source_blob_name).download_blob().readall()
    qc, _img = _probe_image_quality(data)

    width = _safe_int(qc.get("width"))
    height = _safe_int(qc.get("height"))
    content_type = (
        getattr(getattr(blob, "content_settings", None), "content_type", None)
        or _guess_content_type(source_blob_name)
    )
    size_bytes = _safe_int(getattr(blob, "size", 0), len(data))

    gender, gender_conf = _infer_gender(source_text, sidecar)
    framing, framing_conf = _infer_framing(source_text, sidecar, width, height)
    pose, pose_conf = _infer_pose(source_text, sidecar)
    region = _infer_region(source_text, sidecar, default_region=default_region)
    age_band = _infer_from_metadata(sidecar, ("age_band",)) or "adult"
    body_type = _infer_from_metadata(sidecar, ("body_type",)) or "average"
    skin_tone = _infer_from_metadata(sidecar, ("skin_tone",)) or "medium"
    style_tags = _infer_style_tags(source_text, sidecar)
    allowed_garment_kinds, preferred_garment_kinds = _infer_allowed_garments(gender, sidecar)
    alt_asset_uris = _extract_alt_asset_uris(sidecar)

    score, confidence, review_reasons, reject_reasons = _score_candidate(
        width=width,
        height=height,
        qc=qc,
        gender_conf=gender_conf,
        framing_conf=framing_conf,
        pose_conf=pose_conf,
        source_text=source_text,
        style_tags=style_tags,
    )

    is_active = True
    if reject_reasons:
        bucket = "rejected"
    elif gender not in ("female", "male"):
        bucket = "review_needed"
        review_reasons.append("gender_unknown")
    elif framing not in ("full_body", "three_quarter"):
        bucket = "review_needed"
        review_reasons.append("framing_unknown")
    elif pose != "front":
        bucket = "review_needed"
        review_reasons.append("pose_not_front")
    elif confidence < 0.45:
        bucket = "review_needed"
        review_reasons.append("low_overall_confidence")
    else:
        bucket = _bucket_key(gender, framing, pose)

    return Candidate(
        source_container=container,
        source_blob_name=source_blob_name,
        source_az_uri=source_az_uri,
        width=width,
        height=height,
        content_type=content_type,
        size_bytes=size_bytes,
        gender=gender,
        framing=framing,
        pose=pose,
        region=region,
        age_band=age_band,
        body_type=body_type,
        skin_tone=skin_tone,
        is_active=is_active,
        style_tags=style_tags,
        allowed_garment_kinds=allowed_garment_kinds,
        preferred_garment_kinds=preferred_garment_kinds,
        source_meta=sidecar,
        source_text=source_text,
        qc=qc,
        score=score,
        confidence=confidence,
        bucket=bucket,
        review_reasons=sorted(set(review_reasons)),
        reject_reasons=sorted(set(reject_reasons)),
        alt_asset_uris=alt_asset_uris,
    )


def _sort_candidates(cands: Sequence[Candidate]) -> List[Candidate]:
    return sorted(
        cands,
        key=lambda c: (
            -float(c.score),
            -float(c.confidence),
            -int(c.height),
            -int(c.width),
            c.source_blob_name,
        ),
    )


def _select_top_by_bucket(
    buckets: Dict[str, List[Candidate]],
    *,
    female_full_body_target: int,
    female_three_quarter_target: int,
    male_full_body_target: int,
    male_three_quarter_target: int,
) -> Dict[str, List[Candidate]]:
    targets = {
        "female/full_body/front": female_full_body_target,
        "female/three_quarter/front": female_three_quarter_target,
        "male/full_body/front": male_full_body_target,
        "male/three_quarter/front": male_three_quarter_target,
    }
    selected: Dict[str, List[Candidate]] = {}
    for bucket, target in targets.items():
        selected[bucket] = _sort_candidates(buckets.get(bucket, []))[:target]
    return selected


# -------------------------------------------------------------------
# export / manifest
# -------------------------------------------------------------------

def _build_model_code(
    *,
    gender: str,
    framing: str,
    pose: str,
    index_1_based: int,
    region_code: str = "ind",
) -> str:
    return f"pm_{_gender_code(gender)}_{region_code}_{_framing_code(framing)}_{_pose_code(pose)}_{index_1_based:04d}"


def _export_selected_catalog(
    bsc: BlobServiceClient,
    *,
    selected: Dict[str, List[Candidate]],
    target_container: str,
    target_prefix: str,
    region_default: str,
    include_alt_assets: bool,
) -> Dict[str, Any]:
    """
    Writes:
      - primary.png
      - optional alt_01.png
      - meta.json
      - manifest.json
    """
    manifest_models: List[Dict[str, Any]] = []
    counters: Dict[str, int] = collections.defaultdict(int)

    for bucket in (
        "female/full_body/front",
        "female/three_quarter/front",
        "male/full_body/front",
        "male/three_quarter/front",
    ):
        bucket_cands = selected.get(bucket, [])
        for cand in bucket_cands:
            counters[bucket] += 1
            model_code = _build_model_code(
                gender=cand.gender,
                framing=cand.framing,
                pose=cand.pose,
                index_1_based=counters[bucket],
                region_code="ind" if (cand.region or region_default) == "india" else "glb",
            )

            model_dir = f"{target_prefix.rstrip('/')}/{cand.gender}/{cand.framing}/{cand.pose}/{model_code}"
            primary_blob_name = f"{model_dir}/primary.png"
            primary_data = _download_az_bytes(bsc, cand.source_az_uri)
            primary_az_uri = _upload_bytes(
                bsc,
                container=target_container,
                blob_name=primary_blob_name,
                data=primary_data,
                content_type="image/png",
                overwrite=True,
            )

            assets = [
                {
                    "role": "primary",
                    "url": primary_az_uri,
                    "width": cand.width,
                    "height": cand.height,
                }
            ]

            if include_alt_assets and cand.alt_asset_uris:
                try:
                    alt_data = _download_bytes(bsc, cand.alt_asset_uris[0])
                    alt_blob_name = f"{model_dir}/alt_01.png"
                    alt_az_uri = _upload_bytes(
                        bsc,
                        container=target_container,
                        blob_name=alt_blob_name,
                        data=alt_data,
                        content_type="image/png",
                        overwrite=True,
                    )
                    assets.append(
                        {
                            "role": "alt_pose",
                            "url": alt_az_uri,
                            "width": None,
                            "height": None,
                        }
                    )
                except Exception as e:
                    # keep export resilient; alt is optional
                    cand.review_reasons.append(f"alt_copy_failed:{e}")

            meta_json = {
                "model_code": model_code,
                "gender": cand.gender,
                "age_band": cand.age_band,
                "region": cand.region or region_default,
                "framing": cand.framing,
                "pose": cand.pose,
                "body_type": cand.body_type,
                "skin_tone": cand.skin_tone,
                "quality_score": cand.score,
                "confidence": cand.confidence,
                "allowed_garment_kinds": cand.allowed_garment_kinds,
                "preferred_garment_kinds": cand.preferred_garment_kinds,
                "source_az_uri": cand.source_az_uri,
                "primary_asset": "primary.png",
                "alternate_assets": [a["url"] for a in assets if a["role"] != "primary"],
                "style_tags": cand.style_tags,
                "qc": cand.qc,
                "review_reasons": cand.review_reasons,
                "source_meta": cand.source_meta,
            }
            _upload_text_json(
                bsc,
                container=target_container,
                blob_name=f"{model_dir}/meta.json",
                obj=meta_json,
                overwrite=True,
            )

            manifest_models.append(
                {
                    "model_code": model_code,
                    "gender": cand.gender,
                    "age_band": cand.age_band,
                    "region": cand.region or region_default,
                    "framing": cand.framing,
                    "pose": cand.pose,
                    "body_type": cand.body_type,
                    "skin_tone": cand.skin_tone,
                    "style_tags": cand.style_tags,
                    "quality_score": round(float(cand.score), 3),
                    "is_active": True,
                    "allowed_garment_kinds": cand.allowed_garment_kinds,
                    "preferred_garment_kinds": cand.preferred_garment_kinds,
                    "assets": assets,
                    "qc": cand.qc,
                    "meta": {
                        "source_az_uri": cand.source_az_uri,
                        "source_blob_name": cand.source_blob_name,
                        "review_reasons": cand.review_reasons,
                    },
                }
            )

    manifest = {
        "version": "1.0",
        "catalog_code": "platform_models_v1",
        "container": target_container,
        "prefix": target_prefix,
        "defaults": {
            "region": region_default,
            "age_band": "adult",
            "is_active": True,
        },
        "models": manifest_models,
    }

    _upload_text_json(
        bsc,
        container=target_container,
        blob_name=f"{target_prefix.rstrip('/')}/manifest.json",
        obj=manifest,
        overwrite=True,
    )

    return manifest


# -------------------------------------------------------------------
# reporting
# -------------------------------------------------------------------

def _candidate_to_report_row(c: Candidate) -> Dict[str, Any]:
    return {
        "source_az_uri": c.source_az_uri,
        "source_blob_name": c.source_blob_name,
        "width": c.width,
        "height": c.height,
        "size_bytes": c.size_bytes,
        "gender": c.gender,
        "framing": c.framing,
        "pose": c.pose,
        "region": c.region,
        "age_band": c.age_band,
        "body_type": c.body_type,
        "skin_tone": c.skin_tone,
        "style_tags": c.style_tags,
        "allowed_garment_kinds": c.allowed_garment_kinds,
        "preferred_garment_kinds": c.preferred_garment_kinds,
        "score": c.score,
        "confidence": c.confidence,
        "bucket": c.bucket,
        "review_reasons": c.review_reasons,
        "reject_reasons": c.reject_reasons,
        "qc": c.qc,
    }


def _write_reports(
    *,
    review_dir: str,
    scan_summary: Dict[str, Any],
    selected: Dict[str, List[Candidate]],
    review_needed: List[Candidate],
    rejected: List[Candidate],
    manifest: Dict[str, Any],
) -> None:
    _mkdirp(review_dir)

    selected_rows: List[Dict[str, Any]] = []
    for bucket, items in selected.items():
        for c in items:
            row = _candidate_to_report_row(c)
            row["selected_bucket"] = bucket
            selected_rows.append(row)

    review_rows = [_candidate_to_report_row(c) for c in review_needed]
    rejected_rows = [_candidate_to_report_row(c) for c in rejected]

    _write_json_file(os.path.join(review_dir, "summary.json"), scan_summary)
    _write_json_file(os.path.join(review_dir, "selected.json"), {"items": selected_rows})
    _write_json_file(os.path.join(review_dir, "review_needed.json"), {"items": review_rows})
    _write_json_file(os.path.join(review_dir, "rejected.json"), {"items": rejected_rows})
    _write_json_file(os.path.join(review_dir, "manifest.preview.json"), manifest)

    md_lines = [
        "# platform model bootstrap report",
        "",
        f"- scanned_images: {scan_summary.get('scanned_images')}",
        f"- selected_models: {scan_summary.get('selected_models')}",
        f"- review_needed: {scan_summary.get('review_needed')}",
        f"- rejected: {scan_summary.get('rejected')}",
        f"- target_container: {scan_summary.get('target_container')}",
        f"- target_prefix: {scan_summary.get('target_prefix')}",
        "",
        "## selected by bucket",
    ]
    for bucket, count in scan_summary.get("selected_by_bucket", {}).items():
        md_lines.append(f"- {bucket}: {count}")
    md_lines.append("")
    md_lines.append("## missing targets")
    for bucket, missing in scan_summary.get("missing_targets", {}).items():
        md_lines.append(f"- {bucket}: {missing}")
    md_lines.append("")
    md_lines.append("## notes")
    md_lines.append("- review_needed.json contains candidates that need a quick human pass.")
    md_lines.append("- rejected.json contains clearly poor or low-confidence candidates.")
    md_lines.append("- manifest.preview.json mirrors the uploaded manifest.json.")
    with open(os.path.join(review_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))


# -------------------------------------------------------------------
# main
# -------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bootstrap a staging platform-model catalog from Azure person pools."
    )
    ap.add_argument("--source-container", default=os.environ.get("PLATFORM_MODELS_SOURCE_CONTAINER", "commerce-training"))
    ap.add_argument(
        "--source-prefix",
        action="append",
        default=[],
        help="Repeatable. Example: pools/20260222_165920_e8aa84d6/persons/",
    )
    ap.add_argument(
        "--auto-discover-source-prefixes",
        action="store_true",
        help="Discover prefixes like pools/<batch>/persons/ automatically when no --source-prefix is provided.",
    )
    ap.add_argument("--target-container", default=os.environ.get("PLATFORM_MODELS_TARGET_CONTAINER", "commerce-training"))
    ap.add_argument(
        "--target-prefix",
        default="",
        help="Default: pools/platform_models/staging/<run_id>",
    )
    ap.add_argument("--review-dir", default="")
    ap.add_argument("--default-region", default="india")

    ap.add_argument("--female-full-body-target", type=int, default=10)
    ap.add_argument("--female-three-quarter-target", type=int, default=4)
    ap.add_argument("--male-full-body-target", type=int, default=10)
    ap.add_argument("--male-three-quarter-target", type=int, default=4)

    ap.add_argument("--max-images", type=int, default=0, help="0 means all discovered images")
    ap.add_argument("--include-alt-assets", action="store_true", help="Copy first alt asset if sidecar metadata provides one")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    run_id = _utc_stamp()
    target_prefix = args.target_prefix.strip() or f"pools/platform_models/staging/{run_id}"
    review_dir = args.review_dir.strip() or f"/tmp/platform_models_bootstrap_{run_id}"
    _mkdirp(review_dir)

    bsc = _get_blob_service_client()

    source_prefixes = list(args.source_prefix or [])
    if not source_prefixes and args.auto_discover_source_prefixes:
        source_prefixes = _discover_person_prefixes(
            bsc,
            container=args.source_container,
            seed_prefix="pools/",
            max_prefixes=200,
        )

    if not source_prefixes:
        raise SystemExit(
            "No source prefixes provided or discovered.\n"
            "Pass --source-prefix pools/<batch>/persons/ or use --auto-discover-source-prefixes."
        )

    print("SOURCE_CONTAINER =", args.source_container)
    print("SOURCE_PREFIXES  =", source_prefixes)
    print("TARGET_CONTAINER =", args.target_container)
    print("TARGET_PREFIX    =", target_prefix)
    print("REVIEW_DIR       =", review_dir)

    buckets: Dict[str, List[Candidate]] = collections.defaultdict(list)
    review_needed: List[Candidate] = []
    rejected: List[Candidate] = []
    scanned_images = 0

    for blob in _iter_image_blobs(
        bsc,
        container=args.source_container,
        prefixes=source_prefixes,
        max_images=args.max_images,
    ):
        scanned_images += 1
        try:
            cand = _build_candidate(
                bsc,
                container=args.source_container,
                blob=blob,
                default_region=args.default_region,
            )
            if cand.bucket == "review_needed":
                review_needed.append(cand)
            elif cand.bucket == "rejected":
                rejected.append(cand)
            else:
                buckets[cand.bucket].append(cand)
        except Exception as e:
            rejected.append(
                Candidate(
                    source_container=args.source_container,
                    source_blob_name=blob.name,
                    source_az_uri=_make_az_uri(args.source_container, blob.name),
                    width=0,
                    height=0,
                    content_type=_guess_content_type(blob.name),
                    size_bytes=_safe_int(getattr(blob, "size", 0)),
                    gender="unknown",
                    framing="unknown",
                    pose="unknown",
                    region=args.default_region,
                    age_band="adult",
                    body_type="average",
                    skin_tone="medium",
                    is_active=False,
                    style_tags=[],
                    allowed_garment_kinds=[],
                    preferred_garment_kinds=[],
                    source_meta={},
                    source_text=blob.name,
                    qc={"error": str(e)},
                    score=0.0,
                    confidence=0.0,
                    bucket="rejected",
                    review_reasons=[],
                    reject_reasons=[f"scan_error:{e}"],
                    alt_asset_uris=[],
                )
            )

    selected = _select_top_by_bucket(
        buckets,
        female_full_body_target=args.female_full_body_target,
        female_three_quarter_target=args.female_three_quarter_target,
        male_full_body_target=args.male_full_body_target,
        male_three_quarter_target=args.male_three_quarter_target,
    )

    selected_counts = {bucket: len(items) for bucket, items in selected.items()}
    missing_targets = {
        "female/full_body/front": max(0, args.female_full_body_target - selected_counts.get("female/full_body/front", 0)),
        "female/three_quarter/front": max(0, args.female_three_quarter_target - selected_counts.get("female/three_quarter/front", 0)),
        "male/full_body/front": max(0, args.male_full_body_target - selected_counts.get("male/full_body/front", 0)),
        "male/three_quarter/front": max(0, args.male_three_quarter_target - selected_counts.get("male/three_quarter/front", 0)),
    }

    if args.dry_run:
        manifest = {
            "version": "1.0",
            "catalog_code": "platform_models_v1",
            "container": args.target_container,
            "prefix": target_prefix,
            "defaults": {
                "region": args.default_region,
                "age_band": "adult",
                "is_active": True,
            },
            "models": [],
        }
    else:
        manifest = _export_selected_catalog(
            bsc,
            selected=selected,
            target_container=args.target_container,
            target_prefix=target_prefix,
            region_default=args.default_region,
            include_alt_assets=args.include_alt_assets,
        )

    scan_summary = {
        "run_id": run_id,
        "scanned_images": scanned_images,
        "selected_models": sum(len(v) for v in selected.values()),
        "review_needed": len(review_needed),
        "rejected": len(rejected),
        "source_container": args.source_container,
        "source_prefixes": source_prefixes,
        "target_container": args.target_container,
        "target_prefix": target_prefix,
        "selected_by_bucket": selected_counts,
        "missing_targets": missing_targets,
        "dry_run": args.dry_run,
    }

    _write_reports(
        review_dir=review_dir,
        scan_summary=scan_summary,
        selected=selected,
        review_needed=review_needed,
        rejected=rejected,
        manifest=manifest,
    )

    # upload reports too, unless dry_run
    if not args.dry_run:
        _upload_text_json(
            bsc,
            container=args.target_container,
            blob_name=f"{target_prefix.rstrip('/')}/_reports/summary.json",
            obj=scan_summary,
            overwrite=True,
        )
        _upload_text_json(
            bsc,
            container=args.target_container,
            blob_name=f"{target_prefix.rstrip('/')}/_reports/review_needed.json",
            obj={"items": [_candidate_to_report_row(c) for c in review_needed]},
            overwrite=True,
        )
        _upload_text_json(
            bsc,
            container=args.target_container,
            blob_name=f"{target_prefix.rstrip('/')}/_reports/rejected.json",
            obj={"items": [_candidate_to_report_row(c) for c in rejected]},
            overwrite=True,
        )

    print("\nDONE")
    print("review_dir =", review_dir)
    print("target_manifest =", _make_az_uri(args.target_container, f"{target_prefix.rstrip('/')}/manifest.json"))
    print("selected_by_bucket =", selected_counts)
    print("missing_targets =", missing_targets)

    # fail hard only if nothing useful was selected
    if sum(len(v) for v in selected.values()) == 0:
        raise SystemExit(2)


if __name__ == "__main__":
    main()