# services/svc-marketing/app/app/services/planning/llm_client.py
from __future__ import annotations

import json
from typing import Any, Dict

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings


class LLMClient:
    async def generate_json(self, system: str, user: str, schema_hint: str) -> Dict[str, Any]:
        raise NotImplementedError


class NoopLLMClient(LLMClient):
    async def generate_json(self, system: str, user: str, schema_hint: str) -> Dict[str, Any]:
        """
        Deterministic, safe fallback that matches the story-first schema expected by UseCasePlanner.
        - No invented metrics.
        - Respects duration constraints: 6..15.
        - Emphasizes expressive acting + hand/body movement.
        """
        fmt = "reel"
        seconds = 10
        locale = "en-IN"
        industry = "creator"
        persona = "college student"
        gender = "female"
        offer = "DM “DESIFACES” for a demo"

        try:
            obj = json.loads(user) if isinstance(user, str) and user.strip().startswith("{") else {}
            run_input = obj.get("run_input") if isinstance(obj, dict) else {}
            if isinstance(run_input, dict):
                fmt = str(run_input.get("format_hint") or fmt).strip().lower() or fmt
                try:
                    seconds = int(round(float(run_input.get("target_seconds") or seconds)))
                except Exception:
                    seconds = seconds
                seconds = max(6, min(15, seconds))

                lh = str(run_input.get("language_hint") or "").strip().lower()
                if lh.startswith("hi"):
                    locale = "hi-IN"
                elif lh.startswith("ta"):
                    locale = "ta-IN"
                elif lh.startswith("te"):
                    locale = "te-IN"
                elif lh.startswith("bn"):
                    locale = "bn-IN"
                elif lh.startswith("gu"):
                    locale = "gu-IN"
                elif lh.startswith("mr"):
                    locale = "mr-IN"
                elif lh.startswith("kn"):
                    locale = "kn-IN"
                elif lh.startswith("ml"):
                    locale = "ml-IN"
                elif lh.startswith("pa"):
                    locale = "pa-IN"

                industry = str(run_input.get("industry") or industry).strip() or industry
                persona = str(run_input.get("persona") or persona).strip() or persona
                off = str(run_input.get("offer") or "").strip()
                if off:
                    offer = off
        except Exception:
            seconds = max(6, min(15, int(seconds or 10)))

        # Beats count by format (note: duration is max 15, so keep beats tight)
        if fmt == "yt_long":
            beats_n = 10
        elif fmt in ("story", "carousel"):
            beats_n = 6
        else:
            beats_n = 7

        per = max(1, int(seconds / max(1, beats_n)))

        base_beats = [
            {
                "beat_index": 1,
                "duration_s": per,
                "on_screen_text": "I almost skipped posting…",
                "narration": f"As a {persona}, I had zero time to shoot content today.",
                "visual_prompt": f"{persona}, premium look, real-world setting, expressive face, natural hand gestures, cinematic but authentic",
                "performance_notes": "Surprised expression; raise one hand like 'wait'; quick head tilt; energetic delivery.",
            },
            {
                "beat_index": 2,
                "duration_s": per,
                "on_screen_text": "The usual way takes HOURS",
                "narration": "Script… retakes… edits… and suddenly the day is gone.",
                "visual_prompt": "Candid frustration, phone in hand, busy background, documentary vibe, premium lighting",
                "performance_notes": "Frustrated eyebrows; open palms showing 'too much work'; small shrug; glance at clock.",
            },
            {
                "beat_index": 3,
                "duration_s": per,
                "on_screen_text": "Then I tried DesiFaces.ai",
                "narration": "It created a consistent face, voice, and a talking video in minutes—ready to post.",
                "visual_prompt": "Confident smile, modern setting, clean framing, creator vibe, premium look",
                "performance_notes": "Smile; slight forward lean; point gently to camera; natural hand emphasis on key words.",
            },
            {
                "beat_index": 4,
                "duration_s": per,
                "on_screen_text": "Real vibe. My language.",
                "narration": f"It matched the vibe and language ({locale}) without me recording anything.",
                "visual_prompt": "Warm lighting, authentic environment, friendly close-up, premium portrait",
                "performance_notes": "Nod; count benefits with fingers (1–2–3); relaxed shoulders; expressive eyes.",
            },
            {
                "beat_index": 5,
                "duration_s": per,
                "on_screen_text": "I posted ON TIME",
                "narration": "More consistency. Less stress.",
                "visual_prompt": "Happy walking shot, upbeat real-life vibe, authentic environment",
                "performance_notes": "Big smile; small celebratory fist pump; swing arms naturally while walking.",
            },
            {
                "beat_index": 6,
                "duration_s": per,
                "on_screen_text": "Want a demo?",
                "narration": "DM “DESIFACES” and I’ll show you how I do it.",
                "visual_prompt": "Clean end frame, bold CTA, brand-forward, premium typography",
                "performance_notes": "Direct eye contact; point to CTA area; friendly wave; confident posture.",
            },
        ]

        # Pad/trim to beats_n
        beats = base_beats[:]
        if len(beats) > beats_n:
            beats = beats[:beats_n]
        elif len(beats) < beats_n:
            last = beats[-1]
            while len(beats) < beats_n:
                nxt = dict(last)
                nxt["beat_index"] = len(beats) + 1
                beats.append(nxt)

        story_script = {
            "schema_version": "1.0",
            "persona": {"who": persona, "gender": gender, "locale": locale, "industry": industry},
            "problem": "Creating consistent, high-quality short-form content takes too much time and effort.",
            "beats": beats,
            "video_direction": {
                "acting_style": "expressive, friendly, confident",
                "gesture_style": "natural hand gestures + upper-body movement; avoid stiffness",
                "camera_style": "waist-up framing with headroom; premium lighting",
                "energy": "high but authentic",
            },
            "cta": offer,
            "duration_s": seconds,
        }

        onscreen_lines = []
        for b in beats:
            t = str(b.get("on_screen_text") or "").strip()
            if t and t not in onscreen_lines:
                onscreen_lines.append(t)
            if len(onscreen_lines) >= 6:
                break

        voiceover_script = " ".join([str(b.get("narration") or "").strip() for b in beats]).strip()
        if len(voiceover_script) > 600:
            voiceover_script = voiceover_script[:597] + "..."

        marketing_plan = {
            "tts": {"target_locale": locale},
            "demographics": {
                "gender": gender,
                "age_range": "18-24" if "college" in persona.lower() else "25-35",
                "region": "from India",
                "attire": "context-appropriate, authentic attire",
            },
            "visual": {
                "scene": "real world, relatable",
                "background": "authentic environment, not stocky",
                "shot": "waist_up",
                "lighting": "premium natural lighting",
                "camera": "35mm portrait",
            },
            "video": {
                "emotion": "engaging",
                "motion_style": "expressive_gestures",
            },
        }

        return {
            "picked_use_case_id": "",
            "story_script": story_script,
            "hook_text": onscreen_lines[0] if onscreen_lines else "A real story in seconds",
            "onscreen_lines": onscreen_lines,
            "voiceover_script": voiceover_script,
            "music_prompt": "uplifting short loop, light percussion, 10 seconds",
            "marketing_plan": marketing_plan,
        }


class OpenAILLMClient(LLMClient):
    def __init__(self) -> None:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY required for LLM_MODE=openai")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def generate_json(self, system: str, user: str, schema_hint: str) -> Dict[str, Any]:
        base_url = (settings.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
        url = base_url + "/chat/completions"
        headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"}

        schema_hint = (schema_hint or "").strip()
        user_msg = user + ("\n\nSchema hint:\n" + schema_hint if schema_hint else "")

        payload = {
            "model": settings.OPENAI_MODEL,
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            txt = data["choices"][0]["message"]["content"]
            return json.loads(txt)


def get_llm() -> LLMClient:
    if settings.LLM_MODE == "openai":
        return OpenAILLMClient()
    return NoopLLMClient()