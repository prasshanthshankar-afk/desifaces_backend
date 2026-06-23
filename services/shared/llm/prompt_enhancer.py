from __future__ import annotations

import json
import logging
import os
import re
from hashlib import sha1
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("desifaces.llm.prompt_enhancer")


class PromptEnhanceAlternative(BaseModel):
    label: str
    text: str


class PromptEnhanceRequest(BaseModel):
    studio: Literal["face", "audio", "fusion"]
    mode: Optional[str] = None
    user_input: str
    locked_fields: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    locale: str = "en"
    max_alternatives: int = 3


class PromptEnhanceResponse(BaseModel):
    original_input: str
    enhanced_input: str
    alternatives: List[PromptEnhanceAlternative] = Field(default_factory=list)
    tips: List[str] = Field(default_factory=list)
    why_this_is_better: Optional[str] = None
    source: Literal["llm", "fallback"] = "fallback"
    fallback_used: bool = True
    structured: Dict[str, Any] = Field(default_factory=dict)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _friendly_label(value: Any) -> str:
    raw = _clean_text(value)
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered in {"optional", "none", "null"}:
        return ""
    return re.sub(r"\s+", " ", raw.replace("_", " ")).strip()


def _dedupe(parts: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = _clean_text(part)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _studio_hash(payload: Dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    return sha1(blob).hexdigest()[:12]


def _llm_enabled() -> bool:
    if os.getenv("DF_PROMPT_ENHANCER_DISABLE_LLM", "").lower() in {"1", "true", "yes"}:
        return False
    return bool(os.getenv("OPENAI_API_KEY"))


def _extract_response_text(response: Any) -> str:
    text = _clean_text(getattr(response, "output_text", ""))
    if text:
        return text

    output = getattr(response, "output", None) or []
    chunks: List[str] = []
    for item in output:
        for content in getattr(item, "content", []) or []:
            candidate = _clean_text(getattr(content, "text", ""))
            if candidate:
                chunks.append(candidate)
    return "\n".join(chunks).strip()


def _extract_json_block(text: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("empty_llm_response")

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("llm_json_not_found")
    return json.loads(match.group(0))


def _sanitize_alternatives(items: Any, max_alternatives: int) -> List[PromptEnhanceAlternative]:
    out: List[PromptEnhanceAlternative] = []
    if not isinstance(items, list):
        return out

    for raw in items[: max(1, max_alternatives)]:
        label = _friendly_label(getattr(raw, "label", None) if not isinstance(raw, dict) else raw.get("label"))
        text = _clean_text(getattr(raw, "text", None) if not isinstance(raw, dict) else raw.get("text"))
        if not text:
            continue
        out.append(
            PromptEnhanceAlternative(
                label=label or f"Alternative {len(out) + 1}",
                text=text,
            )
        )
    return out


def _sanitize_tips(items: Any) -> List[str]:
    if not isinstance(items, list):
        return []
    tips = [_clean_text(item) for item in items]
    return [tip for tip in tips if tip][:5]


def _build_face_prompt(
    user_input: str,
    locked_fields: Dict[str, Any],
    *,
    flavor: Literal["primary", "commercial", "natural", "premium"],
) -> str:
    mode = _clean_text(locked_fields.get("mode"))
    gender = _friendly_label(locked_fields.get("gender"))
    region = _friendly_label(locked_fields.get("region_label") or locked_fields.get("region_code"))
    zone = _friendly_label(locked_fields.get("zone_label") or locked_fields.get("zone_code"))
    context_label = _friendly_label(locked_fields.get("context_label") or locked_fields.get("context_code"))
    use_case = _friendly_label(locked_fields.get("use_case_label") or locked_fields.get("use_case_code"))
    shot_type = _friendly_label(locked_fields.get("shot_type_label") or locked_fields.get("shot_type_code"))
    aspect_ratio = _clean_text(locked_fields.get("aspect_ratio"))

    style_hint = {
        "commercial": "premium commercial photography, crisp composition, polished styling, realistic lighting",
        "natural": "natural lifestyle realism, candid energy, believable styling, soft authentic light",
        "premium": "premium editorial portrait, refined styling, cinematic realism, elegant lighting",
        "primary": "high-quality portrait, realistic lighting, clean composition, culturally respectful",
    }[flavor]

    identity_guard = (
        "same person and identity preserved from the source photo"
        if mode == "image-to-image"
        else f"{gender} presentation preserved" if gender else ""
    )

    parts = _dedupe(
        [
            user_input,
            identity_guard,
            f"authentic {region} visual cues" if region else f"authentic {zone} regional cues" if zone else "",
            f"{context_label} context" if context_label else "",
            f"{use_case} use case" if use_case else "",
            f"{shot_type} framing" if shot_type else "",
            f"optimized for {aspect_ratio} aspect ratio" if aspect_ratio else "",
            style_hint,
            "family-friendly, culturally respectful",
        ]
    )
    return ", ".join(parts)


def _build_audio_script(
    user_input: str,
    locked_fields: Dict[str, Any],
    *,
    flavor: Literal["primary", "short", "premium"],
) -> str:
    locale = _friendly_label(locked_fields.get("target_locale") or locked_fields.get("locale"))
    voice_style = _friendly_label(locked_fields.get("voice_style") or locked_fields.get("delivery_style"))
    pacing = _friendly_label(locked_fields.get("pacing"))
    tone = "confident, natural, and clear"
    if voice_style:
        tone = f"{voice_style}, natural, and clear"
    if flavor == "premium":
        tone = f"{tone}, premium and polished"
    elif flavor == "short":
        tone = f"{tone}, punchy and concise"

    script = user_input.strip()
    if script and not script.endswith((".", "!", "?")):
        script = f"{script}."

    suffix = _dedupe(
        [
            f"Write for {locale}" if locale else "",
            f"Delivery style: {tone}",
            f"Pacing: {pacing}" if pacing else "",
        ]
    )
    return " ".join([script] + suffix)


def _build_fusion_prompt(
    user_input: str,
    locked_fields: Dict[str, Any],
    *,
    flavor: Literal["primary", "social", "premium"],
) -> Dict[str, Any]:
    aspect_ratio = _clean_text(locked_fields.get("aspect_ratio"))
    camera = _friendly_label(locked_fields.get("camera_style") or locked_fields.get("camera_motion_style"))
    emotion = _friendly_label(locked_fields.get("emotion"))
    body_motion = _friendly_label(locked_fields.get("body_motion"))
    background_motion = _friendly_label(locked_fields.get("background_motion"))
    performance = _dedupe(
        [
            user_input,
            emotion if emotion else "warm confident emotional arc",
            body_motion if body_motion else "subtle upper-body motion and believable facial life",
            "premium storytelling performance" if flavor == "premium" else "social-ready presenter performance" if flavor == "social" else "expressive presenter performance",
        ]
    )

    return {
        "performance_prompt": ", ".join(performance),
        "emotion_prompt": emotion or ("elevated warmth and conviction" if flavor == "premium" else "clear warmth"),
        "body_motion_prompt": body_motion or "subtle hand movement, gentle posture shifts, natural head motion",
        "background_motion_prompt": background_motion or "soft environmental movement that supports the story without stealing focus",
        "camera_prompt": ", ".join(
            _dedupe(
                [
                    camera or ("cinematic camera language" if flavor == "premium" else "clean social framing"),
                    f"optimized for {aspect_ratio}" if aspect_ratio else "",
                ]
            )
        ),
    }


def _fallback_response(req: PromptEnhanceRequest) -> PromptEnhanceResponse:
    user_input = _clean_text(req.user_input)
    locked = req.locked_fields or {}
    context = req.context or {}

    if req.studio == "face":
        variants = int(float(locked.get("num_variants") or context.get("num_variants") or 4))
        preservation_strength = float(locked.get("preservation_strength") or 0.0)
        why = (
            f"STRICT IDENTITY LOCK {preservation_strength:.2f} - EDIT THE INPUT PHOTO ONLY: preserve the exact same human identity, same face, same gender presentation, same facial geometry, same age group, same skin tone, same eyes, same eye spacing, same lips, same jawline, same eyebrows, same nose, same cheekbones, and same facial proportions from the source image. The user prompt may change only request-only editable attributes such as styling, attire, accessories, lighting, framing, background, and composition. Do not change identity-defining features even if the prompt asks for a different face."
            if _clean_text(locked.get("mode")) == "image-to-image"
            else "The rewrite adds clearer framing, visual cues, and quality direction without changing your chosen identity inputs."
        )
        return PromptEnhanceResponse(
            original_input=user_input,
            enhanced_input=_build_face_prompt(user_input, locked, flavor="primary"),
            alternatives=[
                PromptEnhanceAlternative(label="Commercial", text=_build_face_prompt(user_input, locked, flavor="commercial")),
                PromptEnhanceAlternative(label="Natural lifestyle", text=_build_face_prompt(user_input, locked, flavor="natural")),
                PromptEnhanceAlternative(label="Premium editorial", text=_build_face_prompt(user_input, locked, flavor="premium")),
            ][: max(1, req.max_alternatives)],
            tips=_dedupe(
                [
                    "" if _friendly_label(locked.get("shot_type_label")) else "Add one framing cue like headshot, medium shot, or full-body.",
                    "" if _friendly_label(locked.get("context_label")) else "Add a clear setting so the background looks intentional instead of generic.",
                    "Run 2 to 4 variants first when you are testing a new idea to save credits." if variants > 4 else "",
                    "Keep the prompt focused on styling, lighting, attire, accessories, and scene changes. Do not change face, gender, age group, skin tone, or identity when I2I identity lock is on." if _clean_text(locked.get("mode")) == "image-to-image" else "Call out attire, lighting, and mood together for more reliable results.",
                    why,
                ]
            )[:5],
            why_this_is_better=why,
            source="fallback",
            fallback_used=True,
            structured={
                "shot_type": _friendly_label(locked.get("shot_type_label")),
                "region": _friendly_label(locked.get("region_label")),
                "context": _friendly_label(locked.get("context_label")),
                "use_case": _friendly_label(locked.get("use_case_label")),
                "aspect_ratio": _clean_text(locked.get("aspect_ratio")),
            },
        )

    if req.studio == "audio":
        return PromptEnhanceResponse(
            original_input=user_input,
            enhanced_input=_build_audio_script(user_input, locked, flavor="primary"),
            alternatives=[
                PromptEnhanceAlternative(label="Shorter", text=_build_audio_script(user_input, locked, flavor="short")),
                PromptEnhanceAlternative(label="Premium", text=_build_audio_script(user_input, locked, flavor="premium")),
            ][: max(1, req.max_alternatives)],
            tips=_dedupe(
                [
                    "Shorter sentences usually sound cleaner in TTS.",
                    "Add tone and pacing separately so the voice direction stays clear.",
                    "If you want a viral cut, ask for a punchier opening line and fewer clauses.",
                ]
            ),
            why_this_is_better="The rewrite keeps your meaning but improves delivery direction for TTS.",
            source="fallback",
            fallback_used=True,
            structured={
                "locale": _friendly_label(locked.get("target_locale") or locked.get("locale")),
                "voice_style": _friendly_label(locked.get("voice_style") or locked.get("delivery_style")),
            },
        )

    fusion_structured = _build_fusion_prompt(user_input, locked, flavor="premium")
    return PromptEnhanceResponse(
        original_input=user_input,
        enhanced_input=fusion_structured["performance_prompt"],
        alternatives=[
            PromptEnhanceAlternative(label="Social promo", text=_build_fusion_prompt(user_input, locked, flavor="social")["performance_prompt"]),
            PromptEnhanceAlternative(label="Premium cinematic", text=fusion_structured["performance_prompt"]),
        ][: max(1, req.max_alternatives)],
        tips=_dedupe(
            [
                "Keep one clear emotional arc instead of stacking many different emotions.",
                "Separate performance direction from camera direction for better motion planning.",
                "Describe environment movement separately from the person so background motion feels intentional.",
            ]
        ),
        why_this_is_better="The rewrite separates performance, motion, and camera intent so video generation has cleaner direction.",
        source="fallback",
        fallback_used=True,
        structured=fusion_structured,
    )


def _llm_system_prompt(req: PromptEnhanceRequest) -> str:
    return f"""
You are the prompt enhancement layer for desifaces.ai.

Rules:
- Preserve explicit user intent.
- Preserve locked fields exactly when they are present.
- Do not invent facts.
- Do not change demographic or identity selections.
- Return compact, production-safe JSON only.
- Keep content family-friendly and culturally respectful.
- Studio: {req.studio}

Required JSON shape:
{{
  "enhanced_input": "string",
  "alternatives": [{{"label": "string", "text": "string"}}],
  "tips": ["string"],
  "why_this_is_better": "string or null",
  "structured": {{}}
}}

For face:
- Improve visual specificity, framing, styling, lighting, and scene.
- Do not alter the selected identity.
- structured may include shot_type, context, use_case, aspect_ratio.
- Background images should not be blurred and should be intentional and culturally relevant, not generic.

For audio:
- Improve spoken rhythm, clarity, and delivery direction.
- structured may include voice_style, pacing, locale.
- If the user input is a script, keep the rewrite focused on improving delivery without changing meaning. If the user input is more of a concept or theme, feel free to enhance the script more.
- Keep tone and pacing direction separate for clearer TTS guidance.

For fusion:
- Improve performance, emotion, body motion, background motion, and camera guidance.
- structured should include performance_prompt, emotion_prompt, body_motion_prompt, background_motion_prompt, camera_prompt.
""".strip()


async def _call_openai_json(req: PromptEnhanceRequest) -> Optional[Dict[str, Any]]:
    if not _llm_enabled():
        return None

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        logger.info("OpenAI SDK unavailable; using fallback prompt enhancement.")
        return None

    model = os.getenv("DF_PROMPT_ENHANCER_MODEL", os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))
    client = OpenAI()

    user_payload = {
        "studio": req.studio,
        "mode": req.mode,
        "user_input": req.user_input,
        "locked_fields": req.locked_fields,
        "context": req.context,
        "locale": req.locale,
        "max_alternatives": req.max_alternatives,
    }

    try:
        if hasattr(client, "responses"):
            response = client.responses.create(
                model=model,
                temperature=0.4,
                input=[
                    {"role": "system", "content": _llm_system_prompt(req)},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            )
            text = _extract_response_text(response)
        else:
            completion = client.chat.completions.create(
                model=model,
                temperature=0.4,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _llm_system_prompt(req)},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            )
            text = _clean_text(completion.choices[0].message.content)
        return _extract_json_block(text)
    except Exception:
        logger.exception(
            "Prompt enhancement LLM call failed studio=%s hash=%s",
            req.studio,
            _studio_hash(user_payload),
        )
        return None


async def enhance_prompt(
    req: PromptEnhanceRequest,
    *,
    force_fallback: bool = False,
) -> PromptEnhanceResponse:
    user_input = _clean_text(req.user_input)
    if not user_input:
        raise ValueError("user_input_required")

    fallback = _fallback_response(req)
    if force_fallback:
        return fallback

    payload = await _call_openai_json(req)
    if not payload:
        return fallback

    enhanced_input = _clean_text(payload.get("enhanced_input"))
    if not enhanced_input:
        return fallback

    structured = payload.get("structured") if isinstance(payload.get("structured"), dict) else fallback.structured
    response = PromptEnhanceResponse(
        original_input=user_input,
        enhanced_input=enhanced_input,
        alternatives=_sanitize_alternatives(payload.get("alternatives"), req.max_alternatives) or fallback.alternatives,
        tips=_sanitize_tips(payload.get("tips")) or fallback.tips,
        why_this_is_better=_clean_text(payload.get("why_this_is_better")) or fallback.why_this_is_better,
        source="llm",
        fallback_used=False,
        structured=structured,
    )

    if req.studio == "face" and req.locked_fields:
        # Keep locked visual selections obvious in the final enhanced prompt.
        if _friendly_label(req.locked_fields.get("shot_type_label")) and _friendly_label(req.locked_fields.get("shot_type_label")).lower() not in response.enhanced_input.lower():
            response.enhanced_input = f"{response.enhanced_input}, {_friendly_label(req.locked_fields.get('shot_type_label'))} framing"
        if _friendly_label(req.locked_fields.get("region_label")) and _friendly_label(req.locked_fields.get("region_label")).lower() not in response.enhanced_input.lower():
            response.enhanced_input = f"{response.enhanced_input}, authentic {_friendly_label(req.locked_fields.get('region_label'))} visual cues"

    return response
