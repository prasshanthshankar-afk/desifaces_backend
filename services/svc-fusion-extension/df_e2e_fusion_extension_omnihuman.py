#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

CORE_URL = os.getenv("CORE_URL", "http://localhost:8000").rstrip("/")
FACE_URL = os.getenv("FACE_URL", "http://localhost:8003").rstrip("/")
AUDIO_URL = os.getenv("AUDIO_URL", "http://localhost:8004").rstrip("/")
FUSION_URL = os.getenv("FUSION_URL", "http://localhost:8002").rstrip("/")
LONGFORM_URL = os.getenv("LONGFORM_URL", "http://localhost:8006").rstrip("/")

DF_EMAIL = os.getenv("DF_EMAIL", "user1@desifaces.ai")
DF_PASSWORD = os.getenv("DF_PASSWORD", "password1")

ASPECT_RATIO = os.getenv("ASPECT_RATIO", "16:9")
OUTPUT_DIR = os.getenv(
    "OUT_DIR",
    f"/tmp/df_e2e_longform_profiles_pricing_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}",
)

STORY_TITLE = os.getenv("STORY_TITLE", "DesiFaces Longform Profiles Demo")
STORY_TEXT = os.getenv(
    "STORY_TEXT",
    (
        "Welcome everyone. Today I want to share a short, clear story about why DesiFaces helps teams create culturally grounded video communication faster. "
        "We can use a clean talking-video style for training, invites, walkthroughs, and proposal updates. "
        "Or we can use cinematic video direction with animated background, richer motion, and choreographed storytelling for campaigns and premium promotions."
    ),
)
FACE_PROMPT = os.getenv(
    "FACE_PROMPT",
    "A warm, front-facing Indian presenter in cinematic realism, storytelling-ready, natural expression, high quality portrait, premium realism.",
)
FACE_GENDER = os.getenv("FACE_GENDER", "male")
FACE_COUNT = int(os.getenv("FACE_COUNT", "1"))
AUDIO_LOCALE = os.getenv("AUDIO_LOCALE", "en-US")
HEALTH_TIMEOUT_SECONDS = int(os.getenv("HEALTH_TIMEOUT_SECONDS", "180"))
JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "1800"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "5"))
DB_LOOKUP_FIRST = os.getenv("DB_LOOKUP_FIRST", "1").strip().lower() not in {"0", "false", "no"}
DB_CONTAINER = os.getenv("DB_CONTAINER", "")

FUSION_PROVIDER = os.getenv("FUSION_PROVIDER", "omnihuman_v15").strip().lower() or "omnihuman_v15"
FREE_TIER = os.getenv("FREE_TIER", "0").strip().lower() in {"1", "true", "yes", "y"}
RESOLUTION = os.getenv("RESOLUTION", "720p" if FREE_TIER else "1080p")
TURBO_MODE = os.getenv("TURBO_MODE", "0").strip().lower() in {"1", "true", "yes", "y"}

LONGFORM_PROFILE = os.getenv("LONGFORM_PROFILE", "talking_video").strip().lower() or "talking_video"
LONGFORM_PROFILES = [
    p.strip().lower()
    for p in os.getenv("LONGFORM_PROFILES", LONGFORM_PROFILE).split(",")
    if p.strip()
]

FACE_PREVIEW_PATH = "/api/face/creator/pricing/preview"
FACE_GENERATE_PATH = "/api/face/creator/generate"
FACE_STATUS_PATH = "/api/face/creator/jobs/{job_id}/status"

HEALTH_PATHS = ["/api/health", "/health"]
LONGFORM_CREATE_PATH = "/api/longform/jobs"
LONGFORM_STATUS_PATHS = [
    "/api/longform/jobs/{job_id}",
]
LONGFORM_SEGMENTS_PATHS = [
    "/api/longform/jobs/{job_id}/segments",
]
LONGFORM_VOICE_GENDER = os.getenv("LONGFORM_VOICE_GENDER", "male")
SEGMENT_SECONDS = int(os.getenv("SEGMENT_SECONDS", "8"))
MAX_SEGMENT_SECONDS = int(os.getenv("MAX_SEGMENT_SECONDS", "20"))


def print_step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def ensure_dir(path: str) -> str:
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def normalize_bearer(token_or_header: str) -> str:
    raw = (token_or_header or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw
    return f"Bearer {raw}"


def http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Any] = None,
    timeout: int = 60,
    accepted_statuses: Iterable[int] = (200, 201, 202),
) -> Tuple[int, Dict[str, str], Any]:
    body = None
    merged_headers = {"Accept": "application/json"}
    if headers:
        merged_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        merged_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=merged_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = int(getattr(resp, "status", 200))
            resp_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as ex:
        raw = ex.read().decode("utf-8", errors="replace")
        status = int(ex.code)
        resp_headers = dict(ex.headers.items())
        if status not in set(int(x) for x in accepted_statuses):
            raise RuntimeError(f"{method} {url} failed [{status}]: {raw}") from ex
    except Exception as ex:
        raise RuntimeError(f"{method} {url} failed: {ex}") from ex
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except Exception:
        parsed = {"raw": raw}
    return status, resp_headers, parsed


def service_healthy(base_url: str) -> bool:
    for path in HEALTH_PATHS:
        try:
            status, _, _ = http_json("GET", f"{base_url}{path}", timeout=8, accepted_statuses=(200,))
            if status == 200:
                return True
        except Exception:
            continue
    return False


def wait_for_service_health(name: str, base_url: str, timeout_seconds: int) -> None:
    print_step(f"Waiting for health: {name} {base_url}")
    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if service_healthy(base_url):
            return
        time.sleep(3)
    raise RuntimeError(f"Timed out waiting for health at {base_url}")


def first_present(values: Iterable[Any]) -> Optional[str]:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def recursive_collect(obj: Any, keys: set[str]) -> List[Any]:
    out: List[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) in keys:
                out.append(v)
            out.extend(recursive_collect(v, keys))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(recursive_collect(item, keys))
    return out


def recursive_collect_first_str(obj: Any, keys: set[str]) -> Optional[str]:
    for value in recursive_collect(obj, keys):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def recursive_collect_first_dict(obj: Any, keys: set[str]) -> Optional[Dict[str, Any]]:
    for value in recursive_collect(obj, keys):
        if isinstance(value, dict):
            return value
    return None


def jwt_sub(token_or_header: str) -> Optional[str]:
    raw = (token_or_header or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        parsed = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None
    sub = parsed.get("sub") or parsed.get("user_id")
    return str(sub).strip() if sub else None


def common_headers(access_token: str, user_id: str) -> Dict[str, str]:
    return {
        "Authorization": normalize_bearer(access_token),
        "X-User-Id": str(user_id),
    }


def get_job_with_reauth(job_base_url: str, path_template: str, current_auth: Dict[str, Any], job_id: str) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    url = f"{job_base_url}{path_template.format(job_id=urllib.parse.quote(job_id))}"
    try:
        _, _, payload = http_json("GET", url, headers=common_headers(current_auth["access_token"], current_auth["user_id"]), timeout=60)
        return current_auth, payload if isinstance(payload, dict) else {"raw": payload}, url
    except Exception as ex:
        msg = str(ex)
        if "[401]" not in msg and "401 Unauthorized" not in msg:
            raise
        refreshed = login()
        _, _, payload = http_json("GET", url, headers=common_headers(refreshed["access_token"], refreshed["user_id"]), timeout=60)
        return refreshed, payload if isinstance(payload, dict) else {"raw": payload}, url


def login() -> Dict[str, Any]:
    _, _, payload = http_json(
        "POST",
        f"{CORE_URL}/api/auth/login",
        payload={"email": DF_EMAIL, "password": DF_PASSWORD},
        timeout=30,
    )
    token = normalize_bearer(str(payload.get("access_token") or payload.get("token") or ""))
    if not token:
        raise RuntimeError(f"Login succeeded but no access token found: {payload}")
    user_id = first_present([payload.get("user_id"), payload.get("id"), jwt_sub(token)])
    if not user_id:
        raise RuntimeError("Could not resolve user_id from login response/JWT")
    return {"access_token": token, "user_id": user_id, "raw": payload}


def run(cmd: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            + " ".join(cmd)
            + f"\n\nSTDOUT:\n{p.stdout}\n\nSTDERR:\n{p.stderr}"
        )
    return p


def discover_db_container() -> Optional[str]:
    if DB_CONTAINER:
        return DB_CONTAINER
    try:
        out = run(["docker", "ps", "--format", "{{.Names}}"]).stdout or ""
    except Exception:
        return None
    names = [x.strip() for x in out.splitlines() if x.strip()]
    for name in names:
        if name == "desifaces-db":
            return name
    for name in names:
        if "db" in name and "desifaces" in name:
            return name
    return None


def psql_json_lines(container_name: str, sql: str) -> List[Dict[str, Any]]:
    shell = f'''psql -U "$POSTGRES_USER" -d "${{POSTGRES_DB:-postgres}}" -At <<'SQL'
{sql}
SQL'''
    p = run(["docker", "exec", "-i", container_name, "bash", "-lc", shell], check=True)
    rows: List[Dict[str, Any]] = []
    for line in [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]:
        try:
            rows.append(json.loads(line))
        except Exception:
            rows.append({"raw": line})
    return rows


def table_columns(container_name: str, table_name: str) -> List[str]:
    sql = f"""
    select column_name
    from information_schema.columns
    where table_schema = 'public'
      and table_name = '{table_name}'
    order by ordinal_position;
    """
    shell = f'''psql -U "$POSTGRES_USER" -d "${{POSTGRES_DB:-postgres}}" -At <<'SQL'
{sql}
SQL'''
    try:
        p = run(["docker", "exec", "-i", container_name, "bash", "-lc", shell], check=True)
    except Exception:
        return []
    return [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]


def query_latest_face_jobs(container_name: str, user_id: str, limit: int = 25) -> List[Dict[str, Any]]:
    sql = f"""
    select json_build_object(
      'id', id,
      'studio_type', studio_type,
      'status', status,
      'created_at', created_at,
      'payload_json', coalesce(payload_json, '{{}}'::jsonb),
      'meta_json', coalesce(meta_json, '{{}}'::jsonb)
    )::text
    from public.studio_jobs
    where user_id = '{user_id}'
      and lower(studio_type) in ('face')
      and lower(status) in ('succeeded','completed','success')
    order by created_at desc
    limit {int(limit)};
    """
    return psql_json_lines(container_name, sql)


def extract_face_asset_from_jobs(rows: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    id_keys = {
        "media_asset_id",
        "face_artifact_id",
        "artifact_id",
        "image_artifact_id",
        "selected_face_artifact_id",
    }
    url_keys = {"face_image_url", "image_url", "preview_url", "signed_url", "url", "storage_ref", "sas_url", "blob_url"}
    for row in rows:
        artifact_id = recursive_collect_first_str(row, id_keys)
        image_url = recursive_collect_first_str(row, url_keys)
        if artifact_id or image_url:
            return {
                "source": "database",
                "source_job_id": str(row.get("id") or ""),
                "face_artifact_id": artifact_id,
                "face_image_url": image_url,
            }
    return {"source": None, "source_job_id": None, "face_artifact_id": None, "face_image_url": None}


def maybe_lookup_db_face(user_id: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    meta: Dict[str, Any] = {"enabled": DB_LOOKUP_FIRST}
    if not DB_LOOKUP_FIRST:
        return None, meta
    container_name = discover_db_container()
    meta["container_name"] = container_name
    if not container_name:
        meta["note"] = "db container not found"
        return None, meta
    try:
        face_rows = query_latest_face_jobs(container_name, user_id, limit=25)
        meta["face_row_count"] = len(face_rows)
        return extract_face_asset_from_jobs(face_rows), meta
    except Exception as ex:
        meta["note"] = f"db lookup failed: {ex}"
        return None, meta


def build_face_preview_payload() -> Dict[str, Any]:
    return {
        "mode": "text-to-image",
        "count": max(1, FACE_COUNT),
        "studio_input": {
            "user_prompt": FACE_PROMPT,
            "gender": FACE_GENDER,
        },
    }


def preview_face(access_token: str, user_id: str) -> Dict[str, Any]:
    _, _, payload = http_json(
        "POST",
        f"{FACE_URL}{FACE_PREVIEW_PATH}",
        headers=common_headers(access_token, user_id),
        payload=build_face_preview_payload(),
        timeout=60,
    )
    return payload if isinstance(payload, dict) else {}


def create_face_job(access_token: str, user_id: str, preview_resp: Dict[str, Any]) -> Dict[str, Any]:
    payload = build_face_preview_payload()
    quote_id = first_present([preview_resp.get("quote_id"), recursive_collect_first_str(preview_resp, {"quote_id"})])
    preview_fp = first_present([preview_resp.get("preview_fingerprint"), recursive_collect_first_str(preview_resp, {"preview_fingerprint"})])
    if quote_id and preview_fp:
        payload["pricing_confirmation"] = {"quote_id": quote_id, "preview_fingerprint": preview_fp}
    _, _, resp = http_json(
        "POST",
        f"{FACE_URL}{FACE_GENERATE_PATH}",
        headers=common_headers(access_token, user_id),
        payload=payload,
        timeout=90,
    )
    if not isinstance(resp, dict):
        raise RuntimeError(f"Unexpected face create response: {resp}")
    return resp


def poll_face_status(access_token: str, user_id: str, job_id: str) -> Dict[str, Any]:
    deadline = time.time() + float(JOB_TIMEOUT_SECONDS)
    last_payload: Dict[str, Any] = {}
    while time.time() < deadline:
        _, _, payload = http_json(
            "GET",
            f"{FACE_URL}{FACE_STATUS_PATH.format(job_id=urllib.parse.quote(job_id))}",
            headers=common_headers(access_token, user_id),
            timeout=60,
        )
        if isinstance(payload, dict):
            last_payload = payload
            status = str(payload.get("status") or payload.get("stage") or "").lower()
            if status in {"succeeded", "success", "completed", "done"}:
                return payload
            if status in {"failed", "error", "cancelled", "canceled"}:
                raise RuntimeError(f"Face job failed: {payload}")
        time.sleep(POLL_SECONDS)
    raise RuntimeError(f"Face job timed out. last_payload={last_payload}")


def extract_face_asset_from_status(payload: Dict[str, Any], job_id: str) -> Dict[str, Optional[str]]:
    artifact_id = recursive_collect_first_str(
        payload,
        {"media_asset_id", "face_artifact_id", "artifact_id", "image_artifact_id", "selected_face_artifact_id"},
    )
    image_url = recursive_collect_first_str(payload, {"face_image_url", "image_url", "preview_url", "signed_url", "url", "sas_url", "blob_url", "storage_ref"})
    return {
        "source": "generated",
        "source_job_id": job_id,
        "face_artifact_id": artifact_id,
        "face_image_url": image_url,
    }


def ensure_face_asset(access_token: str, user_id: str, existing: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    meta: Dict[str, Any] = {}
    if existing and existing.get("face_artifact_id"):
        meta["strategy"] = "database"
        return existing, meta
    meta["strategy"] = "generate"
    preview_resp = preview_face(access_token, user_id)
    meta["preview_response"] = preview_resp
    created = create_face_job(access_token, user_id, preview_resp)
    meta["create_response"] = created
    job_id = first_present([created.get("job_id"), created.get("id"), recursive_collect_first_str(created, {"job_id", "id"})])
    if not job_id:
        raise RuntimeError(f"Face create response missing job_id: {created}")
    final_status = poll_face_status(access_token, user_id, job_id)
    meta["status_response"] = final_status
    asset = extract_face_asset_from_status(final_status, job_id)
    if not asset.get("face_artifact_id"):
        raise RuntimeError(
            "Face job succeeded but no stable face_artifact_id/media_asset_id was found. "
            "Do not fall back to face_image_url for longform create."
        )
    return asset, meta


def split_story_text(text: str, *, target_parts: int = 3) -> List[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    if not parts:
        return [text.strip()] * target_parts
    if len(parts) >= target_parts:
        return parts[:target_parts]
    while len(parts) < target_parts:
        parts.append(parts[-1])
    return parts


def profile_env(profile: str, suffix: str, default: str) -> str:
    key = f"{profile.upper()}_{suffix}"
    return os.getenv(key, os.getenv(suffix, default))


def build_presenter_prompt(direction: str, profile: str) -> str:
    motion_phrase = (
        "minimal hand movement, calm delivery, subtle head movement"
        if profile == "talking_video"
        else "visible hand gestures, expressive eyes, subtle torso movement, premium cinematic realism"
    )
    return (
        "Full-screen presenter-led storytelling shot. "
        f"The person speaks naturally to camera with {motion_phrase}. "
        "Community-driven authenticity, premium realism, not corporate avatar aesthetics. "
        f"Direction: {direction}"
    )


def camera_config_for_profile(profile: str) -> Dict[str, str]:
    if profile == "cinematic_video_direction":
        return {
            "camera_angle": profile_env(profile, "CAMERA_ANGLE", "eye_level"),
            "camera_framing": profile_env(profile, "CAMERA_FRAMING", "medium"),
            "camera_motion_style": profile_env(profile, "CAMERA_MOTION_STYLE", "gentle_push_in"),
        }
    return {
        "camera_angle": profile_env(profile, "CAMERA_ANGLE", "eye_level"),
        "camera_framing": profile_env(profile, "CAMERA_FRAMING", "medium_close_up"),
        "camera_motion_style": profile_env(profile, "CAMERA_MOTION_STYLE", "static"),
    }




def aspect_ratio_for_profile(profile: str) -> str:
    p = (profile or "").strip().lower()
    if p == "cinematic_video_direction":
        return profile_env(profile, "ASPECT_RATIO", "16:9")
    return profile_env(profile, "ASPECT_RATIO", os.getenv("ASPECT_RATIO", "16:9"))

def build_directed_segments(face: Dict[str, Optional[str]], profile: str, camera: Dict[str, str]) -> List[Dict[str, Any]]:
    parts = split_story_text(STORY_TEXT, target_parts=3)
    aspect_ratio = aspect_ratio_for_profile(profile)
    face_artifact_id = face.get("face_artifact_id")
    face_image_url = face.get("face_image_url")

    common_assets = {
        "face_artifact_id": face_artifact_id,
        "face_image_url": face_image_url,
    }

    directions = [
        "Clear opening with warmth and confidence.",
        "Middle explanation with energy and conviction.",
        "Closing with clarity and persuasive finish.",
    ]

    bg_providers = ["luma", "kling", "luma"]
    bg_prompts = [
        "Realistic premium cinematic background plate with gentle environmental motion, believable ambience, no warped faces.",
        "Dynamic cinematic background plate with elegant camera drift, moving lights, crowd energy, premium motion.",
        "Premium cinematic environment with subtle realistic background motion and visual depth.",
    ]

    segments: List[Dict[str, Any]] = []
    for idx, part in enumerate(parts):
        shot_type = "presenter_anchor" if profile == "talking_video" else "presenter_with_motion_bg"
        provider_options: Dict[str, Any] = {
            "prompt": build_presenter_prompt(directions[idx], profile),
            "render_kind": shot_type,
            "resolution": RESOLUTION,
            "turbo_mode": TURBO_MODE,
            "camera_angle": camera["camera_angle"],
            "camera_framing": camera["camera_framing"],
            "camera_motion_style": camera["camera_motion_style"],
        }
        seg: Dict[str, Any] = {
            "segment_index": idx,
            "shot_type": shot_type,
            "render_route": "fusion",
            "duration_sec": SEGMENT_SECONDS,
            "aspect_ratio": aspect_ratio,
            "output_profile": RESOLUTION,
            "camera_angle": camera["camera_angle"],
            "camera_framing": camera["camera_framing"],
            "camera_motion_style": camera["camera_motion_style"],
            "script": {
                "spoken_text": part,
                "onscreen_text": [part],
            },
            "resolved_assets": dict(common_assets),
            "provider": FUSION_PROVIDER,
            "provider_options": provider_options,
            "tags": {
                "longform_profile": profile,
                "camera_angle": camera["camera_angle"],
                "camera_framing": camera["camera_framing"],
                "camera_motion_style": camera["camera_motion_style"],
            },
        }
        if profile == "cinematic_video_direction":
            provider_options.update(
                {
                    "background_provider": bg_providers[idx],
                    "background_prompt": bg_prompts[idx],
                    "motion_strength": "medium",
                }
            )
            seg["background_provider"] = bg_providers[idx]
        segments.append(seg)
    return segments


def extract_pricing(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("pricing"), dict):
        return payload.get("pricing") or {}
    found = recursive_collect_first_dict(payload, {"pricing"})
    return found or {}


def extract_pricing_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("pricing_summary"), dict):
        return payload.get("pricing_summary") or {}
    found = recursive_collect_first_dict(payload, {"pricing_summary"})
    return found or {}


def build_longform_payload(face: Dict[str, Optional[str]], profile: str) -> Dict[str, Any]:
    camera = camera_config_for_profile(profile)
    aspect_ratio = aspect_ratio_for_profile(profile)
    segments = build_directed_segments(face, profile, camera)
    directed_plan = {
        "timeline": {"segments": segments, "shots": segments},
        "segments_by_index": {str(item["segment_index"]): item for item in segments},
        "shots": segments,
    }

    return {
        "title": STORY_TITLE,
        "mode": "directed",
        "longform_profile": profile,
        "scenario_type": "festive_campaign" if profile == "cinematic_video_direction" else "product_explainer",
        "goal": (
            "Validate premium cinematic video direction with presenter and animated background."
            if profile == "cinematic_video_direction"
            else "Validate presenter-first talking video flow with clean delivery and minimal motion."
        ),
        "script_text": STORY_TEXT,
        "voice_cfg": {"locale": AUDIO_LOCALE},
        "voice_gender_mode": "manual",
        "voice_gender": LONGFORM_VOICE_GENDER,
        "aspect_ratio": aspect_ratio,
        "segment_seconds": SEGMENT_SECONDS,
        "max_segment_seconds": MAX_SEGMENT_SECONDS,
        "face_artifact_id": face["face_artifact_id"],
        "camera_angle": camera["camera_angle"],
        "camera_framing": camera["camera_framing"],
        "camera_motion_style": camera["camera_motion_style"],
        "tags": {
            "source": "longform_profiles_pricing_e2e",
            "story_title": STORY_TITLE,
            "face_artifact_id": face["face_artifact_id"],
            "face_image_url": face.get("face_image_url"),
            "intent": {"mode": "directed", "scenario_type": "festive_campaign"},
            "scenario": {"scenario_type": "festive_campaign" if profile == "cinematic_video_direction" else "product_explainer"},
            "default_presenter_provider": FUSION_PROVIDER,
            "resolution": RESOLUTION,
            "longform_profile": profile,
            "pricing_variant_code": "CINEMATIC_VIDEO_DIRECTION" if profile == "cinematic_video_direction" else "TALKING_VIDEO",
            "pricing_leaf_sku_code": "LONGFORM_CINEMATIC_MIN" if profile == "cinematic_video_direction" else "LONGFORM_TALK_MIN",
            "camera_angle": camera["camera_angle"],
            "camera_framing": camera["camera_framing"],
            "camera_motion_style": camera["camera_motion_style"],
            "directed_plan": directed_plan,
            "segments_by_index": directed_plan["segments_by_index"],
        },
    }


def create_longform_job(access_token: str, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _, _, resp = http_json(
        "POST",
        f"{LONGFORM_URL}{LONGFORM_CREATE_PATH}",
        headers=common_headers(access_token, user_id),
        payload=payload,
        timeout=90,
    )
    if not isinstance(resp, dict):
        raise RuntimeError(f"Unexpected longform create response: {resp}")
    return resp


def poll_longform_job(auth: Dict[str, Any], job_id: str) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    deadline = time.time() + float(JOB_TIMEOUT_SECONDS)
    last_payload: Dict[str, Any] = {}
    last_exception: Optional[str] = None
    current_auth = dict(auth)

    while time.time() < deadline:
        for path in LONGFORM_STATUS_PATHS:
            try:
                current_auth, payload, url = get_job_with_reauth(LONGFORM_URL, path, current_auth, job_id)
                if not isinstance(payload, dict):
                    continue
                last_payload = payload
                status = str(payload.get("status") or payload.get("stage") or "").lower()
                if status in {"succeeded", "success", "completed", "done"}:
                    return current_auth, url, payload
                if status in {"failed", "error", "cancelled", "canceled"}:
                    raise RuntimeError(f"Longform job failed: {payload}")
            except Exception as ex:
                last_exception = str(ex)
                if "failed:" in str(ex).lower():
                    raise
        time.sleep(POLL_SECONDS)

    raise RuntimeError(f"Timed out polling longform job {job_id}. last_payload={last_payload} last_exception={last_exception}")


def fetch_segments(auth: Dict[str, Any], job_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    errors: List[str] = []
    current_auth = dict(auth)
    for path in LONGFORM_SEGMENTS_PATHS:
        try:
            current_auth, payload, _ = get_job_with_reauth(LONGFORM_URL, path, current_auth, job_id)
            return current_auth, (payload if isinstance(payload, dict) else {"raw": payload})
        except Exception as ex:
            errors.append(str(ex))
    return current_auth, {"errors": errors}


def extract_final_video_url(payload: Dict[str, Any]) -> Optional[str]:
    return first_present([
        payload.get("video_url"),
        payload.get("final_video_url"),
        payload.get("output_url"),
        payload.get("artifact_url"),
        recursive_collect_first_str(payload, {"video_url", "final_video_url", "output_url", "artifact_url", "url"}),
    ])


def capture_account_snapshot(container_name: Optional[str], user_id: str) -> Dict[str, Any]:
    if not container_name:
        return {"available": False, "note": "db container not found"}
    cols = set(table_columns(container_name, "pricing_credit_accounts"))
    wanted = [c for c in ["user_id", "balance_credits", "reserved_credits", "updated_at"] if c in cols]
    if not wanted:
        return {"available": False, "note": "pricing_credit_accounts columns unavailable"}
    sql = f"""
    select json_build_object({", ".join([f"'{c}', {c}" for c in wanted])})::text
    from pricing_credit_accounts
    where user_id = '{user_id}'
    limit 1;
    """
    rows = psql_json_lines(container_name, sql)
    return rows[0] if rows else {"available": False, "note": "no pricing account row"}


def capture_reservations_for_job(container_name: Optional[str], job_id: str) -> Dict[str, Any]:
    if not container_name:
        return {"available": False, "note": "db container not found"}
    cols = set(table_columns(container_name, "pricing_credit_reservations"))
    wanted = [
        c for c in [
            "id", "user_id", "external_ref_type", "external_ref_id", "status",
            "variant_code", "sku_code", "reserved_credits", "charged_credits",
            "charged_money", "currency", "billing_account_id", "settlement_mode",
            "created_at", "updated_at"
        ] if c in cols
    ]
    if not wanted:
        return {"available": False, "note": "pricing_credit_reservations columns unavailable"}
    order_col = "created_at" if "created_at" in cols else wanted[0]
    sql = f"""
    select json_build_object({", ".join([f"'{c}', {c}" for c in wanted])})::text
    from pricing_credit_reservations
    where external_ref_id = '{job_id}'
    order by {order_col} desc
    limit 5;
    """
    rows = psql_json_lines(container_name, sql)
    return {"rows": rows}


def capture_ledger_for_job(container_name: Optional[str], job_id: str) -> Dict[str, Any]:
    if not container_name:
        return {"available": False, "note": "db container not found"}
    cols = set(table_columns(container_name, "pricing_credit_ledger_events"))
    if not cols:
        return {"available": False, "note": "pricing_credit_ledger_events columns unavailable"}
    where_pred = []
    if "external_ref_id" in cols:
        where_pred.append(f"external_ref_id = '{job_id}'")
    if "reference_id" in cols:
        where_pred.append(f"reference_id = '{job_id}'")
    if not where_pred:
        return {"available": False, "note": "no job reference column on pricing_credit_ledger_events"}
    wanted = [
        c for c in [
            "id", "user_id", "external_ref_id", "reference_id", "event_type",
            "sku_code", "credits_delta", "money_amount", "currency",
            "billing_account_id", "created_at"
        ] if c in cols
    ]
    order_col = "created_at" if "created_at" in cols else wanted[0]
    sql = f"""
    select json_build_object({", ".join([f"'{c}', {c}" for c in wanted])})::text
    from pricing_credit_ledger_events
    where {' or '.join(where_pred)}
    order by {order_col} desc
    limit 10;
    """
    rows = psql_json_lines(container_name, sql)
    return {"rows": rows}


def run_profile_case(auth: Dict[str, Any], face_asset: Dict[str, Any], container_name: Optional[str], profile: str, case_dir: str) -> Dict[str, Any]:
    ensure_dir(case_dir)
    case_summary: Dict[str, Any] = {
        "profile": profile,
        "started_at": dt.datetime.utcnow().isoformat() + "Z",
        "aspect_ratio": aspect_ratio,
        "resolution": RESOLUTION,
        "camera": camera_config_for_profile(profile),
    }

    before_account = capture_account_snapshot(container_name, auth["user_id"])
    case_summary["pricing_before_account"] = before_account
    write_json(os.path.join(case_dir, "pricing_before_account.json"), before_account)

    payload = build_longform_payload(face_asset, profile)
    write_json(os.path.join(case_dir, "longform_create_payload.json"), payload)

    create_resp = create_longform_job(auth["access_token"], auth["user_id"], payload)
    write_json(os.path.join(case_dir, "longform_create_response.json"), create_resp)
    case_summary["create_pricing"] = extract_pricing(create_resp)
    case_summary["create_pricing_summary"] = extract_pricing_summary(create_resp)

    job_id = first_present([create_resp.get("job_id"), create_resp.get("id"), recursive_collect_first_str(create_resp, {"job_id", "id"})])
    if not job_id:
        raise RuntimeError(f"Longform create response missing job_id: {create_resp}")
    case_summary["longform_job_id"] = job_id

    reservation_after_create = capture_reservations_for_job(container_name, job_id)
    case_summary["pricing_after_create_reservations"] = reservation_after_create
    write_json(os.path.join(case_dir, "pricing_after_create_reservations.json"), reservation_after_create)

    auth, status_url, final_status = poll_longform_job(auth, job_id)
    write_json(os.path.join(case_dir, "longform_status.json"), final_status)
    case_summary["longform_status_url"] = status_url
    case_summary["final_status"] = final_status.get("status") or final_status.get("stage")
    case_summary["final_video_url"] = extract_final_video_url(final_status)
    case_summary["final_pricing"] = extract_pricing(final_status)
    case_summary["final_pricing_summary"] = extract_pricing_summary(final_status)

    auth, segments_payload = fetch_segments(auth, job_id)
    write_json(os.path.join(case_dir, "longform_segments.json"), segments_payload)

    after_account = capture_account_snapshot(container_name, auth["user_id"])
    reservations_after = capture_reservations_for_job(container_name, job_id)
    ledger_after = capture_ledger_for_job(container_name, job_id)

    case_summary["pricing_after_account"] = after_account
    case_summary["pricing_after_reservations"] = reservations_after
    case_summary["pricing_after_ledger"] = ledger_after
    case_summary["completed_at"] = dt.datetime.utcnow().isoformat() + "Z"

    write_json(os.path.join(case_dir, "pricing_after_account.json"), after_account)
    write_json(os.path.join(case_dir, "pricing_after_reservations.json"), reservations_after)
    write_json(os.path.join(case_dir, "pricing_after_ledger.json"), ledger_after)
    write_json(os.path.join(case_dir, "summary.json"), case_summary)
    return case_summary


def main() -> int:
    ensure_dir(OUTPUT_DIR)
    summary: Dict[str, Any] = {
        "out_dir": OUTPUT_DIR,
        "started_at": dt.datetime.utcnow().isoformat() + "Z",
        "core_url": CORE_URL,
        "face_url": FACE_URL,
        "audio_url": AUDIO_URL,
        "fusion_url": FUSION_URL,
        "longform_url": LONGFORM_URL,
        "df_email": DF_EMAIL,
        "fusion_provider": FUSION_PROVIDER,
        "resolution": RESOLUTION,
        "turbo_mode": TURBO_MODE,
        "profiles_requested": LONGFORM_PROFILES,
    }

    wait_for_service_health("svc-core", CORE_URL, HEALTH_TIMEOUT_SECONDS)
    wait_for_service_health("svc-face", FACE_URL, HEALTH_TIMEOUT_SECONDS)
    wait_for_service_health("svc-audio", AUDIO_URL, HEALTH_TIMEOUT_SECONDS)
    wait_for_service_health("svc-fusion", FUSION_URL, HEALTH_TIMEOUT_SECONDS)
    wait_for_service_health("svc-fusion-extension", LONGFORM_URL, HEALTH_TIMEOUT_SECONDS)

    print_step("Login")
    auth = login()
    write_json(os.path.join(OUTPUT_DIR, "auth.json"), auth["raw"])
    summary["user_id"] = auth["user_id"]

    print_step("Resolve face asset")
    db_face, db_meta = maybe_lookup_db_face(auth["user_id"])
    summary["db_lookup"] = db_meta
    face_asset, face_meta = ensure_face_asset(auth["access_token"], auth["user_id"], db_face)
    summary["resolved_face_asset"] = face_asset
    summary["face_flow"] = face_meta
    write_json(os.path.join(OUTPUT_DIR, "face_flow.json"), face_meta)

    container_name = discover_db_container()
    summary["db_container"] = container_name

    case_results: List[Dict[str, Any]] = []
    for profile in LONGFORM_PROFILES:
        if profile not in {"talking_video", "cinematic_video_direction"}:
            raise RuntimeError(f"Unsupported profile: {profile}")
        print_step(f"Run longform profile: {profile}")
        case_dir = os.path.join(OUTPUT_DIR, profile)
        case_results.append(run_profile_case(auth, face_asset, container_name, profile, case_dir))

    summary["cases"] = case_results
    summary["completed_at"] = dt.datetime.utcnow().isoformat() + "Z"
    write_json(os.path.join(OUTPUT_DIR, "summary.json"), summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as ex:
        ensure_dir(OUTPUT_DIR)
        failure = {
            "out_dir": OUTPUT_DIR,
            "error": str(ex),
            "completed_at": dt.datetime.utcnow().isoformat() + "Z",
        }
        write_json(os.path.join(OUTPUT_DIR, "summary.json"), failure)
        print(json.dumps(failure, indent=2, ensure_ascii=False), file=sys.stderr)
        raise
