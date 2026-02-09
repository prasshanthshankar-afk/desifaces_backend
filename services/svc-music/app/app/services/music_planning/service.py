from __future__ import annotations

import os
from typing import Any, Dict, List

from .azure_openai import azure_embed_texts, azure_chat_json
from .retriever import search_presets


PRESET_TYPES: List[str] = ["story_arc", "stage", "lighting", "shot", "edit", "typography", "style"]


def _snip(s: str, n: int = 700) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def coerce_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _plan_query_text(*, title: str | None, genre: str | None, mood: str | None, language: str | None, lyrics_text: str | None, hints: Dict[str, Any]) -> str:
    # This query is what we embed for preset retrieval.
    vibe = hints.get("vibe_hint") or hints.get("style_refs") or ""
    tempo = hints.get("tempo") or ""
    return "\n".join(
        [
            f"title: {title or ''}",
            f"genre: {genre or ''}",
            f"mood: {mood or ''}",
            f"tempo: {tempo}",
            f"language: {language or ''}",
            f"vibe/style refs: {vibe}",
            "lyrics (snippet):",
            _snip(lyrics_text or "", 900),
        ]
    ).strip()


def _system_prompt() -> str:
    return (
        "You are DesiFaces Music Studio creative director. "
        "Output MUST be valid JSON (no markdown). "
        "Create a world-class music-video plan suitable for a Premiere-grade editor.\n\n"
        "Must include:\n"
        "- story_arc (logline + emotional arc)\n"
        "- segments: intro/verse/chorus/bridge/outro with intent + visuals\n"
        "- stage plan (set pieces, crowd, instruments placement)\n"
        "- lighting plan (palette, cues, movement, haze/strobes)\n"
        "- camera plan (shot types + transitions)\n"
        "- edit plan (cut rules, beat sync intensity)\n"
        "- typography plan (lyrics captions style)\n"
        "- chorus plan (crowd/backup/chorus visuals)\n"
        "- no_face_plan: if faces/lip-sync are not used, plan B-roll/abstract/city/rural/ocean sequences mapped to lyrics/story\n"
        "- deliverable_prompts: a list of text prompts for generating clips per segment.\n"
        "Keep it practical and production-ready."
    )


def _user_prompt(
    *,
    mode: str,
    title: str | None,
    genre: str | None,
    mood: str | None,
    language: str | None,
    lyrics_text: str | None,
    render_video: bool,
    no_face: bool,
    presets_by_type: Dict[str, List[Dict[str, Any]]],
) -> str:
    return (
        f"mode={mode}\n"
        f"render_video={render_video}\n"
        f"no_face={no_face}\n"
        f"title={title}\n"
        f"genre={genre}\n"
        f"mood={mood}\n"
        f"language={language}\n"
        f"lyrics_text:\n{_snip(lyrics_text or '', 1400)}\n\n"
        "Here are premiere preset candidates (use these as defaults, but you may adapt):\n"
        f"{presets_by_type}\n\n"
        "Return JSON with keys: version, summary, story_arc, segments, stage, lighting, camera, edit, typography, chorus, no_face_plan, deliverable_prompts.\n"
    )


class MusicPlanningService:
    """
    Single entrypoint: build_plan().
    Keeps planning logic OUT of music_orchestrator.py.
    """

    async def build_plan(
        self,
        *,
        mode: str,
        language: str | None,
        hints: Dict[str, Any],
        computed: Dict[str, Any],
    ) -> Dict[str, Any]:
        title = hints.get("title") or computed.get("title")
        genre = hints.get("genre") or hints.get("genre_hint")
        mood = hints.get("mood") or hints.get("vibe_hint")
        lyrics_text = computed.get("lyrics_text") or hints.get("lyrics_text") or hints.get("lyrics")

        # switches
        render_video = bool(hints.get("render_video") or hints.get("generate_video"))
        no_face = bool(hints.get("no_face") or hints.get("no_lip_sync") or hints.get("faceless_video"))

        # If user didn’t request video, still create a plan but mark it as “planning_only”
        # (mobile can still show storyboard + prompts)
        query_text = _plan_query_text(title=title, genre=genre, mood=mood, language=language, lyrics_text=lyrics_text, hints=hints)

        # 1) embed the query
        qvec = (await azure_embed_texts([query_text]))[0]

        # 2) retrieve presets by type
        presets_by_type: Dict[str, List[Dict[str, Any]]] = {}
        k = int(os.getenv("MUSIC_PRESET_TOPK", "6"))
        for pt in PRESET_TYPES:
            presets_by_type[pt] = await search_presets(query_embedding=qvec, preset_type=pt, k=k)

        # 3) ask LLM to assemble a plan
        plan = await azure_chat_json(
            _system_prompt(),
            _user_prompt(
                mode=mode,
                title=title,
                genre=genre,
                mood=mood,
                language=language,
                lyrics_text=lyrics_text,
                render_video=render_video,
                no_face=no_face,
                presets_by_type=presets_by_type,
            ),
            temperature=0.4,
            max_tokens=1400,
        )

        # 4) attach retrieval context for debugging/UI
        plan_out: Dict[str, Any] = {
            "version": coerce_int(plan.get("version"), default=1),
            "summary": plan.get("summary") or plan.get("story_arc", {}).get("logline") or "",
            "plan": plan,
            "selected_presets": {
                pt: [
                    {"id": str(p.get("id")), "name": p.get("name"), "preset_type": p.get("preset_type")}
                    for p in (presets_by_type.get(pt) or [])
                ]
                for pt in presets_by_type.keys()
            },
            "flags": {"render_video": render_video, "no_face": no_face},
        }
        return plan_out