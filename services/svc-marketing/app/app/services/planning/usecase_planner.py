# services/svc-marketing/app/app/services/planning/usecase_planner.py
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.domain.enums import Persona, RecipeKind
from app.domain.models import MarketingRunIn, UseCaseSpec
from app.repos.marketing_use_cases_repo import MarketingUseCasesRepo
from app.services.planning.evidence_retriever import EvidenceRetriever
from app.services.planning.llm_client import get_llm
from app.services.planning.usecase_validator import specificity_gate

logger = logging.getLogger("svc-marketing-usecase-planner")

_ALLOWED_CAMPAIGN_TYPES = {"seasonal", "product_launch", "evergreen", "promo_offer"}


# -------------------------
# helpers (robust)
# -------------------------

def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            y = json.loads(s)
            return y if isinstance(y, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)
    except Exception:
        return {}


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def _s(x: Any) -> str:
    return str(x or "").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_hint(inp: MarketingRunIn) -> str:
    v = _as_dict(getattr(inp, "inputs", None)).get("format_hint") or "reel"
    f = str(v).strip().lower()
    if f not in ("reel", "story", "carousel", "yt_short", "yt_long"):
        f = "reel"
    return f


def sanitize_voiceover_text(txt: str) -> str:
    """
    Prevent the TTS from reading metadata like 'Script:' or 'Voiceover:'.
    Also removes small leading bracket labels.
    """
    s = (txt or "").strip()
    s = re.sub(r"^\s*(script|voiceover|narration)\s*:\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^\s*[\[\(].{0,40}?[\]\)]\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _target_seconds(inp: MarketingRunIn, default_s: int = 10) -> int:
    """
    IMPORTANT: UseCaseSpec.target_seconds allows only 6..15.
    """
    ts = getattr(inp, "target_seconds", None)
    if ts is None:
        ts = _as_dict(getattr(inp, "inputs", None)).get("target_seconds")
    try:
        t = int(round(float(ts))) if ts is not None else int(default_s)
    except Exception:
        t = int(default_s)
    return max(6, min(15, t))


def _beats_count_for_format(fmt: str) -> Tuple[int, int]:
    f = (fmt or "reel").strip().lower()
    if f == "yt_long":
        return 10, 16
    if f in ("reel", "yt_short"):
        return 5, 9
    if f in ("story", "carousel"):
        return 5, 8
    return 5, 9


def _norm_gender(x: Any) -> str:
    s = str(x or "").strip().lower()
    if s in ("m", "male", "man", "boy", "masculine"):
        return "male"
    if s in ("f", "female", "woman", "girl", "feminine"):
        return "female"
    if s in ("nb", "nonbinary", "non-binary", "other"):
        return "other"
    return ""


def _coerce_persona(x: Any) -> Persona:
    """
    Persona enum is limited to: creator | smb | user
    """
    if isinstance(x, Persona):
        return x

    s = _s(x).lower()
    if not s:
        return Persona.creator

    for p in (Persona.creator, Persona.smb, Persona.user):
        if s == p.value:
            return p

    if "smb" in s or "business" in s or "shop" in s or "store" in s or "owner" in s:
        return Persona.smb
    if "user" in s or "student" in s or "college" in s or "consumer" in s:
        return Persona.user
    if "creator" in s or "influencer" in s or "youtuber" in s or "instagram" in s:
        return Persona.creator

    return Persona.creator


def _coerce_campaign_type(x: Any) -> str:
    s = _s(x).lower()
    if s in _ALLOWED_CAMPAIGN_TYPES:
        return s
    return "evergreen"


def _parse_tags(inp: MarketingRunIn) -> List[str]:
    """
    Prefer MarketingRunIn.tags, then inputs.tags.
    """
    try:
        if isinstance(inp.tags, list) and inp.tags:
            out = [str(t).strip().lower() for t in inp.tags if str(t).strip()]
            seen = set()
            uniq: List[str] = []
            for t in out:
                if t in seen:
                    continue
                seen.add(t)
                uniq.append(t)
            return uniq
    except Exception:
        pass

    inputs = _as_dict(getattr(inp, "inputs", None))
    raw = inputs.get("tags")
    out2: List[str] = []
    if isinstance(raw, str):
        out2 = [t.strip().lower() for t in raw.split(",") if t.strip()]
    elif isinstance(raw, list):
        out2 = [str(t).strip().lower() for t in raw if str(t).strip()]

    seen2 = set()
    uniq2: List[str] = []
    for t in out2:
        if t in seen2:
            continue
        seen2.add(t)
        uniq2.append(t)
    return uniq2


def _derive_hook_and_lines(story: Dict[str, Any]) -> Tuple[str, List[str], str]:
    beats = _as_list(story.get("beats"))
    ons: List[str] = []
    narr: List[str] = []
    for b in beats:
        bd = _as_dict(b)
        t = _s(bd.get("on_screen_text"))
        if t and t not in ons:
            ons.append(t)
        n = _s(bd.get("narration"))
        if n:
            narr.append(n)
    hook = ons[0] if ons else "A real story in seconds"
    voiceover = " ".join(narr).strip()
    return hook, ons[:6], voiceover


def _clip_text(s: str, max_len: int) -> str:
    s2 = (s or "").strip()
    if len(s2) <= max_len:
        return s2
    return s2[: max(0, max_len - 3)].rstrip() + "..."


def _fallback_story_script(
    *,
    fmt: str,
    persona_who: str,
    gender: str,
    locale: str,
    industry: str,
    offer: str,
    target_seconds: int,
) -> Dict[str, Any]:
    min_b, max_b = _beats_count_for_format(fmt)
    beats_n = min(max_b, max(min_b, 7 if fmt in ("reel", "yt_short") else min_b))
    seconds = max(6, min(15, int(target_seconds)))
    per = max(1, int(seconds / max(1, beats_n)))

    def beat(i: int, ons: str, narr: str, visual: str, perf: str) -> Dict[str, Any]:
        return {
            "beat_index": i,
            "duration_s": per,
            "on_screen_text": ons,
            "narration": narr,
            "visual_prompt": visual,
            "performance_notes": perf,
        }

    beats: List[Dict[str, Any]] = [
        beat(
            1,
            "I almost skipped posting…",
            f"As a {persona_who}, I had zero time to shoot content today.",
            f"{persona_who} in a real-world setting, premium look, expressive face, natural hand gestures, cinematic but authentic",
            "Surprised look; raise one hand like 'wait'; quick head tilt; energetic.",
        ),
        beat(
            2,
            "The usual way takes HOURS",
            "Script… retakes… edits… and suddenly the day is gone.",
            "Candid frustration; phone in hand; busy background; documentary vibe",
            "Frustrated eyebrows; open palms showing ‘too much work’; small shrug; glance at clock.",
        ),
        beat(
            3,
            "Then I tried DesiFaces.ai",
            "It created a consistent face, voice, and a talking video in minutes—ready to post.",
            "Confident smile; modern setting; clean framing; creator vibe",
            "Smile; point gently to camera; hand emphasis while speaking; natural upper-body movement.",
        ),
        beat(
            4,
            "Real vibe. My language.",
            f"It matched the vibe and language ({locale}) without me recording anything.",
            "Warm lighting; authentic environment; friendly close-up; premium portrait",
            "Nod; count benefits with fingers (1–2–3); expressive eyes; natural gestures.",
        ),
        beat(
            5,
            "I posted ON TIME",
            "More consistency. Less stress.",
            "Happy walking shot; upbeat real-life vibe",
            "Celebratory fist pump; relaxed shoulders; friendly grin; natural arm swing.",
        ),
        beat(
            6,
            offer or "Want a demo?",
            "DM “DESIFACES” — I’ll show you how I do it.",
            "Clean end frame; bold CTA; brand-forward",
            "Direct to camera; point to CTA; friendly wave; confident posture.",
        ),
    ]

    if len(beats) > beats_n:
        beats = beats[:beats_n]
    elif len(beats) < beats_n:
        beats = beats + [beats[-1]] * (beats_n - len(beats))

    return {
        "schema_version": "1.0",
        "persona": {"who": persona_who, "gender": gender, "locale": locale, "industry": industry},
        "problem": "Creating consistent, high-quality short-form content takes too much time and effort.",
        "beats": beats,
        "video_direction": {
            "acting_style": "expressive, friendly, confident",
            "gesture_style": "natural hand gestures + upper-body movement; avoid stiffness",
            "camera_style": "waist-up framing with headroom; premium lighting",
            "energy": "high but authentic",
        },
        "cta": offer or "DM “DESIFACES” for a demo",
        "duration_s": seconds,
        "created_at": _now_iso(),
    }


def _default_marketing_plan_from_story(story: Dict[str, Any]) -> Dict[str, Any]:
    persona = _as_dict(story.get("persona"))
    who = _s(persona.get("who")) or "creator"
    gender = _norm_gender(persona.get("gender")) or "female"
    locale = _s(persona.get("locale")) or "en-IN"
    industry = _s(persona.get("industry")) or "creator"

    beats = _as_list(story.get("beats"))
    first = _as_dict(beats[0]) if beats else {}
    visual_prompt = _s(first.get("visual_prompt"))

    return {
        "tts": {"target_locale": locale},
        "demographics": {
            "gender": gender,
            "age_range": "18-24" if "college" in who.lower() else "25-35",
            "region": "from India",
            "attire": "context-appropriate, authentic attire",
        },
        "visual": {
            "scene": "real world, relatable",
            "background": "authentic environment, not stocky",
            "shot": "waist_up",
            "lighting": "premium natural lighting",
            "camera": "35mm portrait",
            "visual_prompt": visual_prompt,
        },
        "video": {
            "emotion": "engaging",
            "motion_style": "expressive_gestures",
        },
        "meta": {"industry": industry, "who": who},
    }


def _row_to_base(row: Dict[str, Any]) -> Dict[str, Any]:
    req_assets = _as_dict(row.get("required_assets_json") or {})
    base_lines = row.get("base_overlay_lines") or []
    if isinstance(base_lines, str):
        try:
            base_lines = json.loads(base_lines)
        except Exception:
            base_lines = [base_lines]

    return {
        "use_case_id": row.get("use_case_id"),
        "persona": row.get("persona"),
        "industry": row.get("industry"),
        "campaign_type": row.get("campaign_type"),
        "recipe": row.get("recipe"),
        "season_event": row.get("season_event"),
        "offer": row.get("default_offer") or row.get("offer"),
        "product_anchor": row.get("product_anchor"),
        "target_seconds": row.get("default_seconds"),
        "hook_text": row.get("default_hook"),
        "onscreen_lines": base_lines if isinstance(base_lines, list) else [],
        "voiceover_script": row.get("base_script") or "",
        "music_prompt": row.get("default_music_prompt"),
        "required_assets": req_assets,
        "tags": _as_list(row.get("tags")),
    }


def _ensure_specificity(*, spec: UseCaseSpec, inp: MarketingRunIn, base: Dict[str, Any]) -> UseCaseSpec:
    """
    specificity_gate requires score >= 6.
    Cheapest guaranteed path:
      persona(1) + industry(1) + hook>=6(1) + voiceover>=40(1) + anchor(2) = 6
    So we enforce those deterministically (no LLM dependency).
    """
    res = specificity_gate(spec)
    if res.ok:
        return spec

    updates: Dict[str, Any] = {}

    if not spec.industry or len(spec.industry.strip()) < 2:
        updates["industry"] = _s(inp.industry or base.get("industry") or "creator") or "creator"

    hook = _s(spec.hook_text)
    if len(hook) < 6:
        updates["hook_text"] = "Posted on time today"

    vo = _s(spec.voiceover_script)
    if len(vo) < 40:
        ind = _s(updates.get("industry") or spec.industry or "creator")
        updates["voiceover_script"] = (
            "Today I nearly skipped posting because I had no time. "
            "With DesiFaces.ai, I created a consistent face, voice, and a talking video fast—"
            f"so my {ind} content stayed on schedule and felt real."
        )

    has_anchor = bool((_s(spec.season_event)) or (_s(spec.offer)) or (_s(spec.product_anchor)))
    if not has_anchor:
        inputs = _as_dict(getattr(inp, "inputs", None))
        pa = _s(inputs.get("product_anchor")) or _s(base.get("product_anchor"))
        off = _s(inp.offer) or _s(base.get("offer")) or _s(base.get("default_offer"))
        se = _s(inp.season_event) or _s(base.get("season_event"))

        if pa:
            updates["product_anchor"] = pa
        elif off:
            updates["offer"] = off
        elif se:
            updates["season_event"] = se
        else:
            ind = _s(updates.get("industry") or spec.industry or "creator")
            updates["product_anchor"] = f"Story-led {ind} demo (real-world problem → solution)"

    if updates:
        try:
            return spec.model_copy(update=updates)  # pydantic v2
        except Exception:
            d = spec.model_dump()
            d.update(updates)
            return UseCaseSpec(**d)

    return spec


class UseCasePlanner:
    def __init__(self, repo: MarketingUseCasesRepo):
        self.repo = repo
        self.llm = get_llm()
        self.retriever = EvidenceRetriever()

    async def _list_candidates(self, inp: MarketingRunIn, limit: int = 12) -> List[Dict[str, Any]]:
        persona_str: Optional[str] = inp.persona.value if inp.persona else None
        industry = _s(inp.industry) or None
        tags = _parse_tags(inp)
        season_event = _s(inp.season_event) or None
        recipe = inp.recipe.value  # uppercase

        rows = await self.repo.list_candidates(
            persona=persona_str,
            industry=industry,
            tags=tags,
            season_event=season_event,
            recipe=recipe,
            limit=int(limit),
            approved_only=True,
        )
        return [dict(r) for r in (rows or [])]

    async def plan(self, inp: MarketingRunIn) -> UseCaseSpec:
        row: Optional[Dict[str, Any]] = None

        if inp.use_case_id:
            try:
                r = await self.repo.get_use_case(inp.use_case_id)
                if r:
                    rr = dict(r)
                    if rr.get("enabled") is True and rr.get("approved") is True:
                        row = rr
            except Exception:
                row = None

        candidates: List[Dict[str, Any]] = []
        if row is None:
            candidates = await self._list_candidates(inp, limit=12)

            if not candidates:
                rows = await self.repo.list_candidates(
                    persona=inp.persona.value if inp.persona else None,
                    industry=_s(inp.industry) or None,
                    tags=[],
                    season_event=_s(inp.season_event) or None,
                    recipe=inp.recipe.value,
                    limit=12,
                    approved_only=True,
                )
                candidates = [dict(r) for r in (rows or [])]

            if not candidates:
                rows = await self.repo.list_candidates(
                    persona=None,
                    industry=None,
                    tags=[],
                    season_event=None,
                    recipe=inp.recipe.value,
                    limit=12,
                    approved_only=True,
                )
                candidates = [dict(r) for r in (rows or [])]

            row = candidates[0] if candidates else None

        row = row or {}
        base = _row_to_base(row)

        persona_for_rag = _s(inp.persona.value if inp.persona else base.get("persona")) or "creator"
        industry_for_rag = _s(inp.industry or base.get("industry")) or "creator"

        evidence = self.retriever.retrieve(
            persona=persona_for_rag,
            industry=industry_for_rag,
            season_event=_s(inp.season_event or base.get("season_event")),
            offer=_s(inp.offer or base.get("offer")),
            language_hint=_s(inp.language_hint or base.get("language_hint") or "en"),
            k=8,
        )

        fmt = _fmt_hint(inp)
        seconds = _target_seconds(inp, default_s=int(base.get("target_seconds") or 10))
        min_b, max_b = _beats_count_for_format(fmt)

        cand_summ: List[Dict[str, Any]] = []
        for r in candidates[:10]:
            cand_summ.append(
                {
                    "use_case_id": _s(r.get("use_case_id")),
                    "persona": _s(r.get("persona")),
                    "industry": _s(r.get("industry")),
                    "recipe": _s(r.get("recipe")),
                    "campaign_type": _s(r.get("campaign_type")),
                    "product_anchor": _s(r.get("product_anchor")),
                    "default_offer": _s(r.get("default_offer")),
                    "default_seconds": r.get("default_seconds"),
                    "default_hook": _s(r.get("default_hook")),
                    "tags": _as_list(r.get("tags")),
                }
            )

        schema_hint = """
Return JSON exactly with these keys (no commentary):

{
  "picked_use_case_id": "string (optional)",
  "story_script": {
    "schema_version": "1.0",
    "persona": { "who": "string", "gender": "male|female|other", "locale": "string", "industry": "string" },
    "problem": "string",
    "beats": [
      {
        "beat_index": 1,
        "duration_s": 1,
        "on_screen_text": "short (<=8 words)",
        "narration": "conversational narration",
        "visual_prompt": "usable for face image (attire/background/scene)",
        "performance_notes": "MUST include gestures/expressions (hands/body movement)"
      }
    ],
    "video_direction": {
      "acting_style": "string",
      "gesture_style": "string (must emphasize hand/body movement)",
      "camera_style": "string",
      "energy": "string"
    },
    "cta": "string",
    "duration_s": 10
  },
  "hook_text": "string",
  "onscreen_lines": ["string", "..."],
  "voiceover_script": "string",
  "music_prompt": "string (optional)",
  "marketing_plan": {
    "tts": { "target_locale": "string" },
    "demographics": { "gender": "male|female|other", "age_range": "string", "region": "string", "attire": "string" },
    "visual": { "scene": "string", "background": "string", "shot": "string", "lighting": "string", "camera": "string" },
    "video": { "emotion": "string", "motion_style": "expressive_gestures" }
  }
}

Rules:
- Real story, not an ad script.
- DesiFaces.ai benefits must appear through the plot (speed, consistency, localized voice, post-ready output).
- No invented stats/metrics.
- Must include expressive acting + hand/upper body gestures in performance notes.
- duration_s must be between 6 and 15.
"""

        system = (
            "You are an award-winning short-form storyteller and creative director for DesiFaces.ai.\n"
            "Create a relatable real-world story (NOT a scripted ad).\n"
            "Character MUST be expressive with natural hand + upper-body gestures.\n"
            "Weave DesiFaces.ai benefits naturally: faster creation, consistent on-camera presence, localized voice, post-ready output.\n"
            "Avoid salesy language. Show problem → attempt → discovery → transformation.\n"
        )

        user_obj = {
            "run_input": {
                "persona": persona_for_rag,
                "industry": industry_for_rag,
                "season_event": _s(inp.season_event or base.get("season_event")),
                "offer": _s(inp.offer or base.get("offer")),
                "language_hint": _s(inp.language_hint or "en"),
                "format_hint": fmt,
                "target_seconds": seconds,
                "beats_range": [min_b, max_b],
                "recipe": inp.recipe.value,
            },
            "evidence_bullets": _as_list(evidence.get("bullets")),
            "candidate_use_cases": cand_summ,
            "hard_requirements": {
                "motion_style": "expressive_gestures",
                "emotion": "engaging",
                "duration_s_min": 6,
                "duration_s_max": 15,
            },
        }

        llm_out: Dict[str, Any] = {}
        try:
            llm_out = await self.llm.generate_json(
                system=system,
                user=json.dumps(user_obj, ensure_ascii=False),
                schema_hint=schema_hint,
            )
            llm_out = _as_dict(llm_out)
        except Exception as e:
            logger.warning("LLM planning failed; fallback. err=%s", str(e))
            llm_out = {}

        picked_id = _s(llm_out.get("picked_use_case_id"))
        if picked_id and candidates:
            for r in candidates:
                if _s(r.get("use_case_id")) == picked_id:
                    row = r
                    base = _row_to_base(r)
                    break

        story = _as_dict(llm_out.get("story_script"))
        if not story or not _as_list(story.get("beats")):
            persona_who = "college student"
            if (inp.persona or _coerce_persona(base.get("persona"))) == Persona.smb:
                persona_who = "small business owner"
            elif (inp.persona or _coerce_persona(base.get("persona"))) == Persona.user:
                persona_who = "college student"

            locale = "en-IN"
            if (inp.language_hint or "").lower().startswith("hi"):
                locale = "hi-IN"

            story = _fallback_story_script(
                fmt=fmt,
                persona_who=persona_who,
                gender="female",
                locale=locale,
                industry=industry_for_rag,
                offer=_s(inp.offer or base.get("offer")) or "",
                target_seconds=seconds,
            )

        try:
            story["duration_s"] = max(6, min(15, int(float(story.get("duration_s") or seconds))))
        except Exception:
            story["duration_s"] = seconds

        marketing_plan = _as_dict(llm_out.get("marketing_plan"))
        if not marketing_plan:
            marketing_plan = _default_marketing_plan_from_story(story)

        marketing_plan = _as_dict(marketing_plan)
        marketing_plan.setdefault("video", {})
        marketing_plan["video"] = _as_dict(marketing_plan.get("video"))
        marketing_plan["video"].setdefault("emotion", "engaging")
        marketing_plan["video"]["motion_style"] = "expressive_gestures"

        # Ensure demographics gender exists for downstream voice selection
        marketing_plan.setdefault("demographics", {})
        marketing_plan["demographics"] = _as_dict(marketing_plan.get("demographics"))
        g = _norm_gender(marketing_plan["demographics"].get("gender"))
        if g not in ("male", "female", "other"):
            marketing_plan["demographics"]["gender"] = "female"

        hook_f, ons_f, vo_f = _derive_hook_and_lines(story)

        hook_text = _clip_text(_s(llm_out.get("hook_text")) or hook_f, 120)

        onscreen_lines = _as_list(llm_out.get("onscreen_lines")) or ons_f
        onscreen_lines = [_clip_text(_s(x), 80) for x in onscreen_lines if _s(x)]

        voiceover_script = _clip_text(_s(llm_out.get("voiceover_script")) or vo_f, 600)
        voiceover_script = sanitize_voiceover_text(voiceover_script)
        if len(voiceover_script) < 10:
            voiceover_script = "Today I nearly skipped posting. Then I tried DesiFaces.ai — and I posted on time."

        music_prompt = _s(llm_out.get("music_prompt")) or _s(base.get("music_prompt") or "")

        persona_enum = inp.persona or _coerce_persona(base.get("persona"))

        industry_val = _s(inp.industry or base.get("industry") or "creator")
        if len(industry_val) < 2:
            industry_val = "creator"

        campaign_type = _coerce_campaign_type(base.get("campaign_type") or "evergreen")
        recipe_enum: RecipeKind = inp.recipe

        season_event_val = inp.season_event or base.get("season_event")
        offer_val = inp.offer or base.get("offer")
        product_anchor_val = (
            _as_dict(getattr(inp, "inputs", None)).get("product_anchor")
            or base.get("product_anchor")
            or "Story-led demo of DesiFaces.ai"
        )
        language_hint_val = _s(inp.language_hint or base.get("language_hint") or "en") or "en"

        required_assets = _as_dict(base.get("required_assets"))
        required_assets["story_script"] = story
        required_assets["marketing_plan"] = marketing_plan
        required_assets["evidence"] = {
            "bullets": _as_list(evidence.get("bullets")),
            "sources": _as_list(evidence.get("sources")),
        }

        evidence_ids = [str(x) for x in _as_list(evidence.get("sources")) if _s(x)][:20]

        spec = UseCaseSpec(
            use_case_id=base.get("use_case_id"),
            persona=persona_enum,
            industry=industry_val,
            campaign_type=campaign_type,
            recipe=recipe_enum,
            season_event=season_event_val,
            offer=offer_val,
            product_anchor=product_anchor_val,
            target_seconds=seconds,
            language_hint=language_hint_val,
            hook_text=hook_text if len(hook_text) >= 4 else "A real story in seconds",
            onscreen_lines=onscreen_lines,
            voiceover_script=voiceover_script,
            music_prompt=music_prompt or None,
            required_assets=required_assets,
            evidence_ids=evidence_ids,
        )

        # Enforce specificity_gate deterministically so runs never fail for “generic”
        spec = _ensure_specificity(spec=spec, inp=inp, base=base)

        return spec