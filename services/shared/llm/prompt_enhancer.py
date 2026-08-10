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


def _is_i2i_mode(value: Any) -> bool:
    return str(value or "").strip().lower().replace("_", "-") in {"image-to-image", "i2i", "img2img"}


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

    is_i2i = _is_i2i_mode(mode)

    if is_i2i:
        style_hint = {
            "commercial": "clean realistic product-quality photo edit, natural lighting, no beauty retouching",
            "natural": "natural source-faithful realism, believable lighting, ordinary human skin texture",
            "premium": "premium but natural photo edit, refined scene styling, no face polishing",
            "primary": "source-faithful realistic photo edit, natural lighting, no AI-polished face",
        }[flavor]
    else:
        style_hint = {
            "commercial": "premium commercial photography, crisp composition, polished styling, realistic lighting",
            "natural": "natural lifestyle realism, candid energy, believable styling, soft authentic light",
            "premium": "premium editorial portrait, refined styling, cinematic realism, elegant lighting",
            "primary": "high-quality portrait, realistic lighting, clean composition, culturally respectful",
        }[flavor]

    identity_guard = (
        "STRICT EDIT FACE IDENTITY LOCK: preserve the exact same person from the source photo, including forehead, hairline, eyes, nose, lips, cheek shape, cheek volume, cheek fullness, lower-face width, jawline, chin size, chin shape, skin tone, natural skin texture, age appearance, facial hair, and glasses or eyewear if present; do not beautify, smooth, slim, swell, reshape, de-age, or make the face look AI-generated"
        if is_i2i
        else f"{gender} presentation preserved" if gender else ""
    )

    parts = _dedupe(
        [
            user_input,
            identity_guard,
            (f"authentic {region} cues only in clothing, background, or scene, not facial traits" if region else f"authentic {zone} cues only in clothing, background, or scene, not facial traits" if zone else "") if is_i2i else (f"authentic {region} visual cues" if region else f"authentic {zone} regional cues" if zone else ""),
            f"{context_label} context" if context_label else "",
            f"{use_case} use case" if use_case else "",
            f"{shot_type} framing" if shot_type else "",
            f"optimized for {aspect_ratio} aspect ratio" if aspect_ratio else "",
            style_hint,
            "family-friendly, culturally respectful",
        ]
    )
    return ", ".join(parts)


def _normalize_audio_spoken_script(user_input: str) -> str:
    script = " ".join(_clean_text(user_input).split())

    if script and not script.endswith((".", "!", "?")):
        script = f"{script}."

    return script


def _audio_script_has_control_directives(text: str) -> bool:
    """
    Audio enhancement output must contain spoken words only.

    Locale, voice, pacing, style and TTS instructions are control
    metadata and must never leak into the script that will be spoken.
    """
    normalized = " ".join(
        _clean_text(text).lower().split()
    )

    if not normalized:
        return False

    control_markers = (
        "write for ",
        "delivery style:",
        "pacing:",
        "target locale:",
        "source locale:",
        "voice style:",
        "voice direction:",
        "tts direction:",
        "tts guidance:",
        "tts instruction:",
        "speech instruction:",
        "narration instruction:",
    )

    return any(
        marker in normalized
        for marker in control_markers
    )


def _build_audio_script(
    user_input: str,
    locked_fields: Dict[str, Any],
    *,
    flavor: Literal["primary", "short", "premium"],
) -> str:
    """
    Conservative local fallback.

    The live LLM performs semantic script enrichment. If the LLM is
    unavailable, preserve the user's factual content and make it
    speakable without fabricating facts or injecting TTS controls.
    """
    script = _normalize_audio_spoken_script(user_input)

    if flavor == "short":
        # Safely prefer the first complete sentence when a multi-sentence
        # script is available. Never invent new factual content.
        boundaries = [
            index
            for marker in (". ", "! ", "? ")
            for index in [script.find(marker)]
            if index >= 0
        ]

        if boundaries:
            end = min(boundaries)
            return script[: end + 1].strip()

    return script
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
            f"STRICT IDENTITY LOCK {preservation_strength:.2f} - EDIT THE INPUT PHOTO ONLY: preserve the exact same human identity, same real face, same gender presentation, same facial geometry, same age appearance, same skin tone, same natural skin texture, same eyes, same eye spacing, same lips, same jawline, same eyebrows, same nose, same cheek shape, same cheek volume, same cheek fullness, same lower-face width, same chin size, same chin shape, same hairline, same facial hair, and same glasses/eyewear if present. The user prompt may change only request-only non-identity attributes such as clothing, outfit, jewelry, lighting, framing, background, camera angle, composition, style, and color grade, and only when explicitly requested. Do not make cheeks puffy or swollen, do not change chin or jawline, do not smooth or polish the face, and do not make the person look AI-generated. If the user prompt conflicts with identity preservation, ignore only the conflicting identity-change portion and preserve the source identity. The user prompt is subordinate to this identity lock for image-to-image jobs."
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
                    "Keep the prompt focused on clothing, lighting, background, scene, and framing. Do not ask the enhancer to change face, cheeks, chin, jawline, glasses, facial hair, gender, age, skin tone, or identity when I2I identity lock is on." if _is_i2i_mode(locked.get("mode")) else "Call out attire, lighting, and mood together for more reliable results.",
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
            enhanced_input=_build_audio_script(
                user_input,
                locked,
                flavor="primary",
            ),
            alternatives=[
                PromptEnhanceAlternative(
                    label="Shorter",
                    text=_build_audio_script(
                        user_input,
                        locked,
                        flavor="short",
                    ),
                ),
                PromptEnhanceAlternative(
                    label="Premium",
                    text=_build_audio_script(
                        user_input,
                        locked,
                        flavor="premium",
                    ),
                ),
            ][: max(1, req.max_alternatives)],
            tips=_dedupe(
                [
                    "The script contains spoken words only; voice, locale, pacing, and delivery controls stay separate.",
                    "Live enhancement can enrich context and flow while preserving the original meaning.",
                    "Review names, numbers, dates, and factual claims before applying the rewrite.",
                ]
            ),
            why_this_is_better=(
                "The fallback preserves the spoken message without "
                "mixing TTS or delivery instructions into the script."
            ),
            source="fallback",
            fallback_used=True,
            structured={
                "source_language": "en",
                "target_locale": _friendly_label(
                    locked.get("target_locale")
                    or locked.get("locale")
                ),
                "voice_style": _friendly_label(
                    locked.get("voice_style")
                    or locked.get("delivery_style")
                ),
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
- If mode is image-to-image, Edit Face is source-faithful photo editing, not face transformation. Preserve the exact same person, including cheeks, cheek volume, chin, jawline, lower-face width, skin tone, natural skin texture, facial hair, glasses/eyewear, age appearance, and gender presentation.
- If mode is image-to-image, do not add beautifying, glamour, airbrushed, model-like, younger, slimmer, fuller-cheek, rounded-chin, or AI-polished face language.
- If region/culture is present for image-to-image, apply it only to clothing, background, scene, or styling; never reinterpret facial traits.
- structured may include shot_type, context, use_case, aspect_ratio.
- Background images should not be blurred and should be intentional and culturally relevant, not generic.

For audio:
- Treat user_input as the actual script that a human voice will speak, not as a prompt for a TTS engine.
- enhanced_input and every alternative text must contain spoken English words only.
- The selected target locale is downstream translation metadata. Do not translate the enhanced script and do not copy locale codes or locale instructions into spoken text.
- Voice, gender, tone, pacing, context, target locale, and delivery style are background metadata only. Use them to understand intent, but never write them as instructions inside the spoken script.
- Enrich the actual message: improve grammar, flow, context, transitions, natural narration, clarity, and audience engagement while preserving the user's original meaning.
- For a very short script, the primary enhancement may add reasonable general context that follows from the user's message, but it must not invent specific facts, numbers, dates, partnerships, features, claims, people, places, or events.
- Preserve names, brands, numbers, dates, URLs, product names, and other explicit factual details unless a grammatical correction is clearly needed.
- The primary enhanced_input should normally be richer and more complete than a very short original script.
- Return an alternative labeled "Shorter" that communicates the same message more concisely.
- Return an alternative labeled "Premium" that is more polished, expressive, engaging, and context-rich without fabricating facts.
- Never put phrases such as "Write for", "Delivery style:", "Pacing:", "Target locale:", "Voice style:", "TTS direction:", "Speak in", or similar generation instructions into enhanced_input or alternative text.
- structured may contain voice_style, pacing, source_language, and target_locale because structured is metadata and is not spoken.

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

    if req.studio == "audio":
        spoken_outputs = [
            response.enhanced_input,
            *[
                alternative.text
                for alternative in response.alternatives
            ],
        ]

        if any(
            _audio_script_has_control_directives(text)
            for text in spoken_outputs
        ):
            logger.warning(
                "Audio script enhancer returned control directives; "
                "using safe spoken-script fallback."
            )
            return fallback

        labels = {
            _clean_text(alternative.label).lower()
            for alternative in response.alternatives
        }

        # Keep the Audio UX contract deterministic even if an LLM returns
        # unexpected option names.
        if not {"shorter", "premium"}.issubset(labels):
            response.alternatives = fallback.alternatives

    if req.studio == "face" and req.locked_fields:
        # Keep locked visual selections obvious in the final enhanced prompt.
        is_i2i = _is_i2i_mode(req.locked_fields.get("mode") or req.mode)
        if _friendly_label(req.locked_fields.get("shot_type_label")) and _friendly_label(req.locked_fields.get("shot_type_label")).lower() not in response.enhanced_input.lower():
            response.enhanced_input = f"{response.enhanced_input}, {_friendly_label(req.locked_fields.get('shot_type_label'))} framing"
        if _friendly_label(req.locked_fields.get("region_label")) and _friendly_label(req.locked_fields.get("region_label")).lower() not in response.enhanced_input.lower():
            if is_i2i:
                response.enhanced_input = f"{response.enhanced_input}, authentic {_friendly_label(req.locked_fields.get('region_label'))} cues only in clothing, background, or scene, not facial traits"
            else:
                response.enhanced_input = f"{response.enhanced_input}, authentic {_friendly_label(req.locked_fields.get('region_label'))} visual cues"

        if is_i2i:
            i2i_guard = (
                "STRICT EDIT FACE IDENTITY LOCK: preserve the exact same person from the source photo, including cheek shape, cheek volume, cheek fullness, lower-face width, chin size, chin shape, jawline, skin tone, natural skin texture, facial hair, and glasses/eyewear if present; do not beautify, smooth, slim, swell, reshape, de-age, or make the face look AI-generated"
            )
            if "strict edit face identity lock" not in response.enhanced_input.lower():
                response.enhanced_input = f"{i2i_guard}. {response.enhanced_input}"
            for alt in response.alternatives:
                if "strict edit face identity lock" not in alt.text.lower():
                    alt.text = f"{i2i_guard}. {alt.text}"

    return response
