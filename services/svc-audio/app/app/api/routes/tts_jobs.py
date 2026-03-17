from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_user_id
from app.db import get_pool
from app.services.tts_orchestrator import PricingClientError
from app.services.tts_orchestrator import TTSOrchestrator

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


def _extract_pricing_view(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = _jsonb_to_dict(job.get("payload_json"))
    meta = _jsonb_to_dict(job.get("meta_json"))

    pricing = _jsonb_to_dict(payload.get("pricing"))
    if pricing:
        return pricing

    pricing = _jsonb_to_dict(meta.get("pricing"))
    if pricing:
        return pricing

    return None


def _raise_http_for_pricing_error(exc: Exception) -> None:
    msg = str(exc or "")

    if "PRICING_CLIENT_DISABLED" in msg or "pricing client unavailable" in msg.lower():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PRICING_CLIENT_DISABLED",
        )

    if "PRICING_UNKNOWN_OR_INACTIVE_VARIANT" in msg:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="PRICING_UNKNOWN_OR_INACTIVE_VARIANT",
        )

    if "PRICING_VARIANT_ZERO_QTY_LINES" in msg:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="PRICING_VARIANT_ZERO_QTY_LINES",
        )

    raise HTTPException(
        status_code=status.HTTP_424_FAILED_DEPENDENCY,
        detail="PRICING_RESERVATION_FAILED",
    )


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
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    pricing: Optional[Dict[str, Any]] = None


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
    pricing: Optional[Dict[str, Any]] = None


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

    orch = TTSOrchestrator(pool)

    try:
        job_id = await orch.create_job(user_id=user_id, payload=payload)
    except PricingClientError as e:
        _raise_http_for_pricing_error(e)

    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT id::text, status, error_code, error_message, payload_json, meta_json
            FROM public.studio_jobs
            WHERE id = $1::uuid
              AND user_id = $2::uuid
            """,
            job_id,
            user_id,
        )

    if not job:
        return JobCreatedResponse(job_id=job_id, status="queued")

    job_dict = dict(job)

    return JobCreatedResponse(
        job_id=job_dict["id"],
        status=job_dict["status"],
        error_code=job_dict.get("error_code"),
        error_message=job_dict.get("error_message"),
        pricing=_extract_pricing_view(job_dict),
    )


@router.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_pool),
) -> JobStatusResponse:
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """
            SELECT id::text, status, error_code, error_message, payload_json, meta_json
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

    job_dict = dict(job)

    return JobStatusResponse(
        job_id=job_dict["id"],
        status=job_dict["status"],
        error_code=job_dict.get("error_code"),
        error_message=job_dict.get("error_message"),
        variants=variants,
        payload=_jsonb_to_dict(job_dict["payload_json"]),
        pricing=_extract_pricing_view(job_dict),
    )