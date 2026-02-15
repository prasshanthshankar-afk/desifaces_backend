from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .azure_openai import azure_embed_texts, azure_chat_json
from .retriever import search_presets

PRESET_TYPES: List[str] = ["story_arc", "stage", "lighting", "shot", "edit", "typography", "style"]

# Canonical platform keys (your requested contract)
CANONICAL_PLATFORMS: List[str] = [
    "instagram_reels",
    "instagram_feed",
    "youtube_shorts",
    "youtube_long",
    "tiktok",
    "facebook_reels",
    "whatsapp_status",
    # keep room for future additions without breaking older clients
    "default",
]


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


def _as_list(x: Any) -> List[Any]:
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        # JSON list support
        if s.startswith("["):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, list) else []
            except Exception:
                return []
        # tolerate comma-separated input
        if "," in s:
            return [p.strip() for p in s.split(",") if p.strip()]
        return [s]
    return []


def _normalize_str_list(x: Any) -> List[str]:
    out: List[str] = []
    for it in _as_list(x):
        s = str(it or "").strip()
        if s:
            out.append(s)
    # de-dupe preserve order
    seen = set()
    dedup: List[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            dedup.append(s)
    return dedup


def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("-", "_").replace(" ", "_").replace("/", "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def canonical_platform_key(raw: Any) -> str:
    """
    Normalizes platform names into canonical keys.

    Examples:
      "IG Reels" -> "instagram_reels"
      "youtube shorts" -> "youtube_shorts"
      "YT long" -> "youtube_long"
      "whatsapp" -> "whatsapp_status"
    """
    s = _slug(str(raw or ""))
    if not s:
        return "default"

    aliases = {
        # Instagram
        "ig_reels": "instagram_reels",
        "instagram_reel": "instagram_reels",
        "instagram_reels": "instagram_reels",
        "reels": "instagram_reels",

        "ig_feed": "instagram_feed",
        "instagram_feed": "instagram_feed",
        "instagram_post": "instagram_feed",
        "feed": "instagram_feed",

        # YouTube
        "yt_shorts": "youtube_shorts",
        "youtube_shorts": "youtube_shorts",
        "shorts": "youtube_shorts",

        "yt_long": "youtube_long",
        "youtube_long": "youtube_long",
        "youtube": "youtube_long",  # default youtube -> long
        "youtube_video": "youtube_long",

        # TikTok
        "tiktok": "tiktok",
        "tt": "tiktok",

        # Facebook
        "fb_reels": "facebook_reels",
        "facebook_reels": "facebook_reels",

        # WhatsApp
        "whatsapp": "whatsapp_status",
        "wa": "whatsapp_status",
        "wa_status": "whatsapp_status",
        "whatsapp_status": "whatsapp_status",
        "status": "whatsapp_status",

        # Default
        "default": "default",
    }

    if s in aliases:
        return aliases[s]

    # If unknown, still return a stable key (don’t drop it).
    return s


def _platform_default_aspects(platform: str) -> List[str]:
    p = canonical_platform_key(platform)
    if p in ("instagram_reels", "youtube_shorts", "tiktok", "facebook_reels", "whatsapp_status"):
        return ["9:16"]
    if p in ("instagram_feed",):
        return ["4:5", "1:1"]
    if p in ("youtube_long",):
        return ["16:9"]
    return ["9:16"]


def _resolve_platform_targets(hints: Dict[str, Any]) -> List[str]:
    pts = hints.get("platform_targets") or hints.get("platforms") or hints.get("targets") or []
    raw = _normalize_str_list(pts)
    canon = [canonical_platform_key(p) for p in raw]
    # de-dupe preserve order
    seen = set()
    out: List[str] = []
    for p in canon:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _resolve_exports(hints: Dict[str, Any], platform_targets: List[str]) -> Dict[str, List[str]]:
    """
    Returns dict(platform -> exports list). Deterministic.

    If hints.exports is present, it applies to all platforms.
    Otherwise map each platform to defaults.
    If no platforms given, return {"default": ["9:16"]} (mobile-first).
    """
    exports_hint = hints.get("exports") or hints.get("export_aspects") or hints.get("aspects")
    exps = _normalize_str_list(exports_hint)

    if not platform_targets:
        return {"default": exps or ["9:16"]}

    out: Dict[str, List[str]] = {}
    for p in platform_targets:
        cp = canonical_platform_key(p)
        out[cp] = exps or _platform_default_aspects(cp)
    return out


def _plan_query_text(
    *,
    title: str | None,
    genre: str | None,
    mood: str | None,
    language: str | None,
    lyrics_text: str | None,
    hints: Dict[str, Any],
    platform_targets: List[str],
    platform_exports: Dict[str, List[str]],
    safe_zone: Optional[str],
) -> str:
    vibe = hints.get("vibe_hint") or hints.get("style_refs") or ""
    tempo = hints.get("tempo") or ""
    return "\n".join(
        [
            f"title: {title or ''}",
            f"genre: {genre or ''}",
            f"mood: {mood or ''}",
            f"tempo: {tempo}",
            f"language: {language or ''}",
            f"platform_targets: {platform_targets}",
            f"platform_exports: {platform_exports}",
            f"safe_zone: {safe_zone or ''}",
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
        "IMPORTANT: platform keys MUST be canonical:\n"
        "- instagram_reels, youtube_shorts, tiktok, youtube_long, instagram_feed, facebook_reels, whatsapp_status\n\n"
        "Must include:\n"
        "- story_arc (logline + emotional arc)\n"
        "- segments: intro/verse/chorus/bridge/outro with intent + visuals\n"
        "- stage plan (set pieces, crowd, instruments placement)\n"
        "- lighting plan (palette, cues, movement, haze/strobes)\n"
        "- camera plan (shot types + transitions)\n"
        "- edit plan (cut rules, beat sync intensity)\n"
        "- typography plan (lyrics captions style)\n"
        "- chorus plan (crowd/backup/chorus visuals)\n"
        "- no_face_plan: if faces/lip-sync are not used, plan B-roll/abstract/cinematic scenes mapped to lyrics/story\n"
        "- deliverable_prompts: list of clip prompts per segment\n"
        "- deliverables: dict keyed by canonical platform with:\n"
        "    { aspects, duration_targets_sec, caption_rules (safe_zone), framing_notes }\n\n"
        "Platform/aspect requirements are CRITICAL:\n"
        "- 9:16 => center framing, leave safe zones for UI\n"
        "- 16:9 => wide staging, keep action within title-safe\n"
        "- 4:5 or 1:1 => crop-safe composition\n\n"
        "Keep it practical, production-ready, and consistent with the provided preset candidates."
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
    platform_targets: List[str],
    platform_exports: Dict[str, List[str]],
    safe_zone: Optional[str],
) -> str:
    return (
        f"mode={mode}\n"
        f"render_video={render_video}\n"
        f"no_face={no_face}\n"
        f"title={title}\n"
        f"genre={genre}\n"
        f"mood={mood}\n"
        f"language={language}\n"
        f"platform_targets={platform_targets}\n"
        f"platform_exports={platform_exports}\n"
        f"safe_zone={safe_zone}\n"
        f"lyrics_text:\n{_snip(lyrics_text or '', 1400)}\n\n"
        "Here are premiere preset candidates (use these as defaults, but you may adapt):\n"
        f"{presets_by_type}\n\n"
        "Return JSON with keys:\n"
        "version, summary, story_arc, segments, stage, lighting, camera, edit, typography, chorus, "
        "no_face_plan, deliverable_prompts, deliverables.\n"
    )


def _canonicalize_deliverables_keys(deliverables: Dict[str, Any]) -> Dict[str, Any]:
    """
    LLM may return keys like 'YT Shorts' or 'Instagram Reels'. Convert to canonical keys.
    If collisions occur, prefer the first-seen and merge missing fields.
    """
    if not isinstance(deliverables, dict):
        return {}

    out: Dict[str, Any] = {}
    for k, v in deliverables.items():
        ck = canonical_platform_key(k)
        if ck not in out:
            out[ck] = v
        else:
            # merge best-effort if both are dicts
            if isinstance(out[ck], dict) and isinstance(v, dict):
                merged = dict(out[ck])
                for kk, vv in v.items():
                    if kk not in merged or merged.get(kk) in (None, "", [], {}):
                        merged[kk] = vv
                out[ck] = merged
    return out


def _normalize_llm_plan(plan: Dict[str, Any], *, platform_targets: List[str], platform_exports: Dict[str, List[str]], safe_zone: Optional[str], render_video: bool) -> Dict[str, Any]:
    """
    Guardrails: normalize shape without raising.
    Ensures required top-level keys exist and are JSON-friendly.
    Also canonicalizes deliverables keys and injects defaults if missing.
    """
    plan = plan if isinstance(plan, dict) else {}

    # version/summary
    v = coerce_int(plan.get("version"), default=1)
    summary = (plan.get("summary") or "").strip()
    if not summary:
        sa = plan.get("story_arc") if isinstance(plan.get("story_arc"), dict) else {}
        summary = (sa.get("logline") or "").strip()

    # normalize segments
    segs = plan.get("segments")
    if not isinstance(segs, list):
        segs = []
    plan["segments"] = segs

    # normalize deliverable_prompts
    dp = plan.get("deliverable_prompts")
    if not isinstance(dp, list):
        dp = []
    plan["deliverable_prompts"] = dp

    # normalize deliverables (platform-aware dict)
    deliverables = plan.get("deliverables")
    if not isinstance(deliverables, dict):
        deliverables = {}
    deliverables = _canonicalize_deliverables_keys(deliverables)

    # if empty, inject deterministic defaults
    if not deliverables:
        deliverables = _fallback_deliverables(
            platform_targets=platform_targets,
            platform_exports=platform_exports,
            safe_zone=safe_zone,
            render_video=render_video,
        )

    # ensure each deliverable has an aspects list
    for p, cfg in list(deliverables.items()):
        if not isinstance(cfg, dict):
            continue
        aspects = cfg.get("aspects") or cfg.get("exports") or cfg.get("aspect")
        if not isinstance(aspects, list) or not aspects:
            cfg["aspects"] = platform_exports.get(p) or _platform_default_aspects(p)
        deliverables[p] = cfg

    plan["deliverables"] = deliverables

    plan["version"] = v
    plan["summary"] = summary or ""

    # ensure required keys exist (empty dict ok)
    for k in ("story_arc", "stage", "lighting", "camera", "edit", "typography", "chorus", "no_face_plan"):
        if k not in plan or not isinstance(plan.get(k), dict):
            plan[k] = {}

    return plan


def _fallback_deliverables(
    *,
    platform_targets: List[str],
    platform_exports: Dict[str, List[str]],
    safe_zone: Optional[str],
    render_video: bool,
) -> Dict[str, Any]:
    """
    Deterministic deliverables map for fallback plan.
    """
    if not platform_targets:
        platform_targets = ["default"]
        if "default" not in platform_exports:
            platform_exports = {"default": ["9:16"]}

    out: Dict[str, Any] = {}
    for p in platform_targets:
        p = canonical_platform_key(p)
        exps = platform_exports.get(p) or ["9:16"]
        # basic duration targets by platform class
        dur = [15, 30] if "9:16" in exps else ([60, 120] if "16:9" in exps else [30, 60])
        out[p] = {
            "aspects": exps,
            "duration_targets_sec": dur,
            "caption_rules": {
                "safe_zone": safe_zone or ("mobile_ui" if "9:16" in exps else "title_safe"),
                "notes": "Keep captions within safe zones; high contrast; avoid UI overlays.",
            },
            "framing_notes": (
                "Vertical: keep subject centered; leave top/bottom breathing room for UI."
                if "9:16" in exps
                else "Horizontal: wider staging; keep action within title-safe."
            ),
            "planning_only": not bool(render_video),
        }
    return out


def _fallback_plan(
    *,
    mode: str,
    title: str | None,
    genre: str | None,
    mood: str | None,
    language: str | None,
    render_video: bool,
    no_face: bool,
    platform_targets: List[str],
    platform_exports: Dict[str, List[str]],
    safe_zone: Optional[str],
) -> Dict[str, Any]:
    t = (title or "Untitled").strip() or "Untitled"
    g = (genre or "pop").strip() or "pop"
    m = (mood or "uplifting").strip() or "uplifting"
    lang = language or "en"

    summary = f"{t} — {g}, {m} ({lang})"

    segments = [
        {"name": "intro", "intent": "Establish vibe and setting", "visuals": ["wide establishing", "texture shots"]},
        {"name": "verse", "intent": "Build narrative and character", "visuals": ["medium shots", "movement"]},
        {"name": "chorus", "intent": "Peak energy + hooks", "visuals": ["crowd energy", "hero shots"]},
        {"name": "bridge", "intent": "Contrast / emotional lift", "visuals": ["slow motion", "close-ups"]},
        {"name": "outro", "intent": "Resolve and linger", "visuals": ["sunset / closing motif"]},
    ]

    first_platform = canonical_platform_key(platform_targets[0]) if platform_targets else "default"
    first_aspects = platform_exports.get(first_platform) or platform_exports.get("default") or ["9:16"]

    aspect_note = ""
    if "9:16" in first_aspects:
        aspect_note = "vertical 9:16, center framing, leave UI-safe margins"
    elif "16:9" in first_aspects:
        aspect_note = "horizontal 16:9, wide staging, title-safe framing"
    elif "4:5" in first_aspects:
        aspect_note = "portrait 4:5, crop-safe composition"
    elif "1:1" in first_aspects:
        aspect_note = "square 1:1, crop-safe composition"

    deliverable_prompts = [
        f"{t} music video, {g}, {m}, cinematic, premium lighting, {lang}, intro establishing shot, {aspect_note}",
        f"{t} music video, verse sequence, {g}, {m}, cinematic camera movement, {lang}, {aspect_note}",
        f"{t} chorus peak, high energy, crowd, stage lights, cinematic, {lang}, {aspect_note}",
        f"{t} bridge emotional contrast, moody lighting, close-ups, cinematic, {lang}, {aspect_note}",
        f"{t} outro resolution, warm tones, cinematic, {lang}, {aspect_note}",
    ]

    return {
        "version": 1,
        "summary": summary,
        "story_arc": {"logline": summary, "arc": ["setup", "build", "peak", "contrast", "resolve"]},
        "segments": segments,
        "stage": {"notes": "Simple stage block; expand if render_video is enabled."},
        "lighting": {"palette": ["warm", "gold", "neon accents"], "notes": "Beat-synced cues where possible."},
        "camera": {"shots": ["wide", "medium", "close"], "notes": "Cut on beat; add push-ins on chorus."},
        "edit": {"rules": ["cut on beat", "increase intensity in chorus"], "intensity": "medium"},
        "typography": {"style": "clean", "notes": "Readable captions; high contrast; respect safe zones."},
        "chorus": {"notes": "Reinforce hook with repeating motifs + crowd energy."},
        "no_face_plan": {"enabled": bool(no_face), "notes": "Use B-roll/abstract montage if faces are disabled."},
        "deliverable_prompts": deliverable_prompts,
        "deliverables": _fallback_deliverables(
            platform_targets=platform_targets,
            platform_exports=platform_exports,
            safe_zone=safe_zone,
            render_video=render_video,
        ),
        "flags": {
            "render_video": bool(render_video),
            "no_face": bool(no_face),
            "planning_only": not bool(render_video),
            "platform_targets": platform_targets,
            "platform_exports": platform_exports,
            "safe_zone": safe_zone,
        },
    }


def _make_envelope(
    *,
    plan_core: Dict[str, Any],
    presets_by_type: Dict[str, List[Dict[str, Any]]],
    render_video: bool,
    no_face: bool,
    platform_targets: List[str],
    platform_exports: Dict[str, List[str]],
    safe_zone: Optional[str],
    trace: Dict[str, Any],
) -> Dict[str, Any]:
    plan_core = plan_core if isinstance(plan_core, dict) else {}
    return {
        "version": coerce_int(plan_core.get("version"), default=1),
        "summary": (plan_core.get("summary") or "").strip(),
        "plan": plan_core,
        "selected_presets": {
            pt: [
                {"id": str(p.get("id")), "name": p.get("name"), "preset_type": p.get("preset_type")}
                for p in (presets_by_type.get(pt) or [])
            ]
            for pt in presets_by_type.keys()
        },
        "flags": {
            "render_video": bool(render_video),
            "no_face": bool(no_face),
            "platform_targets": platform_targets,
            "platform_exports": platform_exports,
            "safe_zone": safe_zone,
        },
        "trace": trace,
    }


class MusicPlanningService:
    """
    Single entrypoint: build_plan().

    Guardrails:
      - Always returns stable envelope {version,summary,plan,selected_presets,flags,trace}
      - Retrieval/LLM failures fall back deterministically (never blocks UX)
      - Platform keys are canonical (contract-safe)
    """

    async def build_plan(
        self,
        *,
        mode: str,
        language: str | None,
        hints: Dict[str, Any],
        computed: Dict[str, Any],
    ) -> Dict[str, Any]:
        t0 = time.time()

        title = hints.get("title") or computed.get("title")
        genre = hints.get("genre") or hints.get("genre_hint")
        mood = hints.get("mood") or hints.get("vibe_hint")
        lyrics_text = computed.get("lyrics_text") or hints.get("lyrics_text") or hints.get("lyrics")

        render_video = bool(hints.get("render_video") or hints.get("generate_video"))
        no_face = bool(hints.get("no_face") or hints.get("no_lip_sync") or hints.get("faceless_video"))

        platform_targets = _resolve_platform_targets(hints)
        platform_exports = _resolve_exports(hints, platform_targets)

        safe_zone = hints.get("safe_zone")
        safe_zone_s = str(safe_zone).strip() if safe_zone else None

        planner_mode = (os.getenv("MUSIC_PLANNER_MODE", "llm") or "llm").strip().lower()
        if planner_mode not in ("llm", "retrieval_only", "fallback"):
            planner_mode = "llm"

        query_text = _plan_query_text(
            title=title,
            genre=genre,
            mood=mood,
            language=language,
            lyrics_text=lyrics_text,
            hints=hints,
            platform_targets=platform_targets,
            platform_exports=platform_exports,
            safe_zone=safe_zone_s,
        )

        presets_by_type: Dict[str, List[Dict[str, Any]]] = {pt: [] for pt in PRESET_TYPES}
        trace: Dict[str, Any] = {
            "planner_mode": planner_mode,
            "platform_targets": platform_targets,
            "platform_exports": platform_exports,
            "safe_zone": safe_zone_s,
        }

        # ---- Retrieval (best effort) ----
        qvec = None
        if planner_mode in ("llm", "retrieval_only"):
            try:
                qvec = (await azure_embed_texts([query_text]))[0]
                trace["embed_ok"] = True
            except Exception as e:
                trace["embed_ok"] = False
                trace["embed_error"] = str(e)

        if qvec is not None:
            k = int(os.getenv("MUSIC_PRESET_TOPK", "6"))
            trace["preset_topk"] = k
            for pt in PRESET_TYPES:
                try:
                    presets_by_type[pt] = await search_presets(query_embedding=qvec, preset_type=pt, k=k)
                except Exception as e:
                    presets_by_type[pt] = []
                    trace[f"preset_error_{pt}"] = str(e)

        # ---- Assemble plan ----
        if planner_mode in ("fallback", "retrieval_only"):
            core = _fallback_plan(
                mode=mode,
                title=title,
                genre=genre,
                mood=mood,
                language=language,
                render_video=render_video,
                no_face=no_face,
                platform_targets=platform_targets,
                platform_exports=platform_exports,
                safe_zone=safe_zone_s,
            )
            trace["used"] = "fallback" if planner_mode == "fallback" else "retrieval_only_fallback_core"
            trace["latency_ms"] = int((time.time() - t0) * 1000)
            return _make_envelope(
                plan_core=core,
                presets_by_type=presets_by_type,
                render_video=render_video,
                no_face=no_face,
                platform_targets=platform_targets,
                platform_exports=platform_exports,
                safe_zone=safe_zone_s,
                trace=trace,
            )

        # planner_mode == "llm"
        try:
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
                    platform_targets=platform_targets,
                    platform_exports=platform_exports,
                    safe_zone=safe_zone_s,
                ),
                temperature=0.4,
                max_tokens=1400,
            )

            plan_core = _normalize_llm_plan(
                plan if isinstance(plan, dict) else {},
                platform_targets=platform_targets,
                platform_exports=platform_exports,
                safe_zone=safe_zone_s,
                render_video=render_video,
            )

            trace["used"] = "llm"
            trace["latency_ms"] = int((time.time() - t0) * 1000)
            return _make_envelope(
                plan_core=plan_core,
                presets_by_type=presets_by_type,
                render_video=render_video,
                no_face=no_face,
                platform_targets=platform_targets,
                platform_exports=platform_exports,
                safe_zone=safe_zone_s,
                trace=trace,
            )
        except Exception as e:
            core = _fallback_plan(
                mode=mode,
                title=title,
                genre=genre,
                mood=mood,
                language=language,
                render_video=render_video,
                no_face=no_face,
                platform_targets=platform_targets,
                platform_exports=platform_exports,
                safe_zone=safe_zone_s,
            )
            trace["used"] = "fallback_on_llm_error"
            trace["llm_error"] = str(e)
            trace["latency_ms"] = int((time.time() - t0) * 1000)
            return _make_envelope(
                plan_core=core,
                presets_by_type=presets_by_type,
                render_video=render_video,
                no_face=no_face,
                platform_targets=platform_targets,
                platform_exports=platform_exports,
                safe_zone=safe_zone_s,
                trace=trace,
            )