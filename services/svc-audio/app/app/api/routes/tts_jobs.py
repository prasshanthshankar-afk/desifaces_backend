from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import get_pool
from app.api.deps import get_current_user_id

router = APIRouter(prefix="/api/audio", tags=["audio-tts"])

AUDIO_STUDIO_TYPE = "audio"


def _jsonb_to_dict(val: Any) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return {}
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    try:
        d = dict(val)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


class TTSCreateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    target_locale: str = Field(..., min_length=2, max_length=20)
    source_language: Optional[str] = Field(default=None, max_length=20)
    translate: bool = True

    voice: Optional[str] = None
    style: Optional[str] = None
    style_degree: Optional[float] = None
    rate: Optional[float] = None
    pitch: Optional[float] = None
    volume: Optional[float] = None
    context: Optional[str] = None

    output_format: str = Field(default="mp3")  # mp3|wav


class JobCreatedResponse(BaseModel):
    job_id: str
    status: str


class VariantAudio(BaseModel):
    audio_url: str
    artifact_id: Optional[str] = None
    content_type: Optional[str] = None
    bytes: Optional[int] = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    variants: List[VariantAudio] = Field(default_factory=list)
    payload: Dict[str, Any] = Field(default_factory=dict)


def _stable_hash(user_id: str, payload: Dict[str, Any]) -> str:
    s = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    h = hashlib.sha256()
    h.update(user_id.encode("utf-8"))
    h.update(b"|")
    h.update(s.encode("utf-8"))
    return h.hexdigest()


@router.post("/tts", response_model=JobCreatedResponse)
async def create_tts_job(
    req: TTSCreateRequest,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_pool),
) -> JobCreatedResponse:
    payload: Dict[str, Any] = {
        "text": req.text,
        "target_locale": req.target_locale,

        "source_language": req.source_language,
        "input_language": (req.source_language or "en"),

        "translate": req.translate,

        "voice": req.voice,
        "style": req.style,
        "style_degree": req.style_degree,
        "rate": req.rate,
        "pitch": req.pitch,
        "volume": req.volume,
        "context": req.context,

        "output_format": req.output_format,
    }

    request_hash = _stable_hash(user_id, payload)

    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO public.studio_jobs (studio_type, status, request_hash, payload_json, meta_json, user_id)
                VALUES ($1, 'queued', $2, $3::jsonb, $4::jsonb, $5::uuid)
                ON CONFLICT (user_id, studio_type, request_hash)
                DO UPDATE SET updated_at = now()
                RETURNING id::text, status
                """,
                AUDIO_STUDIO_TYPE,
                request_hash,
                json.dumps(payload),
                json.dumps({"request_type": "audio_tts"}),
                user_id,
            )
        except asyncpg.PostgresError as e:
            raise HTTPException(status_code=400, detail=f"db_error: {str(e)}")

    return JobCreatedResponse(job_id=row["id"], status=row["status"])


@router.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_pool),
) -> JobStatusResponse:
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT id::text, status, error_code, error_message, payload_json
            FROM public.studio_jobs
            WHERE id = $1::uuid
              AND user_id = $2::uuid
            """,
            job_id,
            user_id,
        )
        if not job:
            raise HTTPException(status_code=404, detail="job_not_found")

        arts = await conn.fetch(
            """
            SELECT id::text AS artifact_id, url, content_type, bytes
            FROM public.artifacts
            WHERE job_id = $1::uuid
              AND kind = 'audio'
            ORDER BY created_at DESC
            """,
            job_id,
        )

    variants = [
        VariantAudio(
            audio_url=a["url"],
            artifact_id=a["artifact_id"],
            content_type=a["content_type"],
            bytes=a["bytes"],
        )
        for a in arts
        if a.get("url")
    ]

    return JobStatusResponse(
        job_id=job["id"],
        status=job["status"],
        error_code=job["error_code"],
        error_message=job["error_message"],
        variants=variants,
        payload=_jsonb_to_dict(job["payload_json"]),
    )