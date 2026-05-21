#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import pathlib
import re
import shlex
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
LONGFORM_URL = os.getenv("LONGFORM_URL", "http://localhost:8006").rstrip("/")

DF_EMAIL = os.getenv("DF_EMAIL", "user1@desifaces.ai")
DF_PASSWORD = os.getenv("DF_PASSWORD", "password1")

ASPECT_RATIO = os.getenv("ASPECT_RATIO", "16:9")
OUTPUT_DIR = os.getenv(
    "OUT_DIR",
    f"/tmp/df_e2e_fusion_extension_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}",
)

STORY_TITLE = os.getenv("STORY_TITLE", "DesiFaces Storytelling Demo")
STORY_TEXT = os.getenv(
    "STORY_TEXT",
    (
        "As the diyas glow and the mantras echo, we welcome the blessings of Goddess Lakshmi. "
        "Diwali is more than a festival; it is the sacred triumph of knowledge over ignorance "
        "and light over darkness. Wishing you a peaceful, prosperous, and very Happy Diwali."
    ),
)
FACE_PROMPT = os.getenv(
    "FACE_PROMPT",
    "A warm, front-facing Indian presenter portrait, premium cinematic realism, natural expression, clean background, storytelling-ready, high-quality studio lighting.",
)
FACE_GENDER = os.getenv("FACE_GENDER", "female")
FACE_COUNT = int(os.getenv("FACE_COUNT", "1"))
AUDIO_LOCALE = os.getenv("AUDIO_LOCALE", "en-US")
AUDIO_OUTPUT_FORMAT = os.getenv("AUDIO_OUTPUT_FORMAT", "mp3")
AUDIO_VOICE = os.getenv("AUDIO_VOICE", "")

HEALTH_TIMEOUT_SECONDS = int(os.getenv("HEALTH_TIMEOUT_SECONDS", "180"))
JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "1800"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "5"))
DB_LOOKUP_FIRST = os.getenv("DB_LOOKUP_FIRST", "1").strip().lower() not in {"0", "false", "no"}
DB_CONTAINER = os.getenv("DB_CONTAINER", "")

FACE_PREVIEW_PATH = "/api/face/creator/pricing/preview"
FACE_GENERATE_PATH = "/api/face/creator/generate"
FACE_STATUS_PATH = "/api/face/creator/jobs/{job_id}/status"

AUDIO_PREVIEW_PATH = "/api/audio/tts/pricing/preview"
AUDIO_GENERATE_PATH = "/api/audio/tts"
AUDIO_STATUS_PATH = "/api/audio/jobs/{job_id}/status"

LONGFORM_HEALTH_PATHS = ["/api/health", "/health"]
LONGFORM_CREATE_PATHS = ["/api/longform/jobs"]
LONGFORM_STATUS_PATHS = [
    "/api/longform/jobs/{job_id}/status",
    "/api/longform/jobs/{job_id}",
    "/api/longform/jobs/{job_id}/view",
]
LONGFORM_SCENARIO_TYPE = os.getenv("LONGFORM_SCENARIO_TYPE", "festive_campaign")
LONGFORM_INTENT_GOAL = os.getenv(
    "LONGFORM_INTENT_GOAL",
    "Create a premium cinematic festive storytelling video with expressive presenter beats, narrated support visuals, and a polished closing montage.",
)


def print_step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def ensure_dir(path: str) -> str:
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def run(cmd: Sequence[str], *, check: bool = True, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    p = subprocess.run(
        list(cmd),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and p.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            f"{' '.join(shlex.quote(x) for x in cmd)}\n\n"
            f"STDOUT:\n{p.stdout or ''}\n\n"
            f"STDERR:\n{p.stderr or ''}"
        )
    return p


def normalize_bearer(token_or_header: str) -> str:
    raw = (token_or_header or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw
    return f"Bearer {raw}"


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


def split_story_text(text: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    if len(parts) >= 3:
        return parts[:3]
    if len(parts) == 2:
        return [parts[0], parts[1], parts[1]]
    if len(parts) == 1:
        s = parts[0]
        mid = max(1, len(s) // 2)
        left = s[:mid].strip() or s
        right = s[mid:].strip() or s
        return [left, right, s]
    stripped = text.strip()
    return [stripped, stripped, stripped]


def common_headers(access_token: str, user_id: str) -> Dict[str, str]:
    return {
        "Authorization": normalize_bearer(access_token),
        "X-User-Id": str(user_id),
    }


def service_healthy(base_url: str) -> bool:
    for path in LONGFORM_HEALTH_PATHS:
        try:
            status, _, _ = http_json("GET", f"{base_url}{path}", timeout=8, accepted_statuses=(200,))
            if status == 200:
                return True
        except Exception:
            continue
    return False


def wait_for_service_health(base_url: str, timeout_seconds: int) -> None:
    print_step(f"Waiting for health: {base_url}")
    deadline = time.time() + float(timeout_seconds)
    while time.time() < deadline:
        if service_healthy(base_url):
            return
        time.sleep(3)
    raise RuntimeError(f"Timed out waiting for health at {base_url}")


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


def discover_db_container() -> Optional[str]:
    if DB_CONTAINER:
        return DB_CONTAINER
    try:
        out = run(["docker", "ps", "--format", "{{.Names}}"], check=True).stdout or ""
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
    shell = f"""
psql -U "$POSTGRES_USER" -d "${{POSTGRES_DB:-postgres}}" -At -c {shlex.quote(sql)}
"""
    p = run(["docker", "exec", "-i", container_name, "bash", "-lc", shell], check=True)
    rows: List[Dict[str, Any]] = []
    for line in [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]:
        try:
            rows.append(json.loads(line))
        except Exception:
            rows.append({"raw": line})
    return rows


def query_latest_studio_jobs(container_name: str, user_id: str, studio_types: Sequence[str], limit: int = 20) -> List[Dict[str, Any]]:
    studio_types_sql = ", ".join("'" + x.replace("'", "''") + "'" for x in studio_types)
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
      and lower(studio_type) in ({studio_types_sql})
      and lower(status) in ('succeeded','completed','success')
    order by created_at desc
    limit {int(limit)}
    """
    return psql_json_lines(container_name, sql)


def extract_face_asset_from_jobs(rows: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    id_keys = {"face_artifact_id", "artifact_id", "image_artifact_id", "selected_face_artifact_id"}
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


def extract_audio_asset_from_jobs(rows: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    id_keys = {"audio_artifact_id", "voice_audio_artifact_id", "artifact_id"}
    url_keys = {"audio_url", "voice_audio_url", "url", "signed_url", "preview_url", "sas_url", "blob_url"}
    for row in rows:
        artifact_id = recursive_collect_first_str(row, id_keys)
        audio_url = recursive_collect_first_str(row, url_keys)
        if artifact_id or audio_url:
            return {
                "source": "database",
                "source_job_id": str(row.get("id") or ""),
                "audio_artifact_id": artifact_id,
                "audio_url": audio_url,
            }
    return {"source": None, "source_job_id": None, "audio_artifact_id": None, "audio_url": None}


def maybe_lookup_db_assets(user_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
    meta: Dict[str, Any] = {"enabled": DB_LOOKUP_FIRST}
    if not DB_LOOKUP_FIRST:
        return None, None, meta
    container_name = discover_db_container()
    meta["container_name"] = container_name
    if not container_name:
        meta["note"] = "db container not found"
        return None, None, meta
    try:
        face_rows = query_latest_studio_jobs(container_name, user_id, ["face"], limit=25)
        audio_rows = query_latest_studio_jobs(container_name, user_id, ["audio", "tts"], limit=25)
        meta["face_row_count"] = len(face_rows)
        meta["audio_row_count"] = len(audio_rows)
        return extract_face_asset_from_jobs(face_rows), extract_audio_asset_from_jobs(audio_rows), meta
    except Exception as ex:
        meta["note"] = f"db lookup failed: {ex}"
        return None, None, meta


def build_face_preview_payload() -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "mode": "text-to-image",
        "count": max(1, FACE_COUNT),
        "studio_input": {
            "user_prompt": FACE_PROMPT,
            "gender": FACE_GENDER,
        },
    }
    return payload


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
    preview_fp = first_present([
        preview_resp.get("preview_fingerprint"),
        recursive_collect_first_str(preview_resp, {"preview_fingerprint"}),
    ])
    if quote_id and preview_fp:
        payload["pricing_confirmation"] = {
            "quote_id": quote_id,
            "preview_fingerprint": preview_fp,
        }
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
    artifact_id = recursive_collect_first_str(payload, {"face_artifact_id", "artifact_id", "image_artifact_id", "selected_face_artifact_id"})
    image_url = recursive_collect_first_str(payload, {"face_image_url", "image_url", "preview_url", "signed_url", "url", "sas_url", "blob_url", "storage_ref"})
    return {
        "source": "generated",
        "source_job_id": job_id,
        "face_artifact_id": artifact_id,
        "face_image_url": image_url,
        "image_ref": image_url,
    }


def ensure_face_asset(access_token: str, user_id: str, existing: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    meta: Dict[str, Any] = {}
    if existing and (existing.get("face_artifact_id") or existing.get("face_image_url")):
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
    if not asset.get("face_artifact_id") and not asset.get("face_image_url"):
        raise RuntimeError(f"Face job succeeded but no face asset was found in payload: {final_status}")
    return asset, meta


def build_audio_preview_payload() -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "text": STORY_TEXT,
        "target_locale": AUDIO_LOCALE,
        "output_format": AUDIO_OUTPUT_FORMAT,
    }
    if AUDIO_VOICE.strip():
        payload["voice"] = AUDIO_VOICE.strip()
    return payload


def preview_audio(access_token: str, user_id: str) -> Dict[str, Any]:
    _, _, payload = http_json(
        "POST",
        f"{AUDIO_URL}{AUDIO_PREVIEW_PATH}",
        headers=common_headers(access_token, user_id),
        payload=build_audio_preview_payload(),
        timeout=60,
    )
    return payload if isinstance(payload, dict) else {}


def create_audio_job(access_token: str, user_id: str, preview_resp: Dict[str, Any]) -> Dict[str, Any]:
    payload = build_audio_preview_payload()
    quote_id = first_present([preview_resp.get("quote_id"), recursive_collect_first_str(preview_resp, {"quote_id"})])
    preview_fp = first_present([
        preview_resp.get("preview_fingerprint"),
        recursive_collect_first_str(preview_resp, {"preview_fingerprint"}),
    ])
    if quote_id and preview_fp:
        payload["pricing_confirmation"] = {
            "quote_id": quote_id,
            "preview_fingerprint": preview_fp,
        }
    _, _, resp = http_json(
        "POST",
        f"{AUDIO_URL}{AUDIO_GENERATE_PATH}",
        headers=common_headers(access_token, user_id),
        payload=payload,
        timeout=90,
    )
    if not isinstance(resp, dict):
        raise RuntimeError(f"Unexpected audio create response: {resp}")
    return resp


def poll_audio_status(access_token: str, user_id: str, job_id: str) -> Dict[str, Any]:
    deadline = time.time() + float(JOB_TIMEOUT_SECONDS)
    last_payload: Dict[str, Any] = {}
    while time.time() < deadline:
        _, _, payload = http_json(
            "GET",
            f"{AUDIO_URL}{AUDIO_STATUS_PATH.format(job_id=urllib.parse.quote(job_id))}",
            headers=common_headers(access_token, user_id),
            timeout=60,
        )
        if isinstance(payload, dict):
            last_payload = payload
            status = str(payload.get("status") or payload.get("stage") or "").lower()
            if status in {"succeeded", "success", "completed", "done"}:
                return payload
            if status in {"failed", "error", "cancelled", "canceled"}:
                raise RuntimeError(f"Audio job failed: {payload}")
        time.sleep(POLL_SECONDS)
    raise RuntimeError(f"Audio job timed out. last_payload={last_payload}")


def extract_audio_asset_from_status(payload: Dict[str, Any], job_id: str) -> Dict[str, Optional[str]]:
    artifact_id = recursive_collect_first_str(payload, {"audio_artifact_id", "voice_audio_artifact_id", "artifact_id"})
    audio_url = recursive_collect_first_str(payload, {"audio_url", "voice_audio_url", "url", "signed_url", "preview_url", "sas_url", "blob_url"})
    return {
        "source": "generated",
        "source_job_id": job_id,
        "audio_artifact_id": artifact_id,
        "audio_url": audio_url,
    }


def ensure_audio_asset(access_token: str, user_id: str, existing: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    meta: Dict[str, Any] = {}
    if existing and (existing.get("audio_artifact_id") or existing.get("audio_url")):
        meta["strategy"] = "database"
        return existing, meta

    meta["strategy"] = "generate"
    preview_resp = preview_audio(access_token, user_id)
    meta["preview_response"] = preview_resp
    created = create_audio_job(access_token, user_id, preview_resp)
    meta["create_response"] = created
    job_id = first_present([created.get("job_id"), created.get("id"), recursive_collect_first_str(created, {"job_id", "id"})])
    if not job_id:
        raise RuntimeError(f"Audio create response missing job_id: {created}")
    final_status = poll_audio_status(access_token, user_id, job_id)
    meta["status_response"] = final_status
    asset = extract_audio_asset_from_status(final_status, job_id)
    if not asset.get("audio_artifact_id") and not asset.get("audio_url"):
        raise RuntimeError(f"Audio job succeeded but no audio asset was found in payload: {final_status}")
    return asset, meta


def build_directed_segments(face: Dict[str, Optional[str]], audio: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    parts = split_story_text(STORY_TEXT)
    face_artifact_id = face.get("face_artifact_id")
    face_image_url = face.get("face_image_url")
    audio_artifact_id = audio.get("audio_artifact_id")
    audio_url = audio.get("audio_url")
    support_images = [x for x in [face_image_url] if x]
    support_videos: List[str] = []

    return [
        {
            "segment_index": 0,
            "shot_type": "title_card",
            "render_route": "internal_card",
            "duration_sec": 3,
            "aspect_ratio": ASPECT_RATIO,
            "script": {
                "title": STORY_TITLE,
                "subtitle": "Storytelling quality check",
                "cta": "DesiFaces",
                "onscreen_text": [STORY_TITLE, "Storytelling quality check"],
            },
            "resolved_assets": {
                "logo_url": None,
                "face_artifact_id": face_artifact_id,
                "face_image_url": face_image_url,
            },
        },
        {
            "segment_index": 1,
            "shot_type": "presenter_anchor",
            "render_route": "fusion",
            "duration_sec": 8,
            "aspect_ratio": ASPECT_RATIO,
            "script": {
                "spoken_text": parts[0],
                "onscreen_text": [parts[0]],
            },
            "resolved_assets": {
                "face_artifact_id": face_artifact_id,
                "face_image_url": face_image_url,
                "voice_audio_artifact_id": audio_artifact_id,
                "voice_audio_url": audio_url,
            },
        },
        {
            "segment_index": 2,
            "shot_type": "narrated_broll",
            "render_route": "audio_broll",
            "duration_sec": 8,
            "aspect_ratio": ASPECT_RATIO,
            "script": {
                "voiceover_text": parts[1],
                "onscreen_text": [parts[1]],
                "subtitle": "Narrated support visuals",
            },
            "resolved_assets": {
                "face_artifact_id": face_artifact_id,
                "face_image_url": face_image_url,
                "voice_audio_artifact_id": audio_artifact_id,
                "voice_audio_url": audio_url,
                "image_urls": support_images,
                "video_urls": support_videos,
                "screenshot_urls": [],
            },
        },
        {
            "segment_index": 3,
            "shot_type": "closing_montage",
            "render_route": "internal_montage",
            "duration_sec": 8,
            "aspect_ratio": ASPECT_RATIO,
            "script": {
                "onscreen_text": [parts[2]],
                "subtitle": "Closing montage",
                "cta": "Rendered by svc-fusion-extension",
            },
            "resolved_assets": {
                "face_artifact_id": face_artifact_id,
                "face_image_url": face_image_url,
                "image_urls": support_images,
                "video_urls": support_videos,
                "screenshot_urls": [],
            },
        },
    ]


def build_longform_payloads(face: Dict[str, Optional[str]], audio: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    segments = build_directed_segments(face, audio)
    common_tags = {
        "intent": {"mode": "directed", "scenario_type": LONGFORM_SCENARIO_TYPE},
        "scenario": {"scenario_type": LONGFORM_SCENARIO_TYPE},
        "face_artifact_id": face.get("face_artifact_id"),
        "face_image_url": face.get("face_image_url"),
        "image_ref": face.get("image_ref") or face.get("face_image_url"),
        "image_url": face.get("face_image_url"),
        "audio_artifact_id": audio.get("audio_artifact_id"),
        "audio_url": audio.get("audio_url"),
        "directed_plan": {
            "timeline": {"segments": segments, "shots": segments},
            "segments_by_index": {str(item["segment_index"]): item for item in segments},
            "shots": segments,
        },
    }
    common_constraints = {"external_provider_ok": True}

    base_payload = {
        "title": STORY_TITLE,
        "mode": "directed",
        "scenario_type": LONGFORM_SCENARIO_TYPE,
        "aspect_ratio": ASPECT_RATIO,
        "script": STORY_TEXT,
        "prompt": STORY_TEXT,
        "user_prompt": STORY_TEXT,
        "intent": {
            "goal": LONGFORM_INTENT_GOAL,
            "scenario_type": LONGFORM_SCENARIO_TYPE,
            "prompt": STORY_TEXT,
            "user_prompt": STORY_TEXT,
        },
        "constraints": common_constraints,
        "face_artifact_id": face.get("face_artifact_id"),
        "face_image_url": face.get("face_image_url"),
        "image_ref": face.get("image_ref") or face.get("face_image_url"),
        "image_url": face.get("face_image_url"),
        "audio_artifact_id": audio.get("audio_artifact_id"),
        "audio_url": audio.get("audio_url"),
        "tags": common_tags,
        "directed_plan": common_tags["directed_plan"],
    }

    payload_alt = {
        "title": STORY_TITLE,
        "mode": "directed",
        "scenario_type": LONGFORM_SCENARIO_TYPE,
        "aspect_ratio": ASPECT_RATIO,
        "script": STORY_TEXT,
        "intent": {
            "goal": LONGFORM_INTENT_GOAL,
            "scenario_type": LONGFORM_SCENARIO_TYPE,
        },
        "constraints": common_constraints,
        "tags": common_tags,
        "directed_plan": common_tags["directed_plan"],
        "assets": {
            "face_artifact_id": face.get("face_artifact_id"),
            "face_image_url": face.get("face_image_url"),
            "audio_artifact_id": audio.get("audio_artifact_id"),
            "audio_url": audio.get("audio_url"),
        },
    }

    return [base_payload, payload_alt]


def try_create_longform_job(access_token: str, user_id: str, payloads: List[Dict[str, Any]]) -> Tuple[str, str, Dict[str, Any], Dict[str, Any]]:
    headers = common_headers(access_token, user_id)
    errors: List[str] = []
    for path in LONGFORM_CREATE_PATHS:
        url = f"{LONGFORM_URL}{path}"
        for idx, payload in enumerate(payloads):
            try:
                _, _, resp = http_json("POST", url, headers=headers, payload=payload, timeout=90)
                if not isinstance(resp, dict):
                    errors.append(f"{url} payload[{idx}] returned non-dict response: {resp}")
                    continue
                job_id = first_present([resp.get("job_id"), resp.get("id"), recursive_collect_first_str(resp, {"job_id", "id"})])
                if job_id:
                    return url, job_id, payload, resp
                errors.append(f"{url} payload[{idx}] returned no job_id: {resp}")
            except Exception as ex:
                errors.append(f"{url} payload[{idx}] -> {ex}")
    raise RuntimeError("Unable to create longform job.\n" + "\n".join(errors))


def poll_longform_job(access_token: str, user_id: str, job_id: str) -> Tuple[str, Dict[str, Any]]:
    headers = common_headers(access_token, user_id)
    deadline = time.time() + float(JOB_TIMEOUT_SECONDS)
    last_payload: Dict[str, Any] = {}
    while time.time() < deadline:
        for path in LONGFORM_STATUS_PATHS:
            url = f"{LONGFORM_URL}{path.format(job_id=urllib.parse.quote(job_id))}"
            try:
                _, _, payload = http_json("GET", url, headers=headers, timeout=60)
                if not isinstance(payload, dict):
                    continue
                last_payload = payload
                status = str(payload.get("status") or payload.get("stage") or "").lower()
                if status in {"succeeded", "success", "completed", "done"}:
                    return url, payload
                if status in {"failed", "error", "cancelled", "canceled"}:
                    raise RuntimeError(f"Longform job failed: {payload}")
            except Exception:
                continue
        time.sleep(POLL_SECONDS)
    raise RuntimeError(f"Timed out polling longform job {job_id}. last_payload={last_payload}")


def extract_final_video_url(payload: Dict[str, Any]) -> Optional[str]:
    return first_present([
        payload.get("video_url"),
        payload.get("final_video_url"),
        payload.get("output_url"),
        payload.get("artifact_url"),
        recursive_collect_first_str(payload, {"video_url", "final_video_url", "output_url", "artifact_url", "url"}),
    ])


def main() -> int:
    ensure_dir(OUTPUT_DIR)
    summary: Dict[str, Any] = {
        "out_dir": OUTPUT_DIR,
        "started_at": dt.datetime.utcnow().isoformat() + "Z",
        "core_url": CORE_URL,
        "face_url": FACE_URL,
        "audio_url": AUDIO_URL,
        "longform_url": LONGFORM_URL,
        "df_email": DF_EMAIL,
    }

    wait_for_service_health(LONGFORM_URL, HEALTH_TIMEOUT_SECONDS)

    print_step("Login")
    auth = login()
    write_json(os.path.join(OUTPUT_DIR, "auth.json"), auth["raw"])
    summary["user_id"] = auth["user_id"]

    print_step("Resolve face/audio assets")
    db_face, db_audio, db_meta = maybe_lookup_db_assets(auth["user_id"])
    summary["db_lookup"] = db_meta

    face_asset, face_meta = ensure_face_asset(auth["access_token"], auth["user_id"], db_face)
    audio_asset, audio_meta = ensure_audio_asset(auth["access_token"], auth["user_id"], db_audio)

    summary["resolved_face_asset"] = face_asset
    summary["resolved_audio_asset"] = audio_asset
    summary["face_flow"] = face_meta
    summary["audio_flow"] = audio_meta

    write_json(os.path.join(OUTPUT_DIR, "face_flow.json"), face_meta)
    write_json(os.path.join(OUTPUT_DIR, "audio_flow.json"), audio_meta)

    print_step("Create directed storytelling longform job")
    payloads = build_longform_payloads(face_asset, audio_asset)
    write_json(os.path.join(OUTPUT_DIR, "longform_payload_candidates.json"), payloads)

    create_url, job_id, chosen_payload, create_resp = try_create_longform_job(auth["access_token"], auth["user_id"], payloads)
    summary["longform_create_url"] = create_url
    summary["longform_job_id"] = job_id
    write_json(os.path.join(OUTPUT_DIR, "longform_create_payload.json"), chosen_payload)
    write_json(os.path.join(OUTPUT_DIR, "longform_create_response.json"), create_resp)

    print_step(f"Poll longform job: {job_id}")
    status_url, final_status = poll_longform_job(auth["access_token"], auth["user_id"], job_id)
    summary["longform_status_url"] = status_url
    summary["final_status"] = final_status.get("status") or final_status.get("stage")
    summary["final_video_url"] = extract_final_video_url(final_status)
    write_json(os.path.join(OUTPUT_DIR, "longform_status.json"), final_status)

    summary["completed_at"] = dt.datetime.utcnow().isoformat() + "Z"
    write_json(os.path.join(OUTPUT_DIR, "summary.json"), summary)
    print_step("Done")
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
