from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _install_shared_llm_path() -> None:
    """Make services/shared/llm importable in service images where it is mounted.

    In this repo, shared LLM helpers live at services/shared/llm, while the
    packaged pricing helpers live under desifaces_shared. Some service images do
    not package desifaces_shared.llm, so the Audio tips route must also support
    importing directly from the mounted repo path.
    """
    candidates = [
        os.getenv("DF_SHARED_LLM_PATH"),
        "/app/services/shared/llm",
        "/app/shared/llm",
        "/app/shared_llm",
        "/workspace/desifaces-v2/services/shared/llm",
        "/home/azureuser/workspace/desifaces-v2/services/shared/llm",
    ]
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(str(parent / "services" / "shared" / "llm"))
        candidates.append(str(parent / "shared" / "llm"))
    for raw in candidates:
        if not raw:
            continue
        path = Path(str(raw)).resolve()
        if path.exists() and path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


_install_shared_llm_path()

PROMPT_ENHANCER_AVAILABLE = False
STUDIO_COACH_AVAILABLE = False

try:
    from desifaces_shared.llm.prompt_enhancer import (
        PromptEnhanceRequest,
        PromptEnhanceResponse,
        enhance_prompt,
    )
    PROMPT_ENHANCER_AVAILABLE = True
except Exception:
    try:
        from prompt_enhancer import (  # type: ignore
            PromptEnhanceRequest,
            PromptEnhanceResponse,
            enhance_prompt,
        )
        PROMPT_ENHANCER_AVAILABLE = True
    except Exception as exc:
        logger.warning("shared_prompt_enhancer_unavailable: %s", exc)

        class PromptEnhanceRequest(BaseModel):
            studio: str = "audio"
            mode: Optional[str] = None
            user_input: str
            locked_fields: Dict[str, Any] = Field(default_factory=dict)
            context: Dict[str, Any] = Field(default_factory=dict)
            locale: str = "en"
            max_alternatives: int = 3

        class PromptEnhanceResponse(BaseModel):
            original_input: str
            enhanced_input: str
            alternatives: List[Dict[str, str]] = Field(default_factory=list)
            tips: List[str] = Field(default_factory=list)
            source: str = "fallback"
            fallback_used: bool = True

        async def enhance_prompt(
            req: PromptEnhanceRequest,
            force_fallback: bool = False,
        ) -> PromptEnhanceResponse:
            original = (req.user_input or "").strip()
            if not original:
                return PromptEnhanceResponse(
                    original_input="",
                    enhanced_input="",
                    alternatives=[],
                    tips=["Start with a clear spoken script, then choose the target locale and voice."],
                    source="fallback",
                    fallback_used=True,
                )
            return PromptEnhanceResponse(
                original_input=original,
                enhanced_input=original,
                alternatives=[],
                tips=[
                    "Shorter sentences usually sound cleaner in TTS.",
                    "Add tone and pacing explicitly for better delivery control.",
                ],
                source="fallback",
                fallback_used=True,
            )

try:
    from desifaces_shared.llm.studio_coach import (
        StudioCoachRequest,
        StudioCoachResponse,
        fallback_tips,
        rank_studio_tips,
    )
    STUDIO_COACH_AVAILABLE = True
except Exception:
    try:
        from studio_coach import (  # type: ignore
            StudioCoachRequest,
            StudioCoachResponse,
            fallback_tips,
            rank_studio_tips,
        )
        STUDIO_COACH_AVAILABLE = True
    except Exception as exc:
        logger.warning("shared_studio_coach_unavailable: %s", exc)

        class StudioCoachRequest(BaseModel):
            studio: str = "audio"
            mode: Optional[str] = None
            prompt: Optional[str] = None
            form_state: Dict[str, Any] = Field(default_factory=dict)
            context: Dict[str, Any] = Field(default_factory=dict)
            locale: str = "en"
            limit: int = 4

        class StudioCoachResponse(BaseModel):
            studio: str
            tips: List[Dict[str, Any]] = Field(default_factory=list)
            source: str = "fallback"
            fallback_used: bool = True
            ttl_seconds: int = 180
            rotation_key: Optional[str] = None

        def fallback_tips(req: StudioCoachRequest) -> List[Dict[str, Any]]:
            tips: List[Dict[str, Any]] = [
                {
                    "id": "audio-fallback-tighten",
                    "title": "Tighten the script",
                    "body": "Short spoken lines usually sound more natural than long written paragraphs.",
                    "tone": "premium",
                    "weight": 0.0,
                    "tags": {"source": "local_fallback"},
                },
                {
                    "id": "audio-fallback-locale",
                    "title": "Lock the locale and voice",
                    "body": "Pick the target language and voice before you refine the script so delivery stays consistent.",
                    "tone": "neutral",
                    "weight": 0.0,
                    "tags": {"source": "local_fallback"},
                },
            ]
            return tips[: max(1, min(int(req.limit or 4), 6))]

        def rank_studio_tips(
            req: StudioCoachRequest,
            candidates: List[Dict[str, Any]],
            *,
            include_fallback_when_sparse: bool = True,
        ) -> StudioCoachResponse:
            # Local emergency fallback only; production path should import the
            # shared ranker from services/shared/llm/studio_coach.py.
            tips = fallback_tips(req)
            return StudioCoachResponse(
                studio="audio",
                tips=tips,
                source="fallback",
                fallback_used=True,
                ttl_seconds=180,
                rotation_key="fallback",
            )

SHARED_LLM_AVAILABLE = PROMPT_ENHANCER_AVAILABLE and STUDIO_COACH_AVAILABLE

from app.api.deps import get_current_user_id
from app.db import get_pool
from app.services.tts_orchestrator import PricingClientError
from app.services.tts_orchestrator import TTSOrchestrator
from desifaces_shared.pricing.orchestration import (
    PricingPreviewSpec,
    build_preview_request,
    make_preview_artifact,
)

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



def _tip_to_dict(tip: Any) -> Dict[str, Any]:
    """Return a JSON-safe dict for dict/Pydantic/object tip payloads."""
    if tip is None:
        return {}
    if hasattr(tip, "model_dump"):
        try:
            d = tip.model_dump(mode="json")
            return d if isinstance(d, dict) else {}
        except Exception:
            pass
    if isinstance(tip, dict):
        return dict(tip)
    try:
        d = dict(tip)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {"title": str(tip), "body": ""}


def _tip_display_key(tip: Any) -> str:
    """Normalize a tip identity for display de-duplication.

    Studio Coach refresh can legitimately create multiple DB rows with the same
    title across refresh runs. Returning duplicate titles in the UI feels broken,
    so de-dupe primarily by title, falling back to body/id when needed.
    """
    d = _tip_to_dict(tip)
    title = str(d.get("title") or "").strip().lower()
    body = str(d.get("body") or "").strip().lower()
    tip_id = str(d.get("id") or "").strip().lower()
    return title or body or tip_id


def _tip_is_fallback(tip: Any) -> bool:
    d = _tip_to_dict(tip)
    tip_id = str(d.get("id") or "").lower()
    source = str(d.get("source") or "").lower()
    tags = _jsonb_to_dict(d.get("tags"))
    tag_source = str(tags.get("source") or "").lower()
    if source == "fallback" or tag_source in {"fallback", "local_fallback"}:
        return True
    return "fallback" in tip_id


def _candidate_row_to_tip(row: Dict[str, Any]) -> Dict[str, Any]:
    tags = _jsonb_to_dict(row.get("tags_json"))
    tip: Dict[str, Any] = {
        "id": str(row.get("id") or ""),
        "title": str(row.get("title") or "").strip(),
        "body": str(row.get("body") or "").strip(),
        "tone": str(row.get("tone") or "neutral"),
        "weight": float(row.get("priority") or 0.0),
        "tags": tags,
    }
    # Keep internal/source fields out of the public API payload unless the
    # shared ranker already chooses to expose them. The audit payload still
    # captures the full selected tip objects.
    return {k: v for k, v in tip.items() if v is not None}


def _dedupe_tips_for_display(tips: List[Any], limit: int) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    safe_limit = max(1, min(int(limit or 4), 6))

    for tip in tips or []:
        d = _tip_to_dict(tip)
        key = _tip_display_key(d)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(d)
        if len(out) >= safe_limit:
            break

    return out


def _normalize_coach_response_for_display(
    *,
    req: StudioCoachRequest,
    response: StudioCoachResponse,
    candidate_rows: Optional[List[Dict[str, Any]]] = None,
    limit: int = 4,
) -> StudioCoachResponse:
    """De-dupe ranked tips and backfill from DB/fallback only when needed."""
    safe_limit = max(1, min(int(limit or getattr(req, "limit", 4) or 4), 6))
    selected = _dedupe_tips_for_display(getattr(response, "tips", []) or [], safe_limit)
    seen_keys = {_tip_display_key(t) for t in selected if _tip_display_key(t)}

    # Prefer additional DB rows over fallback when the shared ranker selected a
    # duplicate title from multiple refresh batches.
    for row in candidate_rows or []:
        if len(selected) >= safe_limit:
            break
        tip = _candidate_row_to_tip(row)
        key = _tip_display_key(tip)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        selected.append(tip)

    # Only use fallback when DB candidates still cannot satisfy the requested
    # count after display de-dupe.
    if len(selected) < safe_limit:
        for tip in fallback_tips(req) if "fallback_tips" in globals() else []:
            if len(selected) >= safe_limit:
                break
            d = _tip_to_dict(tip)
            key = _tip_display_key(d)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            selected.append(d)

    fallback_used = any(_tip_is_fallback(t) for t in selected)
    has_db_or_nonfallback = any(not _tip_is_fallback(t) for t in selected)

    if fallback_used and has_db_or_nonfallback:
        source = "hybrid"
    elif fallback_used:
        source = "fallback"
    else:
        source = "db"

    kwargs: Dict[str, Any] = {
        "studio": getattr(response, "studio", None) or getattr(req, "studio", "audio") or "audio",
        "tips": selected[:safe_limit],
        "source": source,
        "fallback_used": fallback_used,
        "ttl_seconds": int(getattr(response, "ttl_seconds", 180) or 180),
    }
    rotation_key = getattr(response, "rotation_key", None)
    if rotation_key is not None:
        kwargs["rotation_key"] = rotation_key

    try:
        return StudioCoachResponse(**kwargs)
    except TypeError:
        kwargs.pop("rotation_key", None)
        return StudioCoachResponse(**kwargs)



def _coach_response_from_fallback(req: StudioCoachRequest) -> StudioCoachResponse:
    raw_tips = fallback_tips(req) if "fallback_tips" in globals() else [
        {"title": "Tighten the script", "body": "Short spoken lines usually sound more natural than long written paragraphs."},
        {"title": "Lock the locale and voice", "body": "Pick the target language and voice before you refine the script so delivery stays consistent."},
    ]
    limit = max(1, min(int(getattr(req, "limit", 4) or 4), 6))
    tips = raw_tips[:limit]
    try:
        return StudioCoachResponse(
            studio="audio",
            tips=tips,
            source="fallback",
            fallback_used=True,
            ttl_seconds=180,
            rotation_key="fallback",
        )
    except Exception:
        # Compatibility for older fallback response models without rotation_key.
        return StudioCoachResponse(
            studio="audio",
            tips=[t.model_dump(mode="json") if hasattr(t, "model_dump") else dict(t) if isinstance(t, dict) else {"title": str(t), "body": ""} for t in tips],
            source="fallback",
            fallback_used=True,
            ttl_seconds=180,
        )


async def _fetch_studio_coach_tip_rows(
    conn: asyncpg.Connection,
    *,
    studio: str,
    mode: str,
    locale: str,
    limit: int,
) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT
            id::text,
            studio,
            mode,
            locale,
            title,
            body,
            tone,
            priority,
            source,
            targeting_json,
            tags_json,
            is_active,
            expires_at
        FROM public.studio_coach_tips
        WHERE is_active = TRUE
          AND studio = $1
          AND (mode IS NULL OR mode = '' OR mode = $2)
          AND (locale IS NULL OR locale = '' OR locale = $3 OR locale = 'en')
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY priority DESC, updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        LIMIT $4
        """,
        studio,
        mode,
        locale,
        max(24, min(200, int(limit or 4) * 12)),
    )
    return [dict(r) for r in rows]


async def _table_columns(conn: asyncpg.Connection, table_name: str) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = $1
        """,
        table_name,
    )
    return {str(r["column_name"]) for r in rows}


async def _write_studio_coach_tip_audit(
    conn: asyncpg.Connection,
    *,
    user_id: str,
    req: StudioCoachRequest,
    response: StudioCoachResponse,
) -> None:
    """Best-effort audit write for served tips.

    This is intentionally schema-tolerant: it writes only columns that exist so
    early launch DB migrations do not break the live TTS tips path.
    """
    try:
        cols = await _table_columns(conn, "studio_coach_tip_audit")
        if not cols:
            return

        tips_payload: List[Dict[str, Any]] = []
        tip_ids: List[str] = []
        for tip in getattr(response, "tips", []) or []:
            if hasattr(tip, "model_dump"):
                d = tip.model_dump(mode="json")
            elif isinstance(tip, dict):
                d = dict(tip)
            else:
                d = {"title": str(tip)}
            tips_payload.append(d)
            if d.get("id"):
                tip_ids.append(str(d["id"]))

        payload: Dict[str, Any] = {
            "action": "served",
            "user_id": str(user_id),
            "studio": req.studio,
            "mode": req.mode or "tts",
            "locale": req.locale or "en",
            "source": getattr(response, "source", None),
            "fallback_used": bool(getattr(response, "fallback_used", False)),
            "rotation_key": getattr(response, "rotation_key", None),
            "tip_ids": tip_ids,
            "tips": tips_payload,
            "request_json": {
                "prompt": req.prompt,
                "form_state": req.form_state or {},
                "context": req.context or {},
                "limit": req.limit,
            },
            "response_json": {
                "studio": req.studio,
                "source": getattr(response, "source", None),
                "fallback_used": bool(getattr(response, "fallback_used", False)),
                "ttl_seconds": getattr(response, "ttl_seconds", 180),
                "rotation_key": getattr(response, "rotation_key", None),
                "tips": tips_payload,
            },
        }

        insert_cols: List[str] = []
        values: List[Any] = []
        sql_values: List[str] = []
        json_cols = {"tip_ids", "tips", "request_json", "response_json", "context_json", "form_state_json"}

        for col in (
            "action",
            "user_id",
            "studio",
            "mode",
            "locale",
            "source",
            "fallback_used",
            "rotation_key",
            "tip_ids",
            "tips",
            "request_json",
            "response_json",
        ):
            if col not in cols:
                continue
            insert_cols.append(col)
            if col == "user_id":
                values.append(payload[col])
                sql_values.append(f"${len(values)}::uuid")
            elif col in json_cols:
                values.append(json.dumps(payload[col], default=str))
                sql_values.append(f"${len(values)}::jsonb")
            else:
                values.append(payload[col])
                sql_values.append(f"${len(values)}")

        if "context_json" in cols and "context_json" not in insert_cols:
            insert_cols.append("context_json")
            values.append(json.dumps(req.context or {}, default=str))
            sql_values.append(f"${len(values)}::jsonb")
        if "form_state_json" in cols and "form_state_json" not in insert_cols:
            insert_cols.append("form_state_json")
            values.append(json.dumps(req.form_state or {}, default=str))
            sql_values.append(f"${len(values)}::jsonb")
        for ts_col in ("served_at", "created_at"):
            if ts_col in cols:
                insert_cols.append(ts_col)
                sql_values.append("NOW()")

        if insert_cols:
            sql = f"INSERT INTO public.studio_coach_tip_audit ({', '.join(insert_cols)}) VALUES ({', '.join(sql_values)})"
            await conn.execute(sql, *values)
    except Exception:
        return


async def _generate_audio_studio_tips_from_db(
    *,
    pool: asyncpg.Pool,
    user_id: str,
    req: StudioCoachRequest,
) -> StudioCoachResponse:
    limit = max(1, min(int(req.limit or 4), 6))
    try:
        async with pool.acquire() as conn:
            rows = await _fetch_studio_coach_tip_rows(
                conn,
                studio="audio",
                mode=req.mode or "tts",
                locale=req.locale or "en",
                limit=limit,
            )
            if rows and STUDIO_COACH_AVAILABLE:
                response = rank_studio_tips(req, rows, include_fallback_when_sparse=True)
            else:
                if not STUDIO_COACH_AVAILABLE:
                    logger.warning("audio_studio_tips_shared_ranker_unavailable; using fallback")
                elif not rows:
                    logger.info("audio_studio_tips_no_active_db_rows; using fallback")
                response = _coach_response_from_fallback(req)

            response = _normalize_coach_response_for_display(
                req=req,
                response=response,
                candidate_rows=rows,
                limit=limit,
            )
            await _write_studio_coach_tip_audit(conn, user_id=user_id, req=req, response=response)
            return response
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("audio_studio_tips_db_path_failed; using fallback: %s", exc)
        return _coach_response_from_fallback(req)

def _chars_1k_units(text: str) -> int:
    n = len((text or "").strip())
    if n <= 0:
        return 1
    return max(1, (n + 999) // 1000)


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


def _extract_pricing_summary_view(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = _jsonb_to_dict(job.get("payload_json"))
    meta = _jsonb_to_dict(job.get("meta_json"))

    summary = _jsonb_to_dict(payload.get("pricing_summary"))
    if summary:
        return summary

    summary = _jsonb_to_dict(meta.get("pricing_summary"))
    if summary:
        return summary

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

    if "PRICING_VARIANT_HAS_NO_LINES" in msg:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="PRICING_VARIANT_HAS_NO_LINES",
        )

    if "PRICING_INSUFFICIENT_CREDITS" in msg:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="PRICING_INSUFFICIENT_CREDITS",
        )

    raise HTTPException(
        status_code=status.HTTP_424_FAILED_DEPENDENCY,
        detail="PRICING_RESERVATION_FAILED",
    )


class PricingConfirmationInput(BaseModel):
    quote_id: Optional[str] = None
    preview_fingerprint: Optional[str] = None


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

    pricing_confirmation: Optional[PricingConfirmationInput] = None



class AudioPricingPreviewResponse(BaseModel):
    quote_id: Optional[str] = None
    preview_fingerprint: Optional[str] = None
    pricing: Dict[str, Any] = Field(default_factory=dict)
    pricing_summary: Dict[str, Any] = Field(default_factory=dict)
    message: Optional[str] = None


class AudioPromptEnhanceRequestModel(BaseModel):
    mode: Optional[str] = None
    user_input: str
    locked_fields: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    locale: str = "en"
    max_alternatives: int = 3


class AudioTipsRequestModel(BaseModel):
    mode: Optional[str] = None
    prompt: Optional[str] = None
    form_state: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    locale: str = "en"
    limit: int = 4


class JobCreatedResponse(BaseModel):
    job_id: str
    status: str
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    pricing: Optional[Dict[str, Any]] = None
    pricing_summary: Optional[Dict[str, Any]] = None


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
    pricing_summary: Optional[Dict[str, Any]] = None


def _build_audio_payload(req: TTSCreateRequest) -> Dict[str, Any]:
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
    if req.pricing_confirmation:
        payload["pricing_confirmation"] = req.pricing_confirmation.model_dump(exclude_none=True)
    return payload


def _audio_preview_meta(req: TTSCreateRequest) -> Dict[str, Any]:
    text = str(req.text or "")
    return {
        "mode": "tts",
        "target_locale": req.target_locale,
        "input_language": req.source_language or "en",
        "output_format": req.output_format,
        "text_length": len(text),
        "chars_1k": str(_chars_1k_units(text)),
    }


@router.post("/tts/pricing/preview", response_model=AudioPricingPreviewResponse)
@router.post("/pricing/preview", response_model=AudioPricingPreviewResponse, include_in_schema=False)
@router.post("/tts/preview", response_model=AudioPricingPreviewResponse, include_in_schema=False)
async def preview_tts_pricing(
    req: TTSCreateRequest,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_pool),
) -> AudioPricingPreviewResponse:
    orch = TTSOrchestrator(pool)

    try:
        pricing_client = orch.pricing_client
    except Exception as e:
        _raise_http_for_pricing_error(PricingClientError(str(e)))
        raise

    if not bool(getattr(pricing_client, "enabled", False)):
        _raise_http_for_pricing_error(PricingClientError("PRICING_CLIENT_DISABLED"))

    payload = _build_audio_payload(req)
    meta = _audio_preview_meta(req)
    estimated_units = str(_chars_1k_units(req.text))

    request_hash_src = {
        "studio_type": AUDIO_STUDIO_TYPE,
        "user_id": str(user_id),
        "payload": payload,
    }
    request_fingerprint = hashlib.sha256(
        json.dumps(
            request_hash_src,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    try:
        spec = PricingPreviewSpec(
            user_id=str(user_id),
            service_name="svc-audio",
            service_action="audio.tts.generate",
            sku_code=getattr(orch, "VARIANT_CODE", "AUDIO_TTS"),
            units=estimated_units,
            idempotency_key=f"svc-audio:preview:{user_id}:{request_fingerprint}",
            meta=meta,
        )
        resp = await pricing_client.preview(build_preview_request(spec))
        artifact = make_preview_artifact(
            resp,
            service_name="svc-audio",
            service_action="audio.tts.generate",
            sku_code=getattr(orch, "VARIANT_CODE", "AUDIO_TTS"),
            meta=meta,
        )
    except PricingClientError as e:
        _raise_http_for_pricing_error(e)
        raise
    except Exception as e:
        _raise_http_for_pricing_error(PricingClientError(str(e)))
        raise

    pricing = dict(artifact.get("pricing") or {})
    summary = dict(artifact.get("pricing_summary") or {})
    return AudioPricingPreviewResponse(
        quote_id=str(pricing.get("quote_id") or "") or None,
        preview_fingerprint=str(pricing.get("preview_fingerprint") or "") or None,
        pricing=pricing,
        pricing_summary=summary,
        message=str(getattr(resp, "message", "") or "") or None,
    )




@router.post("/tts/prompt/enhance", response_model=PromptEnhanceResponse)
async def tts_enhance_prompt(
    req: AudioPromptEnhanceRequestModel,
    user_id: str = Depends(get_current_user_id),
) -> PromptEnhanceResponse:
    try:
        return await enhance_prompt(
            PromptEnhanceRequest(
                studio="audio",
                mode=req.mode or "tts",
                user_input=req.user_input,
                locked_fields=req.locked_fields or {},
                context={
                    **(req.context or {}),
                    "user_id": str(user_id),
                    "surface": "svc-audio",
                },
                locale=req.locale,
                max_alternatives=max(1, min(req.max_alternatives, 4)),
            )
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "bad_request",
                "code": "DF_AUDIO_PROMPT_ENHANCE_BAD_REQUEST",
                "message": str(e),
            },
        )
    except Exception:
        return await enhance_prompt(
            PromptEnhanceRequest(
                studio="audio",
                mode=req.mode or "tts",
                user_input=req.user_input,
                locked_fields=req.locked_fields or {},
                context=req.context or {},
                locale=req.locale,
                max_alternatives=max(1, min(req.max_alternatives, 4)),
            ),
            force_fallback=True,
        )


@router.post("/tts/tips", response_model=StudioCoachResponse)
async def tts_studio_tips(
    req: AudioTipsRequestModel,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_pool),
) -> StudioCoachResponse:
    coach_req = StudioCoachRequest(
        studio="audio",
        mode=req.mode or "tts",
        prompt=req.prompt,
        form_state=req.form_state or {},
        context={
            **(req.context or {}),
            "user_id": str(user_id),
            "surface": "svc-audio",
            "rotation_bucket": int(time.time() // 180),
        },
        locale=req.locale or "en",
        limit=max(1, min(int(req.limit or 4), 6)),
    )

    try:
        return await _generate_audio_studio_tips_from_db(
            pool=pool,
            user_id=str(user_id),
            req=coach_req,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "bad_request",
                "code": "DF_AUDIO_TIPS_BAD_REQUEST",
                "message": str(e),
            },
        )
    except Exception:
        return _coach_response_from_fallback(coach_req)


@router.post("/tts", response_model=JobCreatedResponse)
async def create_tts_job(
    req: TTSCreateRequest,
    user_id: str = Depends(get_current_user_id),
    pool: asyncpg.Pool = Depends(get_pool),
) -> JobCreatedResponse:
    payload = _build_audio_payload(req)

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
        pricing_summary=_extract_pricing_summary_view(job_dict),
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
        pricing_summary=_extract_pricing_summary_view(job_dict),
    )
