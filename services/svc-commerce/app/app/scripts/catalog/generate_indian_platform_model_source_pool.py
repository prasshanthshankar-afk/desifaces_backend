#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    from azure.storage.blob import BlobServiceClient, ContentSettings
except Exception as e:  # pragma: no cover
    raise SystemExit(
        "azure-storage-blob is required for this script.\n"
        f"Import error: {e}"
    )


# -------------------------------------------------------------------
# small helpers
# -------------------------------------------------------------------

def _utc_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def _mkdirp(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, sort_keys=True, default=str)


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _guess_content_type(name: str) -> str:
    ct, _ = mimetypes.guess_type(name)
    return ct or "application/octet-stream"


def _guess_ext_from_content_type(content_type: Optional[str], fallback: str = ".png") -> str:
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "webp" in ct:
        return ".webp"
    return fallback


def _guess_ext_from_url(url: str, fallback: str = ".png") -> str:
    try:
        path = urllib.parse.urlparse(url).path
        base = os.path.basename(path)
        if "." in base:
            ext = "." + base.split(".")[-1].lower()
            if ext in {".png", ".jpg", ".jpeg", ".webp"}:
                return ext
    except Exception:
        pass
    return fallback


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


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


def _normalize_region(region: str) -> str:
    s = (region or "").strip().lower()
    if s in {"india", "indian"}:
        return "india"
    return s or "india"


def _make_az_uri(container: str, blob_name: str) -> str:
    return f"az://{container}/{blob_name}"


# -------------------------------------------------------------------
# HTTP helpers
# -------------------------------------------------------------------

def _http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body_obj: Optional[Any] = None,
    timeout_s: int = 180,
) -> Tuple[int, Dict[str, Any], str]:
    hdrs = dict(headers or {})
    data = None
    if body_obj is not None:
        data = json.dumps(body_obj).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
            code = resp.getcode()
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {"_raw": raw}
            return code, payload, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"_raw": raw}
        return e.code, payload, raw


def _http_bytes(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout_s: int = 180,
) -> Tuple[int, bytes, Dict[str, str]]:
    req = urllib.request.Request(url, headers=dict(headers or {}), method=method)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        return resp.getcode(), data, hdrs


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


def _upload_json(
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


# -------------------------------------------------------------------
# fal queue helpers
# -------------------------------------------------------------------

def _fal_headers(
    *,
    fal_key: str,
    output_expiration_s: Optional[int] = None,
) -> Dict[str, str]:
    headers = {
        "Authorization": f"Key {fal_key}",
        "Content-Type": "application/json",
    }
    if output_expiration_s and output_expiration_s > 0:
        headers["X-Fal-Object-Lifecycle-Preference"] = json.dumps(
            {"expiration_duration_seconds": int(output_expiration_s)}
        )
    return headers


def _fal_submit_url(model_id: str) -> str:
    return f"https://queue.fal.run/{model_id.strip().strip('/')}"


def _fal_top_level_model_id(model_id: str) -> str:
    parts = [p for p in model_id.strip().strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Invalid fal model id: {model_id}")
    return "/".join(parts[:2])


def _fal_status_url(model_id: str, request_id: str) -> str:
    top = _fal_top_level_model_id(model_id)
    return f"https://queue.fal.run/{top}/requests/{request_id}/status"


def _fal_result_url(model_id: str, request_id: str) -> str:
    top = _fal_top_level_model_id(model_id)
    return f"https://queue.fal.run/{top}/requests/{request_id}"


def _fal_submit(
    *,
    model_id: str,
    fal_key: str,
    payload: Dict[str, Any],
    output_expiration_s: Optional[int],
    timeout_s: int,
) -> Dict[str, Any]:
    code, resp, raw = _http_json(
        "POST",
        _fal_submit_url(model_id),
        headers=_fal_headers(fal_key=fal_key, output_expiration_s=output_expiration_s),
        body_obj=payload,
        timeout_s=timeout_s,
    )
    if code >= 400:
        raise RuntimeError(f"fal submit failed code={code} resp={resp or raw}")
    if not resp.get("request_id"):
        raise RuntimeError(f"fal submit missing request_id resp={resp or raw}")
    return resp


def _fal_wait_result(
    *,
    model_id: str,
    fal_key: str,
    request_id: str,
    poll_timeout_s: int,
    poll_interval_s: int,
    timeout_s: int,
) -> Dict[str, Any]:
    started = time.time()
    last_payload: Dict[str, Any] = {}

    while True:
        code, status_payload, raw = _http_json(
            "GET",
            _fal_status_url(model_id, request_id),
            headers={"Authorization": f"Key {fal_key}"},
            timeout_s=timeout_s,
        )
        if code >= 400:
            raise RuntimeError(f"fal status failed code={code} resp={status_payload or raw}")

        last_payload = status_payload if isinstance(status_payload, dict) else {"raw": status_payload}
        st = str(last_payload.get("status") or "").upper()

        if st in {"COMPLETED"}:
            break
        if st in {"FAILED", "CANCELLED"}:
            raise RuntimeError(f"fal request failed request_id={request_id} payload={last_payload}")

        if time.time() - started > poll_timeout_s:
            raise RuntimeError(
                f"fal polling timeout > {poll_timeout_s}s request_id={request_id} last_payload={last_payload}"
            )
        time.sleep(poll_interval_s)

    code, result_payload, raw = _http_json(
        "GET",
        _fal_result_url(model_id, request_id),
        headers={"Authorization": f"Key {fal_key}"},
        timeout_s=timeout_s,
    )
    if code >= 400:
        raise RuntimeError(f"fal result failed code={code} resp={result_payload or raw}")
    return result_payload


def _collect_urls(obj: Any, *, limit: int = 50) -> List[str]:
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
            for _, v in x.items():
                rec(v)
            return
        if isinstance(x, (list, tuple)):
            for v in x:
                rec(v)

    rec(obj)
    seen = set()
    uniq = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _extract_first_image_url(result_payload: Dict[str, Any]) -> str:
    # Common fal result shapes
    response = result_payload.get("response")
    if isinstance(response, dict):
        for key in ("images", "data", "output"):
            val = response.get(key)
            for item in _as_list(val):
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    return item["url"]

    for key in ("images", "data", "output"):
        val = result_payload.get(key)
        for item in _as_list(val):
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                return item["url"]

    urls = _collect_urls(result_payload, limit=20)
    if urls:
        return urls[0]
    return ""


# -------------------------------------------------------------------
# prompt generation
# -------------------------------------------------------------------

FEMALE_SKIN_TONES = ["light brown", "medium brown", "warm brown", "deep brown"]
MALE_SKIN_TONES = ["light brown", "medium brown", "warm brown", "deep brown"]

FEMALE_BODY_TYPES = ["slim", "average", "curvy", "athletic"]
MALE_BODY_TYPES = ["slim", "average", "athletic", "broad-shouldered"]

FEMALE_HEIGHTS = ["petite", "average height", "tall"]
MALE_HEIGHTS = ["average height", "tall"]

FEMALE_HAIR = [
    "dark straight hair tied back neatly",
    "dark wavy hair tied back neatly",
    "dark hair in a low ponytail",
]
MALE_HAIR = [
    "short dark hair",
    "short neatly styled black hair",
    "short dark hair with a clean natural look",
]

FEMALE_ALLOWED = ["salwar_suit", "lehenga_set"]
MALE_ALLOWED = ["kurta_pyjama", "dhoti_kurta", "sherwani"]


@dataclass
class JobSpec:
    gender: str
    index_1_based: int
    model_code: str
    prompt: str
    framing: str
    pose: str
    region: str
    age_band: str
    body_type: str
    skin_tone: str
    style_tags: List[str]
    allowed_garment_kinds: List[str]
    preferred_garment_kinds: List[str]
    seed: Optional[int]


def _build_job_specs(
    *,
    female_count: int,
    male_count: int,
    region: str,
    seed_base: int,
    include_three_quarter: bool,
) -> List[JobSpec]:
    rng = random.Random(seed_base)
    jobs: List[JobSpec] = []

    def make_prompt(
        *,
        gender: str,
        skin_tone: str,
        body_type: str,
        height_desc: str,
        hair_desc: str,
        framing: str,
        pose: str,
    ) -> str:
        gender_word = "Indian woman" if gender == "female" else "Indian man"
        framing_phrase = "full-body" if framing == "full_body" else "three-quarter body"
        pose_phrase = "front-facing" if pose == "front" else "slight three-quarter facing"
        clothing_phrase = (
            "wearing neutral fitted beige base clothing suitable for virtual try-on, "
            "plain solid colors, no jacket, no scarf, no dupatta, no shawl, no heavy accessories"
        )
        return (
            f"Photorealistic studio catalog photo of an adult {gender_word}, South Asian appearance, "
            f"{height_desc}, {body_type} build, {skin_tone} skin tone, {hair_desc}, "
            f"{framing_phrase}, {pose_phrase}, standing upright, centered, arms relaxed slightly away from torso, "
            f"feet visible if full-body, plain light gray seamless background, soft even lighting, "
            f"neutral expression, realistic skin texture, realistic hands, natural proportions, "
            f"{clothing_phrase}, no text, no watermark, no logo, no bag, no sunglasses, no crowd, "
            f"no dramatic pose, no saree, no lehenga, no sherwani, no kurta, no salwar suit, no dhoti."
        )

    # Females
    for i in range(1, female_count + 1):
        framing = "full_body"
        if include_three_quarter and i > max(1, female_count - max(1, female_count // 5)):
            framing = "three_quarter"
        pose = "front"
        skin_tone = FEMALE_SKIN_TONES[(i - 1) % len(FEMALE_SKIN_TONES)]
        body_type = FEMALE_BODY_TYPES[(i - 1) % len(FEMALE_BODY_TYPES)]
        height_desc = FEMALE_HEIGHTS[(i - 1) % len(FEMALE_HEIGHTS)]
        hair_desc = FEMALE_HAIR[(i - 1) % len(FEMALE_HAIR)]
        model_code = f"src_f_ind_{'fb' if framing == 'full_body' else '3q'}_fr_{i:04d}"
        seed = rng.randint(1, 2_000_000_000)
        jobs.append(
            JobSpec(
                gender="female",
                index_1_based=i,
                model_code=model_code,
                prompt=make_prompt(
                    gender="female",
                    skin_tone=skin_tone,
                    body_type=body_type,
                    height_desc=height_desc,
                    hair_desc=hair_desc,
                    framing=framing,
                    pose=pose,
                ),
                framing=framing,
                pose=pose,
                region=region,
                age_band="adult",
                body_type=body_type,
                skin_tone=skin_tone,
                style_tags=["catalog", "clean_bg", "ethnic_friendly", "platform_model_source"],
                allowed_garment_kinds=FEMALE_ALLOWED,
                preferred_garment_kinds=["salwar_suit"],
                seed=seed,
            )
        )

    # Males
    for i in range(1, male_count + 1):
        framing = "full_body"
        if include_three_quarter and i > max(1, male_count - max(1, male_count // 5)):
            framing = "three_quarter"
        pose = "front"
        skin_tone = MALE_SKIN_TONES[(i - 1) % len(MALE_SKIN_TONES)]
        body_type = MALE_BODY_TYPES[(i - 1) % len(MALE_BODY_TYPES)]
        height_desc = MALE_HEIGHTS[(i - 1) % len(MALE_HEIGHTS)]
        hair_desc = MALE_HAIR[(i - 1) % len(MALE_HAIR)]
        model_code = f"src_m_ind_{'fb' if framing == 'full_body' else '3q'}_fr_{i:04d}"
        seed = rng.randint(1, 2_000_000_000)
        jobs.append(
            JobSpec(
                gender="male",
                index_1_based=i,
                model_code=model_code,
                prompt=make_prompt(
                    gender="male",
                    skin_tone=skin_tone,
                    body_type=body_type,
                    height_desc=height_desc,
                    hair_desc=hair_desc,
                    framing=framing,
                    pose=pose,
                ),
                framing=framing,
                pose=pose,
                region=region,
                age_band="adult",
                body_type=body_type,
                skin_tone=skin_tone,
                style_tags=["catalog", "clean_bg", "ethnic_friendly", "platform_model_source"],
                allowed_garment_kinds=MALE_ALLOWED,
                preferred_garment_kinds=["kurta_pyjama"],
                seed=seed,
            )
        )

    return jobs


def _build_fal_input(
    *,
    job: JobSpec,
    width: int,
    height: int,
    extra_input: Dict[str, Any],
    include_seed: bool,
) -> Dict[str, Any]:
    payload = dict(extra_input or {})
    payload["prompt"] = job.prompt

    # only set these if not already passed in extra_input
    payload.setdefault("image_size", {"width": width, "height": height})

    if include_seed:
        payload.setdefault("seed", job.seed)

    return payload


# -------------------------------------------------------------------
# main generator flow
# -------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate an Indian platform-model source pool in Azure for later catalog bootstrap."
    )
    ap.add_argument("--target-container", default=os.environ.get("PLATFORM_MODELS_SOURCE_CONTAINER", "commerce-training"))
    ap.add_argument(
        "--target-prefix",
        default=os.environ.get("PLATFORM_MODELS_SOURCE_PREFIX", ""),
        help="Default: pools/platform_models_source/v1/<run_id>",
    )
    ap.add_argument("--female-count", type=int, default=20)
    ap.add_argument("--male-count", type=int, default=20)
    ap.add_argument("--region", default="india")
    ap.add_argument("--model-id", default=os.environ.get("PLATFORM_MODELS_SOURCE_FAL_MODEL", "fal-ai/flux-1/dev"))
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1536)
    ap.add_argument("--seed-base", type=int, default=20260307)
    ap.add_argument("--include-seed", action="store_true")
    ap.add_argument("--include-three-quarter", action="store_true")
    ap.add_argument("--extra-input-json", default="", help="Optional JSON merged into fal input")
    ap.add_argument("--poll-timeout-s", type=int, default=900)
    ap.add_argument("--poll-interval-s", type=int, default=6)
    ap.add_argument("--http-timeout-s", type=int, default=180)
    ap.add_argument("--output-expiration-s", type=int, default=7 * 24 * 3600)
    ap.add_argument("--review-dir", default="")
    ap.add_argument("--dry-run", action="store_true", help="Do not call fal or upload Azure artifacts; only write local plan")
    args = ap.parse_args()

    fal_key = os.environ.get("FAL_KEY", "").strip()
    if not args.dry_run and not fal_key:
        raise SystemExit("FAL_KEY is required unless --dry-run is used")

    run_id = _utc_stamp()
    target_prefix = args.target_prefix.strip() or f"pools/platform_models_source/v1/{run_id}"
    review_dir = args.review_dir.strip() or f"/tmp/generate_indian_platform_model_source_pool_{run_id}"
    _mkdirp(review_dir)

    region = _normalize_region(args.region)
    extra_input: Dict[str, Any] = {}
    if args.extra_input_json.strip():
        extra_input = json.loads(args.extra_input_json)

    jobs = _build_job_specs(
        female_count=args.female_count,
        male_count=args.male_count,
        region=region,
        seed_base=args.seed_base,
        include_three_quarter=args.include_three_quarter,
    )

    _write_json(
        os.path.join(review_dir, "plan.json"),
        {
            "run_id": run_id,
            "target_container": args.target_container,
            "target_prefix": target_prefix,
            "model_id": args.model_id,
            "female_count": args.female_count,
            "male_count": args.male_count,
            "region": region,
            "width": args.width,
            "height": args.height,
            "include_seed": args.include_seed,
            "include_three_quarter": args.include_three_quarter,
            "job_count": len(jobs),
            "jobs": [
                {
                    "model_code": j.model_code,
                    "gender": j.gender,
                    "framing": j.framing,
                    "pose": j.pose,
                    "region": j.region,
                    "age_band": j.age_band,
                    "body_type": j.body_type,
                    "skin_tone": j.skin_tone,
                    "allowed_garment_kinds": j.allowed_garment_kinds,
                    "preferred_garment_kinds": j.preferred_garment_kinds,
                    "seed": j.seed,
                    "prompt": j.prompt,
                }
                for j in jobs
            ],
        },
    )

    if args.dry_run:
        print("DRY RUN")
        print("review_dir =", review_dir)
        print("plan_file  =", os.path.join(review_dir, "plan.json"))
        return

    bsc = _get_blob_service_client()

    summary_items: List[Dict[str, Any]] = []
    source_manifest_items: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for idx, job in enumerate(jobs, 1):
        print(f"\n[{idx}/{len(jobs)}] generating {job.model_code} ({job.gender}, {job.framing})")

        item_dir = f"{target_prefix.rstrip('/')}/{job.gender}/{job.framing}/{job.pose}/{job.model_code}"
        local_debug: Dict[str, Any] = {
            "model_code": job.model_code,
            "gender": job.gender,
            "framing": job.framing,
            "pose": job.pose,
            "region": job.region,
            "prompt": job.prompt,
            "seed": job.seed,
            "item_dir": item_dir,
            "status": "started",
        }

        try:
            fal_input = _build_fal_input(
                job=job,
                width=args.width,
                height=args.height,
                extra_input=extra_input,
                include_seed=args.include_seed,
            )

            submit_resp = _fal_submit(
                model_id=args.model_id,
                fal_key=fal_key,
                payload=fal_input,
                output_expiration_s=args.output_expiration_s,
                timeout_s=args.http_timeout_s,
            )
            request_id = str(submit_resp["request_id"])
            local_debug["request_id"] = request_id
            _write_json(os.path.join(review_dir, f"{job.model_code}_submit.json"), submit_resp)

            result_payload = _fal_wait_result(
                model_id=args.model_id,
                fal_key=fal_key,
                request_id=request_id,
                poll_timeout_s=args.poll_timeout_s,
                poll_interval_s=args.poll_interval_s,
                timeout_s=args.http_timeout_s,
            )
            _write_json(os.path.join(review_dir, f"{job.model_code}_result.json"), result_payload)

            image_url = _extract_first_image_url(result_payload)
            if not image_url:
                raise RuntimeError(f"No image URL found in fal result for {job.model_code}")

            code, image_bytes, hdrs = _http_bytes(
                "GET",
                image_url,
                timeout_s=args.http_timeout_s,
            )
            if code >= 400 or not image_bytes:
                raise RuntimeError(f"Failed to download generated image for {job.model_code} url={image_url}")

            content_type = hdrs.get("content-type", "")
            ext = _guess_ext_from_content_type(content_type, _guess_ext_from_url(image_url, ".png"))
            image_blob_name = f"{item_dir}/primary{ext}"
            image_az_uri = _upload_bytes(
                bsc,
                container=args.target_container,
                blob_name=image_blob_name,
                data=image_bytes,
                content_type=content_type or _guess_content_type(image_blob_name),
                overwrite=True,
            )

            primary_sidecar = {
                "model_code": job.model_code,
                "gender": job.gender,
                "framing": job.framing,
                "pose": job.pose,
                "region": job.region,
                "age_band": job.age_band,
                "body_type": job.body_type,
                "skin_tone": job.skin_tone,
                "style_tags": job.style_tags,
                "allowed_garment_kinds": job.allowed_garment_kinds,
                "preferred_garment_kinds": job.preferred_garment_kinds,
                "source_kind": "platform_model_source",
                "quality_score": None,
                "is_active": True,
                "prompt": job.prompt,
                "seed": job.seed,
                "fal_model_id": args.model_id,
                "fal_request_id": request_id,
                "image_url_source": image_url,
                "alt_asset_uris": [],
            }
            _upload_json(
                bsc,
                container=args.target_container,
                blob_name=f"{item_dir}/primary.json",
                obj=primary_sidecar,
                overwrite=True,
            )

            meta_json = {
                "model_code": job.model_code,
                "gender": job.gender,
                "framing": job.framing,
                "pose": job.pose,
                "region": job.region,
                "age_band": job.age_band,
                "body_type": job.body_type,
                "skin_tone": job.skin_tone,
                "style_tags": job.style_tags,
                "allowed_garment_kinds": job.allowed_garment_kinds,
                "preferred_garment_kinds": job.preferred_garment_kinds,
                "primary_asset": f"primary{ext}",
                "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "prompt": job.prompt,
                "seed": job.seed,
                "fal_model_id": args.model_id,
                "fal_request_id": request_id,
                "result_payload_debug_file": f"{job.model_code}_result.json",
            }
            meta_az_uri = _upload_json(
                bsc,
                container=args.target_container,
                blob_name=f"{item_dir}/meta.json",
                obj=meta_json,
                overwrite=True,
            )

            source_manifest_items.append(
                {
                    "model_code": job.model_code,
                    "gender": job.gender,
                    "framing": job.framing,
                    "pose": job.pose,
                    "region": job.region,
                    "age_band": job.age_band,
                    "body_type": job.body_type,
                    "skin_tone": job.skin_tone,
                    "style_tags": job.style_tags,
                    "allowed_garment_kinds": job.allowed_garment_kinds,
                    "preferred_garment_kinds": job.preferred_garment_kinds,
                    "assets": [
                        {
                            "role": "primary",
                            "url": image_az_uri,
                            "content_type": content_type or _guess_content_type(image_blob_name),
                        }
                    ],
                    "meta_url": meta_az_uri,
                    "prompt": job.prompt,
                    "seed": job.seed,
                    "fal_model_id": args.model_id,
                    "fal_request_id": request_id,
                }
            )

            local_debug.update(
                {
                    "status": "succeeded",
                    "request_id": request_id,
                    "image_url_source": image_url,
                    "image_az_uri": image_az_uri,
                    "meta_az_uri": meta_az_uri,
                }
            )
            summary_items.append(local_debug)
            _write_json(os.path.join(review_dir, f"{job.model_code}_summary.json"), local_debug)
            print(f"OK -> {image_az_uri}")

        except Exception as e:
            local_debug["status"] = "failed"
            local_debug["error"] = str(e)
            failures.append(local_debug)
            summary_items.append(local_debug)
            _write_json(os.path.join(review_dir, f"{job.model_code}_summary.json"), local_debug)
            print(f"FAILED -> {job.model_code}: {e}", file=sys.stderr)

    source_manifest = {
        "version": "1.0",
        "catalog_code": "platform_models_source_v1",
        "container": args.target_container,
        "prefix": target_prefix,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": run_id,
        "generator": {
            "script": "generate_indian_platform_model_source_pool.py",
            "fal_model_id": args.model_id,
            "width": args.width,
            "height": args.height,
            "region": region,
            "female_count_requested": args.female_count,
            "male_count_requested": args.male_count,
            "include_seed": args.include_seed,
            "include_three_quarter": args.include_three_quarter,
        },
        "items": source_manifest_items,
    }

    manifest_az_uri = _upload_json(
        bsc,
        container=args.target_container,
        blob_name=f"{target_prefix.rstrip('/')}/manifest.json",
        obj=source_manifest,
        overwrite=True,
    )

    summary = {
        "run_id": run_id,
        "target_container": args.target_container,
        "target_prefix": target_prefix,
        "review_dir": review_dir,
        "manifest_az_uri": manifest_az_uri,
        "generated_total": len(source_manifest_items),
        "failed_total": len(failures),
        "female_generated": sum(1 for x in source_manifest_items if x["gender"] == "female"),
        "male_generated": sum(1 for x in source_manifest_items if x["gender"] == "male"),
        "items": summary_items,
        "failures": failures,
    }

    _write_json(os.path.join(review_dir, "summary.json"), summary)
    _upload_json(
        bsc,
        container=args.target_container,
        blob_name=f"{target_prefix.rstrip('/')}/_reports/summary.json",
        obj=summary,
        overwrite=True,
    )

    print("\nDONE")
    print("review_dir     =", review_dir)
    print("manifest_az_uri=", manifest_az_uri)
    print("generated_total=", summary["generated_total"])
    print("failed_total   =", summary["failed_total"])

    if summary["generated_total"] == 0:
        raise SystemExit(2)


if __name__ == "__main__":
    main()