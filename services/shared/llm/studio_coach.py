from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence

from pydantic import BaseModel, Field


Tone = Literal["neutral", "success", "warning", "premium"]
Studio = Literal["face", "audio", "fusion"]

ALLOWED_TONES = {"neutral", "success", "warning", "premium"}
DEFAULT_TTL_SECONDS = 180


class StudioCoachTip(BaseModel):
    id: str
    title: str
    body: str
    tone: Tone = "neutral"
    weight: float = 0.0
    tags: Dict[str, Any] = Field(default_factory=dict)


class StudioCoachRequest(BaseModel):
    studio: Studio
    mode: Optional[str] = None
    prompt: Optional[str] = None
    form_state: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    locale: str = "en"
    limit: int = 3


class StudioCoachResponse(BaseModel):
    studio: Studio
    tips: List[StudioCoachTip] = Field(default_factory=list)
    source: Literal["db", "hybrid", "fallback"] = "db"
    fallback_used: bool = False
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    rotation_key: str


@dataclass(frozen=True)
class CandidateTip:
    id: str
    studio: str
    title: str
    body: str
    tone: str = "neutral"
    locale: str = "en"
    mode: str = ""
    priority: float = 0.0
    targeting: Dict[str, Any] | None = None
    tags: Dict[str, Any] | None = None
    is_active: bool = True
    expires_at: datetime | None = None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _friendly_label(value: Any) -> str:
    raw = _clean_text(value)
    if not raw:
        return ""
    return raw.replace("_", " ").replace("-", " ").strip()


def _json_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return {}
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


def _rotation_key(*parts: Any) -> str:
    blob = json.dumps(parts, ensure_ascii=False, default=str, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha1(blob).hexdigest()[:12]


def _tip_id(title: str, body: str, tone: str) -> str:
    return _rotation_key(title, body, tone)


def _rotation_bucket(ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
    ttl = max(30, int(ttl_seconds or DEFAULT_TTL_SECONDS))
    return int(time.time() // ttl)


def _rotation_seed(req: StudioCoachRequest, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """
    Stable for a TTL bucket and request-shape, but changes over time.

    Excludes user_id-like values from context by default to keep rotation broadly
    consistent across users while still allowing plan/mode/form-state differences.
    """
    context = dict(req.context or {})
    for noisy_key in ("user_id", "userId", "sub", "email", "surface"):
        context.pop(noisy_key, None)
    return _rotation_key(
        req.studio,
        _clean_text(req.mode),
        _clean_text(req.locale) or "en",
        req.form_state or {},
        context,
        req.prompt or "",
        _rotation_bucket(ttl_seconds),
    )


def _weighted_rotation_sort_key(tip: StudioCoachTip, *, seed: str) -> tuple[float, float, str]:
    """
    Priority-aware deterministic random rotation.

    Higher weight still wins, but same/similar candidate pools rotate within the
    TTL window. This prevents the UI from always showing the same top rows while
    avoiding flicker during repeated renders.
    """
    rng = random.Random(_rotation_key(seed, tip.id, tip.title, tip.body))
    jitter = rng.random()
    return (-float(tip.weight or 0.0), -jitter, tip.title.lower())


# -----------------------------
# deterministic fallback tips
# -----------------------------

def _fallback_tip(title: str, body: str, tone: Tone = "neutral") -> StudioCoachTip:
    return StudioCoachTip(id=_tip_id(title, body, tone), title=title, body=body, tone=tone)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _face_fallback(req: StudioCoachRequest) -> List[StudioCoachTip]:
    form = req.form_state or {}
    context = req.context or {}
    mode = _clean_text(req.mode)
    shot_type = _friendly_label(form.get("shot_type_label") or form.get("shot_type_code"))
    context_label = _friendly_label(form.get("context_label") or form.get("context_code"))
    use_case = _friendly_label(form.get("use_case_label") or form.get("use_case_code"))
    aspect_ratio = _clean_text(form.get("aspect_ratio")) or "9:16"
    safety = _clean_text(form.get("image_safety_state"))
    num_variants = _safe_int(form.get("num_variants"), 4)
    plan_name = _clean_text(context.get("plan_name")).lower()

    tips: List[StudioCoachTip] = []
    if not shot_type:
        tips.append(_fallback_tip("Lock the framing", "Use one framing term like headshot, medium shot, or full-body so composition stays consistent.", "premium"))
    if not context_label:
        tips.append(_fallback_tip("Name the setting", "Adding a clear environment helps the background feel intentional instead of generic.", "neutral"))
    if not use_case:
        tips.append(_fallback_tip("Match the use case", "Describe whether this is for profile, promo, editorial, or social so styling fits the end use.", "neutral"))
    if mode == "image-to-image":
        body = (
            "With identity lock, ask for styling, lighting, attire, and background changes instead of changing the person."
            if safety == "passed"
            else "Use a clean, well-lit source photo facing the camera to improve Edit Face reliability."
        )
        tips.append(_fallback_tip("Preserve identity cleanly", body, "success"))
    else:
        tips.append(_fallback_tip("Bundle mood and light", "Put attire, mood, and lighting in the same sentence to reduce flat or generic outputs.", "premium"))
    if num_variants > 4 or plan_name == "free":
        tips.append(_fallback_tip("Save credits while testing", "Try 2 to 4 variants first, then scale up only after the prompt direction feels right.", "warning"))
    tips.append(_fallback_tip("Choose the downstream frame", f"You are currently building for {aspect_ratio}. Keep the face composition centered if you plan to continue into Audio and Fusion.", "neutral"))
    return tips


def _audio_fallback(req: StudioCoachRequest) -> List[StudioCoachTip]:
    form = req.form_state or {}
    context = req.context or {}
    locale = _friendly_label(form.get("target_locale") or form.get("locale") or req.locale)
    long_script = len(_clean_text(req.prompt)) > 220
    tips = [
        _fallback_tip("Prefer shorter sentences", "Shorter sentences usually sound cleaner and more natural in TTS.", "premium"),
        _fallback_tip("Direct the delivery", "Describe tone and pacing separately so the voice direction stays clear.", "neutral"),
        _fallback_tip("Match the target locale", f"Write natively for {locale} instead of translating word for word." if locale else "If you have a target locale, ask for native phrasing instead of direct translation.", "success"),
    ]
    if long_script:
        tips.append(_fallback_tip("Create a shorter pass", "Use a condensed version first to test voice fit before committing to the long script.", "warning"))
    if context.get("insufficient_balance"):
        tips.append(_fallback_tip("Test with a shorter clip", "Validate the voice with a shorter cut before generating the full script.", "warning"))
    return tips


def _fusion_fallback(req: StudioCoachRequest) -> List[StudioCoachTip]:
    form = req.form_state or {}
    aspect_ratio = _clean_text(form.get("aspect_ratio")) or "9:16"
    tips = [
        _fallback_tip("Keep one emotional arc", "Give the performer one clear emotional direction instead of stacking multiple moods.", "premium"),
        _fallback_tip("Separate motion layers", "Describe performer motion and background motion separately so the scene feels intentional.", "neutral"),
        _fallback_tip("Guide the camera independently", "Mention framing and camera movement separately from the acting prompt.", "success"),
        _fallback_tip("Build for the final frame", f"Your current aspect ratio is {aspect_ratio}. Keep the performer centered if this needs safe room for captions.", "neutral"),
    ]
    if _clean_text(req.prompt) and len(_clean_text(req.prompt)) < 35:
        tips.append(_fallback_tip("Add performance detail", "Short prompts tend to under-direct expression and body life. Add one emotional and one motion cue.", "warning"))
    return tips


def fallback_tips(req: StudioCoachRequest) -> List[StudioCoachTip]:
    if req.studio == "face":
        return _face_fallback(req)
    if req.studio == "audio":
        return _audio_fallback(req)
    return _fusion_fallback(req)


# -----------------------------
# DB row normalization
# -----------------------------

def candidate_tip_from_row(row: Dict[str, Any]) -> CandidateTip:
    targeting = _json_dict(row.get("targeting_json") or row.get("targeting"))
    tags = _json_dict(row.get("tags_json") or row.get("tags"))
    title = _clean_text(row.get("title"))
    body = _clean_text(row.get("body"))
    tone = _clean_text(row.get("tone")) or "neutral"
    return CandidateTip(
        id=_clean_text(row.get("id")) or _tip_id(title, body, tone),
        studio=_clean_text(row.get("studio")),
        mode=_clean_text(row.get("mode")),
        title=title,
        body=body,
        tone=tone,
        locale=_clean_text(row.get("locale")) or "en",
        priority=float(row.get("priority") or 0.0),
        targeting=targeting,
        tags=tags,
        is_active=bool(row.get("is_active", True)),
        expires_at=row.get("expires_at"),
    )


def _build_feature_map(req: StudioCoachRequest) -> Dict[str, Any]:
    form = req.form_state or {}
    context = req.context or {}
    prompt = _clean_text(req.prompt)
    target_locale = _clean_text(form.get("target_locale") or form.get("locale") or req.locale) or "en"
    return {
        "studio": req.studio,
        "mode": _clean_text(req.mode),
        "locale": _clean_text(req.locale) or "en",
        "target_locale": target_locale,
        "has_prompt": bool(prompt),
        "prompt_length": len(prompt),
        "prompt_length_bucket": (
            "empty" if not prompt else "short" if len(prompt) < 60 else "medium" if len(prompt) < 220 else "long"
        ),
        "plan_name": _clean_text(context.get("plan_name")).lower(),
        "insufficient_balance": bool(context.get("insufficient_balance")),
        "image_safety_state": _clean_text(form.get("image_safety_state")),
        "shot_type_code": _clean_text(form.get("shot_type_code")),
        "context_code": _clean_text(form.get("context_code")),
        "use_case_code": _clean_text(form.get("use_case_code")),
        "aspect_ratio": _clean_text(form.get("aspect_ratio")) or "9:16",
        "num_variants": _safe_int(form.get("num_variants"), 4),
        "voice": _clean_text(form.get("voice")),
        "style": _clean_text(form.get("style")),
        "output_format": _clean_text(form.get("output_format")),
    }


def _list_lower(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip().lower() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip().lower() for x in value.split(",") if x.strip()]
    return [str(value).strip().lower()] if str(value).strip() else []


def _match_targeting(targeting: Dict[str, Any], features: Dict[str, Any]) -> tuple[bool, float]:
    if not targeting:
        return True, 0.0

    score = 0.0

    studio = _clean_text(targeting.get("studio"))
    if studio and studio != features["studio"]:
        return False, 0.0
    if studio:
        score += 5.0

    mode = _clean_text(targeting.get("mode"))
    if mode and mode != features["mode"]:
        return False, 0.0
    if mode:
        score += 4.0

    locale = _clean_text(targeting.get("locale"))
    if locale and locale != features["locale"] and locale != "en":
        return False, 0.0
    if locale:
        score += 1.5

    for key in (
        "target_locale",
        "shot_type_code",
        "context_code",
        "use_case_code",
        "aspect_ratio",
        "image_safety_state",
        "voice",
        "style",
        "output_format",
    ):
        expected = _clean_text(targeting.get(key))
        if expected:
            if expected != _clean_text(features.get(key)):
                return False, 0.0
            score += 3.0

    prompt_bucket = _clean_text(targeting.get("prompt_length_bucket"))
    if prompt_bucket:
        if prompt_bucket != _clean_text(features.get("prompt_length_bucket")):
            return False, 0.0
        score += 2.0

    has_prompt = targeting.get("has_prompt")
    if has_prompt is not None:
        if bool(has_prompt) != bool(features.get("has_prompt")):
            return False, 0.0
        score += 1.5

    missing_any = targeting.get("missing_fields_any") or []
    if missing_any:
        missing_set = {field for field in missing_any if not _clean_text(features.get(field))}
        if not missing_set:
            return False, 0.0
        score += 3.5

    missing_all = targeting.get("missing_fields_all") or []
    if missing_all:
        if any(_clean_text(features.get(field)) for field in missing_all):
            return False, 0.0
        score += 4.0

    plan_in = _list_lower(targeting.get("plan_in"))
    if plan_in:
        if str(features.get("plan_name") or "").lower() not in plan_in:
            return False, 0.0
        score += 2.0

    if targeting.get("insufficient_balance") is not None:
        if bool(targeting.get("insufficient_balance")) != bool(features.get("insufficient_balance")):
            return False, 0.0
        score += 2.5

    variants_gte = targeting.get("num_variants_gte")
    if variants_gte is not None:
        if int(features["num_variants"]) < int(variants_gte):
            return False, 0.0
        score += 1.0

    variants_lte = targeting.get("num_variants_lte")
    if variants_lte is not None:
        if int(features["num_variants"]) > int(variants_lte):
            return False, 0.0
        score += 1.0

    # Generic targeting extensions for future DB-driven rules.
    field_equals = _json_dict(targeting.get("field_equals"))
    for key, expected in field_equals.items():
        if _clean_text(features.get(key)) != _clean_text(expected):
            return False, 0.0
        score += 2.0

    field_in = _json_dict(targeting.get("field_in"))
    for key, expected_values in field_in.items():
        allowed = _list_lower(expected_values)
        if allowed and _clean_text(features.get(key)).lower() not in allowed:
            return False, 0.0
        score += 2.0

    return True, score


def _dedupe_ranked(tips: Sequence[StudioCoachTip], limit: int) -> List[StudioCoachTip]:
    seen = set()
    out: List[StudioCoachTip] = []
    for tip in tips:
        key = (tip.title.strip().lower(), tip.body.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(tip)
        if len(out) >= max(1, limit):
            break
    return out


def _is_expired(expires_at: datetime | None, *, now: datetime) -> bool:
    if not expires_at:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def rank_studio_tips(
    req: StudioCoachRequest,
    candidates: Iterable[CandidateTip | Dict[str, Any]],
    *,
    include_fallback_when_sparse: bool = True,
) -> StudioCoachResponse:
    limit = max(1, min(int(req.limit or 3), 8))
    req = req.model_copy(update={"limit": limit})
    features = _build_feature_map(req)
    fallback = fallback_tips(req)
    now = datetime.now(timezone.utc)
    ttl_seconds = DEFAULT_TTL_SECONDS
    seed = _rotation_seed(req, ttl_seconds=ttl_seconds)

    ranked: List[StudioCoachTip] = []
    for raw in candidates:
        candidate = candidate_tip_from_row(raw) if isinstance(raw, dict) else raw
        if not candidate.is_active:
            continue
        if candidate.studio != req.studio:
            continue
        candidate_mode = _clean_text(candidate.mode)
        requested_mode = _clean_text(req.mode)
        if candidate_mode and requested_mode and candidate_mode != requested_mode:
            continue
        if _is_expired(candidate.expires_at, now=now):
            continue
        matched, targeting_score = _match_targeting(candidate.targeting or {}, features)
        if not matched:
            continue
        if not candidate.title or not candidate.body:
            continue
        tone = candidate.tone if candidate.tone in ALLOWED_TONES else "neutral"
        ranked.append(
            StudioCoachTip(
                id=candidate.id,
                title=candidate.title,
                body=candidate.body,
                tone=tone,  # type: ignore[arg-type]
                weight=float(candidate.priority) + targeting_score,
                tags=dict(candidate.tags or {}),
            )
        )

    ranked.sort(key=lambda tip: _weighted_rotation_sort_key(tip, seed=seed))
    selected = _dedupe_ranked(ranked, limit)

    source: Literal["db", "hybrid", "fallback"] = "db"
    fallback_used = False
    if include_fallback_when_sparse and len(selected) < limit:
        source = "hybrid" if selected else "fallback"
        fallback_used = True
        fallback_ranked = list(fallback)
        # Fallbacks also rotate so sparse DB contexts do not feel static.
        fallback_ranked.sort(key=lambda tip: _weighted_rotation_sort_key(tip, seed=seed))
        selected = _dedupe_ranked([*selected, *fallback_ranked], limit)

    return StudioCoachResponse(
        studio=req.studio,
        tips=selected,
        source=source,
        fallback_used=fallback_used,
        ttl_seconds=ttl_seconds,
        rotation_key=seed,
    )


async def generate_studio_tips(req: StudioCoachRequest, force_fallback: bool = False) -> StudioCoachResponse:
    """
    Backward-compatible fallback-only helper.

    API routes that have DB access should fetch active rows from
    studio_coach_tips and call rank_studio_tips(req, candidates). This helper is
    intentionally DB-free so shared code remains usable by all services.
    """
    if force_fallback:
        return rank_studio_tips(req, [], include_fallback_when_sparse=True)
    return rank_studio_tips(req, [], include_fallback_when_sparse=True)
