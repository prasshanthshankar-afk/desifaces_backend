from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient


# ----------------------------
# Config
# ----------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
FAL_KEY = (
    os.getenv("FAL_KEY", "").strip()
    or os.getenv("FAL_API_KEY", "").strip()
)

CONTAINER_NAME = "commerce-catalog"
CATALOG_PREFIX = "platform_models/"
WIPE_FIRST = os.getenv("PLATFORM_MODEL_WIPE_FIRST", "1").strip().lower() in {"1", "true", "yes", "y"}
POLL_INTERVAL_SEC = int(os.getenv("PLATFORM_MODEL_POLL_INTERVAL_SEC", "5"))
POLL_TIMEOUT_SEC = int(os.getenv("PLATFORM_MODEL_POLL_TIMEOUT_SEC", "600"))

# Use Krea first; fall back to base FLUX dev if needed.
FAL_ENDPOINTS = [
    "fal-ai/flux/krea",
    "fal-ai/flux/dev",
]

BASE_STYLE_TAGS = ["catalog", "studio", "starter_generated", "vton_base"]


def _allowed_garments_for_gender(gender: str) -> List[str]:
    g = str(gender or "").strip().lower()
    if g == "female":
        return ["saree_set", "salwar_suit", "lehenga_set", "upper_body", "lower_body", "dresses"]
    if g == "male":
        return ["kurta_pyjama", "dhoti_kurta", "sherwani", "upper_body", "lower_body", "dresses"]
    return ["upper_body", "lower_body", "dresses"]


def _preferred_garments_for_gender(gender: str) -> List[str]:
    g = str(gender or "").strip().lower()
    if g == "female":
        return ["saree_set", "salwar_suit", "lehenga_set"]
    if g == "male":
        return ["kurta_pyjama", "dhoti_kurta", "sherwani"]
    return []


def _spec(
    *,
    model_code: str,
    gender: str,
    region: str,
    body_type: str,
    look: str,
    skin_tone: str = "medium",
    extra_style_tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    region_norm = str(region or "").strip().lower().replace(" ", "_")
    gender_norm = str(gender or "").strip().lower()
    return {
        "model_code": model_code,
        "gender": gender_norm,
        "age_band": "adult",
        "pose": "front",
        "framing": "full_body",
        "body_type": body_type,
        "skin_tone": skin_tone,
        "region_tags": ["india", "south_asian", region_norm],
        "style_tags": BASE_STYLE_TAGS + [f"region_{region_norm}"] + list(extra_style_tags or []),
        "allowed_garment_kinds": _allowed_garments_for_gender(gender_norm),
        "preferred_garment_kinds": _preferred_garments_for_gender(gender_norm),
        "look": look,
    }


# Diverse India-first starter set: 20 Indian adult full-body models
STARTER_SPECS = [
    _spec(
        model_code="female_delhi_fullbody_001",
        gender="female",
        region="delhi",
        body_type="petite",
        look="adult Indian woman from Delhi, petite build, dark hair tied back, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="female_delhi_fullbody_002",
        gender="female",
        region="delhi",
        body_type="average",
        look="adult Indian woman from Delhi, average build, shoulder-length dark hair, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="male_delhi_fullbody_001",
        gender="male",
        region="delhi",
        body_type="slim",
        look="adult Indian man from Delhi, slim build, short dark hair, clean shaven, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="male_delhi_fullbody_002",
        gender="male",
        region="delhi",
        body_type="average",
        look="adult Indian man from Delhi, average build, short dark hair, light stubble, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="female_maharashtra_fullbody_001",
        gender="female",
        region="maharashtra",
        body_type="curvy",
        look="adult Indian woman from Maharashtra, curvy build, long dark wavy hair, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="female_maharashtra_fullbody_002",
        gender="female",
        region="maharashtra",
        body_type="tall",
        look="adult Indian woman from Maharashtra, tall build, sleek dark hair, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="male_maharashtra_fullbody_001",
        gender="male",
        region="maharashtra",
        body_type="athletic",
        look="adult Indian man from Maharashtra, athletic build, short dark hair, trimmed beard, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="male_maharashtra_fullbody_002",
        gender="male",
        region="maharashtra",
        body_type="broad",
        look="adult Indian man from Maharashtra, broad build, short dark hair, clean shaven, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="female_west_bengal_fullbody_001",
        gender="female",
        region="west_bengal",
        body_type="average",
        look="adult Indian woman from West Bengal, average build, dark hair in a low bun, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="female_west_bengal_fullbody_002",
        gender="female",
        region="west_bengal",
        body_type="petite",
        look="adult Indian woman from West Bengal, petite build, shoulder-length dark hair, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="male_west_bengal_fullbody_001",
        gender="male",
        region="west_bengal",
        body_type="slim",
        look="adult Indian man from West Bengal, slim build, short dark hair, clean shaven, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="male_west_bengal_fullbody_002",
        gender="male",
        region="west_bengal",
        body_type="average",
        look="adult Indian man from West Bengal, average build, short dark hair, trimmed beard, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="female_assam_fullbody_001",
        gender="female",
        region="assam",
        body_type="average",
        look="adult Indian woman from Assam, average build, long dark straight hair, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="male_assam_fullbody_001",
        gender="male",
        region="assam",
        body_type="slim",
        look="adult Indian man from Assam, slim build, short dark hair, clean shaven, neutral catalog look",
        skin_tone="medium",
    ),
    _spec(
        model_code="female_manipur_fullbody_001",
        gender="female",
        region="manipur",
        body_type="petite",
        look="adult Indian woman from Manipur, petite build, dark hair tied back, neutral catalog look",
        skin_tone="light_medium",
    ),
    _spec(
        model_code="male_manipur_fullbody_001",
        gender="male",
        region="manipur",
        body_type="slim",
        look="adult Indian man from Manipur, slim build, short dark hair, clean shaven, neutral catalog look",
        skin_tone="light_medium",
    ),
    _spec(
        model_code="female_mizoram_fullbody_001",
        gender="female",
        region="mizoram",
        body_type="petite",
        look="adult Indian woman from Mizoram, petite build, shoulder-length dark hair, neutral catalog look",
        skin_tone="light_medium",
    ),
    _spec(
        model_code="male_mizoram_fullbody_001",
        gender="male",
        region="mizoram",
        body_type="slim",
        look="adult Indian man from Mizoram, slim build, short dark hair, clean shaven, neutral catalog look",
        skin_tone="light_medium",
    ),
    _spec(
        model_code="female_jammu_kashmir_fullbody_001",
        gender="female",
        region="jammu_kashmir",
        body_type="average",
        look="adult Indian woman from Jammu and Kashmir, average build, long dark hair, neutral catalog look",
        skin_tone="light_medium",
    ),
    _spec(
        model_code="male_jammu_kashmir_fullbody_001",
        gender="male",
        region="jammu_kashmir",
        body_type="average",
        look="adult Indian man from Jammu and Kashmir, average build, short dark hair, light stubble, neutral catalog look",
        skin_tone="light_medium",
    ),
]


@dataclass
class GeneratedAsset:
    source_blob_name: str
    preview_blob_name: str
    source_asset_url: str
    preview_asset_url: str
    width: Optional[int]
    height: Optional[int]
    content_type: str
    endpoint: str
    request_id: str
    prompt: str


# ----------------------------
# Helpers
# ----------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_az_ref(container: str, blob_name: str) -> str:
    return f"az://{container}/{blob_name}"


def ext_from_content_type(content_type: str, fallback_url: str) -> str:
    ctype = (content_type or "").lower()
    if "png" in ctype:
        return ".png"
    if "jpeg" in ctype or "jpg" in ctype:
        return ".jpg"
    if "webp" in ctype:
        return ".webp"
    suffix = Path(fallback_url.split("?")[0]).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def http_json(
    url: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> Tuple[int, Dict[str, Any]]:
    data = None
    req_headers = headers.copy() if headers else {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url=url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.getcode(), json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {"raw_error": raw}
        return e.code, body


def http_bytes(
    url: str,
    timeout: int = 180,
) -> Tuple[bytes, str]:
    req = urllib.request.Request(url=url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        return data, content_type


def build_prompt(spec: Dict[str, Any]) -> str:
    gender = spec["gender"]
    if gender == "female":
        base_clothes = "wearing a plain fitted beige sleeveless top and plain fitted beige leggings"
    else:
        base_clothes = "wearing a plain fitted beige crew-neck t-shirt and plain fitted beige trousers"

    return (
        f"Photorealistic studio catalog photo of {spec['look']}. "
        f"Full body, front-facing, standing straight, head-to-toe visible, both arms slightly away from torso, "
        f"neutral expression, symmetrical pose, centered composition, plain light gray seamless studio background, "
        f"soft even studio lighting, realistic skin texture, {base_clothes}. "
        f"No jacket, no scarf, no dupatta, no jewelry, no bag, no props, no extra people, no text, no watermark, "
        f"no cropped feet, no cropped head."
    )


def fal_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Fal-Object-Lifecycle-Preference": json.dumps({"expiration_duration_seconds": 3600}),
    }


def fal_submit(endpoint: str, prompt: str) -> Dict[str, Any]:
    url = f"https://queue.fal.run/{endpoint}"
    status_code, body = http_json(
        url=url,
        method="POST",
        payload={"prompt": prompt},
        headers=fal_headers(),
        timeout=120,
    )
    if status_code not in (200, 201, 202):
        raise RuntimeError(f"fal submit failed endpoint={endpoint} status={status_code} body={body}")
    if "status_url" not in body or "response_url" not in body:
        raise RuntimeError(f"fal submit missing queue URLs endpoint={endpoint} body={body}")
    return body


def fal_wait_for_result(submit_payload: Dict[str, Any]) -> Dict[str, Any]:
    status_url = submit_payload["status_url"]
    response_url = submit_payload["response_url"]
    started = time.time()

    while True:
        if time.time() - started > POLL_TIMEOUT_SEC:
            raise RuntimeError(f"fal generation timed out after {POLL_TIMEOUT_SEC}s: {status_url}")

        poll_url = status_url + ("&logs=1" if "?" in status_url else "?logs=1")
        _, status_body = http_json(
            url=poll_url,
            method="GET",
            payload=None,
            headers={"Authorization": f"Key {FAL_KEY}", "Accept": "application/json"},
            timeout=120,
        )

        status = str(status_body.get("status") or "").upper()
        if status == "COMPLETED":
            break
        if status in {"FAILED", "CANCELLED", "CANCELED", "CANCELLATION_REQUESTED"}:
            raise RuntimeError(f"fal generation failed status={status} body={status_body}")

        time.sleep(POLL_INTERVAL_SEC)

    response_code, response_body = http_json(
        url=response_url,
        method="GET",
        payload=None,
        headers={"Authorization": f"Key {FAL_KEY}", "Accept": "application/json"},
        timeout=180,
    )
    if response_code != 200:
        raise RuntimeError(f"fal result fetch failed status={response_code} body={response_body}")
    return response_body


def fal_generate_one(spec: Dict[str, Any]) -> Dict[str, Any]:
    prompt = build_prompt(spec)
    last_error = None

    for endpoint in FAL_ENDPOINTS:
        try:
            submit_payload = fal_submit(endpoint=endpoint, prompt=prompt)
            result_payload = fal_wait_for_result(submit_payload=submit_payload)

            response = result_payload.get("response") if isinstance(result_payload.get("response"), dict) else result_payload
            images = response.get("images") if isinstance(response, dict) else None
            if not images:
                raise RuntimeError(f"fal result missing images endpoint={endpoint} payload={result_payload}")

            first = images[0]
            image_url = first.get("url")
            if not image_url:
                raise RuntimeError(f"fal image missing url endpoint={endpoint} payload={result_payload}")

            return {
                "endpoint": endpoint,
                "request_id": submit_payload.get("request_id"),
                "prompt": prompt,
                "image_url": image_url,
                "width": first.get("width"),
                "height": first.get("height"),
                "content_type": first.get("content_type") or "image/jpeg",
            }
        except Exception as e:
            last_error = str(e)

    raise RuntimeError(f"All fal endpoints failed for {spec['model_code']}: {last_error}")


def ensure_private_container(bsc: BlobServiceClient) -> None:
    try:
        bsc.create_container(CONTAINER_NAME)
    except ResourceExistsError:
        pass
    cc = bsc.get_container_client(CONTAINER_NAME)
    cc.set_container_access_policy(signed_identifiers={}, public_access=None)


def delete_prefix(cc, prefix: str) -> int:
    count = 0
    for blob in cc.list_blobs(name_starts_with=prefix):
        cc.delete_blob(blob.name, delete_snapshots="include")
        count += 1
    return count


def upload_model_assets_to_azure(
    cc,
    spec: Dict[str, Any],
    generated: Dict[str, Any],
) -> GeneratedAsset:
    image_bytes, downloaded_content_type = http_bytes(generated["image_url"], timeout=240)
    content_type = generated.get("content_type") or downloaded_content_type or "image/jpeg"
    ext = ext_from_content_type(content_type, generated["image_url"])

    model_prefix = f"{CATALOG_PREFIX}{spec['model_code']}/"
    source_blob_name = f"{model_prefix}source{ext}"
    preview_blob_name = f"{model_prefix}preview{ext}"
    meta_blob_name = f"{model_prefix}meta.json"

    delete_prefix(cc, model_prefix)

    cc.upload_blob(
        name=source_blob_name,
        data=image_bytes,
        overwrite=True,
        content_type=content_type,
    )
    cc.upload_blob(
        name=preview_blob_name,
        data=image_bytes,
        overwrite=True,
        content_type=content_type,
    )

    source_ref = make_az_ref(CONTAINER_NAME, source_blob_name)
    preview_ref = make_az_ref(CONTAINER_NAME, preview_blob_name)

    meta_payload = {
        "model_code": spec["model_code"],
        "gender": spec["gender"],
        "age_band": spec["age_band"],
        "pose": spec["pose"],
        "framing": spec["framing"],
        "body_type": spec["body_type"],
        "skin_tone": spec.get("skin_tone", "medium"),
        "region_tags": spec["region_tags"],
        "style_tags": spec["style_tags"],
        "allowed_garment_kinds": spec.get("allowed_garment_kinds") or _allowed_garments_for_gender(spec["gender"]),
        "preferred_garment_kinds": spec.get("preferred_garment_kinds") or _preferred_garments_for_gender(spec["gender"]),
        "generated_at": utc_now(),
        "source": {
            "provider": "fal",
            "endpoint": generated["endpoint"],
            "request_id": generated["request_id"],
            "prompt": generated["prompt"],
        },
        "assets": [
            {
                "asset_role": "primary",
                "asset_url": source_ref,
                "blob_name": source_blob_name,
                "width": generated.get("width"),
                "height": generated.get("height"),
                "content_type": content_type,
            },
            {
                "asset_role": "preview",
                "asset_url": preview_ref,
                "blob_name": preview_blob_name,
                "width": generated.get("width"),
                "height": generated.get("height"),
                "content_type": content_type,
            },
        ],
    }

    cc.upload_blob(
        name=meta_blob_name,
        data=json.dumps(meta_payload, indent=2).encode("utf-8"),
        overwrite=True,
        content_type="application/json",
    )

    if not cc.get_blob_client(source_blob_name).exists():
        raise RuntimeError(f"Azure verify failed for {source_blob_name}")
    if not cc.get_blob_client(preview_blob_name).exists():
        raise RuntimeError(f"Azure verify failed for {preview_blob_name}")
    if not cc.get_blob_client(meta_blob_name).exists():
        raise RuntimeError(f"Azure verify failed for {meta_blob_name}")

    return GeneratedAsset(
        source_blob_name=source_blob_name,
        preview_blob_name=preview_blob_name,
        source_asset_url=source_ref,
        preview_asset_url=preview_ref,
        width=generated.get("width"),
        height=generated.get("height"),
        content_type=content_type,
        endpoint=generated["endpoint"],
        request_id=generated["request_id"],
        prompt=generated["prompt"],
    )


async def upsert_model_and_assets(
    conn: asyncpg.Connection,
    spec: Dict[str, Any],
    asset: GeneratedAsset,
) -> str:
    source_prefix = f"{CATALOG_PREFIX}{spec['model_code']}/"
    meta_json = {
        "catalog_generation": {
            "generated_at": utc_now(),
            "provider": "fal",
            "endpoint": asset.endpoint,
            "request_id": asset.request_id,
            "prompt": asset.prompt,
            "container": CONTAINER_NAME,
            "source_prefix": source_prefix,
            "verified": True,
        },
        "region_tags": spec["region_tags"],
        "style_tags": spec["style_tags"],
        "allowed_garment_kinds": spec.get("allowed_garment_kinds") or _allowed_garments_for_gender(spec["gender"]),
        "preferred_garment_kinds": spec.get("preferred_garment_kinds") or _preferred_garments_for_gender(spec["gender"]),
        "skin_tone": spec.get("skin_tone", "medium"),
    }

    async with conn.transaction():
        model_id = await conn.fetchval(
            """
            INSERT INTO public.platform_models (
                model_code,
                gender,
                age_band,
                pose,
                framing,
                body_type,
                region_tags,
                style_tags,
                quality_score,
                face_quality_score,
                body_visibility_score,
                is_active,
                source_container,
                source_prefix,
                meta_json
            ) VALUES (
                $1, $2, $3, $4, $5, $6,
                COALESCE($7::jsonb, '[]'::jsonb),
                COALESCE($8::jsonb, '[]'::jsonb),
                $9, $10, $11, true, $12, $13, COALESCE($14::jsonb, '{}'::jsonb)
            )
            ON CONFLICT (model_code)
            DO UPDATE SET
                gender = EXCLUDED.gender,
                age_band = EXCLUDED.age_band,
                pose = EXCLUDED.pose,
                framing = EXCLUDED.framing,
                body_type = EXCLUDED.body_type,
                region_tags = EXCLUDED.region_tags,
                style_tags = EXCLUDED.style_tags,
                quality_score = EXCLUDED.quality_score,
                face_quality_score = EXCLUDED.face_quality_score,
                body_visibility_score = EXCLUDED.body_visibility_score,
                is_active = true,
                source_container = EXCLUDED.source_container,
                source_prefix = EXCLUDED.source_prefix,
                meta_json = EXCLUDED.meta_json,
                updated_at = now()
            RETURNING id::text
            """,
            spec["model_code"],
            spec["gender"],
            spec["age_band"],
            spec["pose"],
            spec["framing"],
            spec["body_type"],
            json.dumps(spec["region_tags"]),
            json.dumps(spec["style_tags"]),
            0.90,
            0.85,
            1.00,
            CONTAINER_NAME,
            source_prefix,
            json.dumps(meta_json),
        )

        await conn.execute(
            """
            UPDATE public.platform_model_assets
            SET is_active = false, updated_at = now()
            WHERE platform_model_id = $1::uuid
            """,
            model_id,
        )

        await conn.execute(
            """
            INSERT INTO public.platform_model_assets (
                platform_model_id,
                asset_role,
                asset_url,
                width,
                height,
                content_type,
                sort_order,
                is_active,
                qc_json
            ) VALUES (
                $1::uuid, 'primary', $2, $3, $4, $5, 0, true, COALESCE($6::jsonb, '{}'::jsonb)
            )
            """,
            model_id,
            asset.source_asset_url,
            asset.width,
            asset.height,
            asset.content_type,
            json.dumps({
                "verified": True,
                "generated_at": utc_now(),
                "provider": "fal",
                "endpoint": asset.endpoint,
                "request_id": asset.request_id,
            }),
        )

        await conn.execute(
            """
            INSERT INTO public.platform_model_assets (
                platform_model_id,
                asset_role,
                asset_url,
                width,
                height,
                content_type,
                sort_order,
                is_active,
                qc_json
            ) VALUES (
                $1::uuid, 'preview', $2, $3, $4, $5, 10, true, COALESCE($6::jsonb, '{}'::jsonb)
            )
            """,
            model_id,
            asset.preview_asset_url,
            asset.width,
            asset.height,
            asset.content_type,
            json.dumps({
                "verified": True,
                "generated_at": utc_now(),
                "provider": "fal",
                "endpoint": asset.endpoint,
                "request_id": asset.request_id,
            }),
        )

    return model_id


def coerce_json(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


async def build_manifest(conn: asyncpg.Connection) -> Dict[str, Any]:
    rows = await conn.fetch(
        """
        SELECT
            pm.id::text AS id,
            pm.model_code,
            pm.gender,
            pm.age_band,
            pm.pose,
            pm.framing,
            pm.body_type,
            pm.region_tags,
            pm.style_tags,
            pm.quality_score,
            pm.face_quality_score,
            pm.body_visibility_score,
            pm.is_active,
            pm.source_container,
            pm.source_prefix,
            pm.meta_json,
            COALESCE(
                jsonb_agg(
                    jsonb_build_object(
                        'asset_role', pma.asset_role,
                        'asset_url', pma.asset_url,
                        'width', pma.width,
                        'height', pma.height,
                        'content_type', pma.content_type,
                        'sort_order', pma.sort_order,
                        'is_active', pma.is_active,
                        'qc_json', pma.qc_json
                    )
                    ORDER BY pma.sort_order, pma.asset_role, pma.created_at
                ) FILTER (WHERE pma.id IS NOT NULL),
                '[]'::jsonb
            ) AS assets
        FROM public.platform_models pm
        LEFT JOIN public.platform_model_assets pma
          ON pma.platform_model_id = pm.id
         AND pma.is_active = true
        WHERE pm.is_active = true
        GROUP BY
            pm.id, pm.model_code, pm.gender, pm.age_band, pm.pose, pm.framing,
            pm.body_type, pm.region_tags, pm.style_tags, pm.quality_score,
            pm.face_quality_score, pm.body_visibility_score, pm.is_active,
            pm.source_container, pm.source_prefix, pm.meta_json
        ORDER BY pm.model_code
        """
    )

    models = []
    for r in rows:
        assets = coerce_json(r["assets"]) or []
        primary_asset_url = None
        for a in assets:
            if isinstance(a, dict) and a.get("asset_role") == "primary":
                primary_asset_url = a.get("asset_url")
                break

        meta_obj = coerce_json(r["meta_json"]) or {}
        region_tags = coerce_json(r["region_tags"]) or []
        style_tags = coerce_json(r["style_tags"]) or []
        region = next((x for x in region_tags if x not in {"india", "south_asian"}), "india")

        models.append({
            "id": r["id"],
            "model_code": r["model_code"],
            "gender": r["gender"],
            "age_band": r["age_band"],
            "pose": r["pose"],
            "framing": r["framing"],
            "body_type": r["body_type"],
            "region": region,
            "region_tags": region_tags,
            "style_tags": style_tags,
            "skin_tone": meta_obj.get("skin_tone", "medium"),
            "quality_score": r["quality_score"],
            "face_quality_score": r["face_quality_score"],
            "body_visibility_score": r["body_visibility_score"],
            "is_active": r["is_active"],
            "source_container": r["source_container"],
            "source_prefix": r["source_prefix"],
            "primary_asset_url": primary_asset_url,
            "allowed_garment_kinds": meta_obj.get("allowed_garment_kinds") or _allowed_garments_for_gender(r["gender"]),
            "preferred_garment_kinds": meta_obj.get("preferred_garment_kinds") or _preferred_garments_for_gender(r["gender"]),
            "assets": assets,
            "meta": meta_obj,
            "meta_json": meta_obj,
        })

    return {
        "version": 1,
        "catalog": "platform_models",
        "container": CONTAINER_NAME,
        "prefix": CATALOG_PREFIX,
        "generated_at": utc_now(),
        "model_count": len(models),
        "models": models,
    }


def upload_manifest_and_state(cc, manifest: Dict[str, Any]) -> None:
    state = {
        "catalog": "platform_models",
        "container": CONTAINER_NAME,
        "prefix": CATALOG_PREFIX,
        "generated_at": utc_now(),
        "state": "active",
        "model_count": manifest.get("model_count", 0),
    }
    readme = (
        "DesiFaces svc-commerce platform model catalog\n"
        "Generated by generate_platform_model_catalog.py\n"
    )

    cc.upload_blob(
        name=f"{CATALOG_PREFIX}manifest.json",
        data=json.dumps(manifest, indent=2).encode("utf-8"),
        overwrite=True,
        content_type="application/json",
    )
    cc.upload_blob(
        name=f"{CATALOG_PREFIX}_catalog_state.json",
        data=json.dumps(state, indent=2).encode("utf-8"),
        overwrite=True,
        content_type="application/json",
    )
    cc.upload_blob(
        name=f"{CATALOG_PREFIX}_README.txt",
        data=readme.encode("utf-8"),
        overwrite=True,
        content_type="text/plain",
    )


async def db_counts(conn: asyncpg.Connection) -> Dict[str, int]:
    row = await conn.fetchrow(
        """
        SELECT
            (SELECT COUNT(*)::int FROM public.platform_models) AS platform_models_count,
            (SELECT COUNT(*)::int FROM public.platform_model_assets) AS platform_model_assets_count,
            (SELECT COUNT(*)::int FROM public.platform_models WHERE is_active = true) AS active_platform_models_count,
            (SELECT COUNT(*)::int FROM public.platform_model_assets WHERE is_active = true) AS active_platform_model_assets_count
        """
    )
    return dict(row)


async def main() -> None:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")
    if not AZURE_STORAGE_CONNECTION_STRING:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is missing")
    if not FAL_KEY:
        raise RuntimeError("FAL_KEY or FAL_API_KEY is missing")

    conn = await asyncpg.connect(DATABASE_URL)
    bsc = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)

    imported = []
    failed = []

    try:
        ensure_private_container(bsc)
        cc = bsc.get_container_client(CONTAINER_NAME)

        reset_summary = {}
        if WIPE_FIRST:
            deleted = delete_prefix(cc, CATALOG_PREFIX)
            async with conn.transaction():
                await conn.execute("LOCK TABLE public.platform_model_assets IN ACCESS EXCLUSIVE MODE")
                await conn.execute("LOCK TABLE public.platform_models IN ACCESS EXCLUSIVE MODE")
                await conn.execute("TRUNCATE TABLE public.platform_model_assets, public.platform_models")
            reset_summary = {"deleted_catalog_blobs": deleted}

        for spec in STARTER_SPECS:
            try:
                generated = fal_generate_one(spec)
                asset = upload_model_assets_to_azure(cc, spec, generated)
                model_id = await upsert_model_and_assets(conn, spec, asset)
                imported.append({
                    "model_code": spec["model_code"],
                    "model_id": model_id,
                    "endpoint": asset.endpoint,
                    "request_id": asset.request_id,
                    "primary_asset_url": asset.source_asset_url,
                })
            except Exception as e:
                failed.append({
                    "model_code": spec["model_code"],
                    "error": str(e),
                })

        manifest = await build_manifest(conn)
        upload_manifest_and_state(cc, manifest)
        counts = await db_counts(conn)

        print(json.dumps({
            "ok": len(imported) > 0 and len(failed) == 0,
            "container": CONTAINER_NAME,
            "prefix": CATALOG_PREFIX,
            "wipe_first": WIPE_FIRST,
            "reset_summary": reset_summary,
            "starter_target_count": len(STARTER_SPECS),
            "imported_count": len(imported),
            "failed_count": len(failed),
            "imported": imported,
            "failed": failed,
            "database": counts,
            "manifest_blob": f"{CATALOG_PREFIX}manifest.json",
        }, indent=2))

    finally:
        await conn.close()
        try:
            bsc.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())