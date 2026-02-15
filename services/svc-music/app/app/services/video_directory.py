from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    # Reuse the canonical mapping you already defined
    from app.services.music_planning.service import canonical_platform_key as _canon_platform  # type: ignore
except Exception:
    # Ultra-safe fallback (won't break runtime if planning module changes)
    def _canon_platform(x: Any) -> str:
        s = str(x or "").strip().lower().replace("-", "_").replace(" ", "_").replace("/", "_")
        while "__" in s:
            s = s.replace("__", "_")
        return s.strip("_") or "default"


# Keep in sync with MusicPlanningService.CANONICAL_PLATFORMS (warn-only)
_CANONICAL_PLATFORMS_SET = {
    "instagram_reels",
    "instagram_feed",
    "youtube_shorts",
    "youtube_long",
    "tiktok",
    "facebook_reels",
    "whatsapp_status",
    "default",
}

_ALLOWED_ASPECTS = {"9:16", "16:9", "4:5", "1:1"}


def _as_dict(x: Any) -> Dict[str, Any]:
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        if s.startswith("{") or s.startswith("["):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
    return {}


def _as_list(x: Any) -> List[Any]:
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        if s.startswith("[") or s.startswith("{"):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, list) else []
            except Exception:
                return []
        # tolerate comma-separated
        if "," in s:
            return [p.strip() for p in s.split(",") if p.strip()]
    return []


def _snip(s: str, n: int = 900) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _stable_json(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def _hash(obj: Any) -> str:
    return hashlib.sha256(_stable_json(obj).encode("utf-8")).hexdigest()


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def _normalize_language(language_hint: Optional[str]) -> str:
    s = (language_hint or "").strip()
    return s or "en-IN"


def _normalize_exports(exports: Any) -> List[str]:
    vals = _as_list(exports)
    out: List[str] = []
    for x in vals:
        v = str(x or "").strip()
        if v:
            out.append(v)
    if not out:
        out = ["9:16"]
    # dedupe preserve order
    seen = set()
    dedup: List[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            dedup.append(v)
    return dedup


def _normalize_exports_override(exports_arg: Any) -> Tuple[bool, List[str]]:
    """
    Treat exports override as "present" only when the caller actually provided
    a non-empty value (so exports_arg="" does NOT accidentally override).
    """
    if exports_arg is None:
        return False, []
    if isinstance(exports_arg, str) and not exports_arg.strip():
        return False, []
    raw = _as_list(exports_arg)
    if not raw:
        return False, []
    return True, _normalize_exports(exports_arg)


def _merge_exports(a: List[str], b: List[str]) -> List[str]:
    # de-dupe preserve order
    out: List[str] = []
    seen = set()
    for x in (a or []) + (b or []):
        xs = str(x or "").strip()
        if xs and xs not in seen:
            seen.add(xs)
            out.append(xs)
    return out


def _extract_plan_core(music_plan: Dict[str, Any]) -> Dict[str, Any]:
    """
    Supports both envelopes:
      A) MusicPlanningService: {version, summary, plan:{...}, selected_presets, flags, trace}
      B) fallback_music_plan: {version, source, summary, brief, steps, notes}
    """
    mp = _as_dict(music_plan)
    if isinstance(mp.get("plan"), dict):
        return _as_dict(mp.get("plan"))
    return mp


def _extract_deliverable_prompts(plan_core: Dict[str, Any]) -> List[str]:
    dp = plan_core.get("deliverable_prompts")
    if isinstance(dp, list):
        out = [str(x).strip() for x in dp if str(x).strip()]
        return out

    # fallback: build from segment info
    segs = plan_core.get("segments")
    if isinstance(segs, list):
        out: List[str] = []
        for seg in segs:
            d = _as_dict(seg)
            name = str(d.get("name") or d.get("segment") or "").strip() or "segment"
            intent = str(d.get("intent") or "").strip()
            visuals = d.get("visuals")
            if isinstance(visuals, list):
                visuals_s = ", ".join([str(v).strip() for v in visuals if str(v).strip()])
            else:
                visuals_s = str(visuals or "").strip()
            prompt = " — ".join([p for p in [name, intent, visuals_s] if p])
            if prompt:
                out.append(prompt)
        return out

    return []


def _extract_segments(plan_core: Dict[str, Any]) -> List[Dict[str, Any]]:
    segs = plan_core.get("segments")
    if not isinstance(segs, list):
        return []
    out: List[Dict[str, Any]] = []
    for s in segs:
        d = _as_dict(s)
        if d:
            out.append(d)
    return out


def _extract_selected_presets(music_plan: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    sp = music_plan.get("selected_presets")
    if not isinstance(sp, dict):
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for k, v in sp.items():
        lst = _as_list(v)
        out[k] = [_as_dict(x) for x in lst if _as_dict(x)]
    return out


def _style_suffix_from_presets(selected_presets: Dict[str, List[Dict[str, Any]]]) -> str:
    """
    Deterministic, provider-agnostic suffix (no reliance on preset.content schema).
    """
    styles = selected_presets.get("style") or []
    names = [str(p.get("name") or "").strip() for p in styles if str(p.get("name") or "").strip()]
    if not names:
        return ""
    names = names[:4]
    return "Style refs: " + ", ".join(names)


def _apply_no_face(prompt: str, *, no_face: bool) -> str:
    if not no_face:
        return prompt
    guard = "No faces, no lip-sync, no identifiable people; use b-roll/abstract/cinematic scenes."
    if guard.lower() in prompt.lower():
        return prompt
    return f"{prompt}\n{guard}".strip()


def _extract_platform_targets_exports(
    *,
    music_plan: Dict[str, Any],
    plan_core: Dict[str, Any],
    exports_arg: Any,
) -> Tuple[List[str], Dict[str, List[str]], Optional[str]]:
    """
    Returns:
      (platform_targets, platform_exports_map, safe_zone)

    Priority:
      1) plan_core.deliverables[*].aspects
      2) music_plan.flags.platform_exports / platform_targets
      3) plan_core.flags.platform_exports / platform_targets
      4) exports_arg as override (if present)
      5) default -> ["9:16"]

    Canonicalizes platform keys before returning.
    """
    mp = _as_dict(music_plan)
    flags_mp = _as_dict(mp.get("flags"))
    flags_core = _as_dict(plan_core.get("flags"))

    # Deliverables map (preferred)
    deliverables = plan_core.get("deliverables")
    deliverables_map: Dict[str, Any] = deliverables if isinstance(deliverables, dict) else {}

    # targets (raw first, canonicalized later)
    targets: List[str] = []
    for k in deliverables_map.keys():
        ks = str(k or "").strip()
        if ks:
            targets.append(ks)

    if not targets:
        targets = [str(x).strip() for x in _as_list(flags_mp.get("platform_targets")) if str(x).strip()]
    if not targets:
        targets = [str(x).strip() for x in _as_list(flags_core.get("platform_targets")) if str(x).strip()]

    # safe_zone (best effort)
    safe_zone = flags_mp.get("safe_zone") or flags_core.get("safe_zone")
    safe_zone_s = str(safe_zone).strip() if safe_zone else None

    # exports override?
    has_override, exports_override = _normalize_exports_override(exports_arg)

    # exports map (raw keys first, canonicalized later)
    exports_map: Dict[str, List[str]] = {}

    # 1) deliverables.aspects
    if deliverables_map:
        for p, cfg in deliverables_map.items():
            p = str(p or "").strip()
            if not p:
                continue
            d = _as_dict(cfg)
            aspects = d.get("aspects") or d.get("exports") or d.get("aspect")
            aspects_norm = _normalize_exports(aspects)
            exports_map[p] = exports_override if has_override else aspects_norm

            # safe_zone from deliverables.caption_rules.safe_zone if present
            if not safe_zone_s:
                cap = _as_dict(d.get("caption_rules"))
                sz = cap.get("safe_zone")
                if sz:
                    safe_zone_s = str(sz).strip() or safe_zone_s

    # 2) flags platform_exports
    if not exports_map:
        pe = flags_mp.get("platform_exports")
        if isinstance(pe, dict):
            for p, aspects in pe.items():
                p = str(p or "").strip()
                if p:
                    exports_map[p] = exports_override if has_override else _normalize_exports(aspects)

    if not exports_map:
        pe = flags_core.get("platform_exports")
        if isinstance(pe, dict):
            for p, aspects in pe.items():
                p = str(p or "").strip()
                if p:
                    exports_map[p] = exports_override if has_override else _normalize_exports(aspects)

    # 3) if still nothing, default
    if not exports_map:
        exports_map = {"default": exports_override if has_override else ["9:16"]}

    # if targets empty, derive from exports_map keys
    if not targets:
        targets = list(exports_map.keys())

    # ensure every target has an exports list
    for p in targets:
        if p not in exports_map:
            exports_map[p] = exports_override if has_override else exports_map.get("default") or ["9:16"]

    # normalize targets de-dupe preserve order
    seen = set()
    targets_dedup: List[str] = []
    for p in targets:
        ps = str(p or "").strip()
        if ps and ps not in seen:
            seen.add(ps)
            targets_dedup.append(ps)

    # canonicalize targets + exports keys
    canon_targets: List[str] = []
    seen_t = set()
    for t in targets_dedup:
        ct = _canon_platform(t)
        if ct and ct not in seen_t:
            seen_t.add(ct)
            canon_targets.append(ct)

    canon_exports: Dict[str, List[str]] = {}
    for k, v in exports_map.items():
        ck = _canon_platform(k)
        if ck not in canon_exports:
            canon_exports[ck] = _normalize_exports(v)
        else:
            canon_exports[ck] = _merge_exports(canon_exports[ck], _normalize_exports(v))

    if not canon_exports:
        canon_exports = {"default": exports_override if has_override else ["9:16"]}

    if not canon_targets:
        canon_targets = list(canon_exports.keys())

    for t in canon_targets:
        if t not in canon_exports:
            canon_exports[t] = canon_exports.get("default") or ["9:16"]

    return canon_targets, canon_exports, safe_zone_s


def _platform_prompt_suffix_from_deliverables(plan_core: Dict[str, Any], platform: str) -> str:
    """
    Stable suffix to nudge downstream generation towards crop-safe composition.
    Uses plan_core.deliverables[platform].framing_notes + caption_rules.safe_zone where available.

    Robust to non-canonical deliverables keys: will match by canonical equivalence.
    """
    deliverables = plan_core.get("deliverables")
    if not isinstance(deliverables, dict) or not deliverables:
        return ""

    pcanon = _canon_platform(platform)

    # direct hits
    cfg = deliverables.get(platform) or deliverables.get(pcanon) or deliverables.get("default")

    # canonical match fallback
    if cfg is None:
        for k, v in deliverables.items():
            if _canon_platform(k) == pcanon:
                cfg = v
                break

    d = _as_dict(cfg)
    if not d:
        return ""

    framing = str(d.get("framing_notes") or "").strip()
    cap = _as_dict(d.get("caption_rules"))
    safe_zone = str(cap.get("safe_zone") or "").strip()

    parts: List[str] = []
    if framing:
        parts.append(f"Framing: {framing}")
    if safe_zone:
        parts.append(f"Captions safe-zone: {safe_zone}")
    return ("\n" + "\n".join(parts)).strip() if parts else ""


def build_clip_manifest(
    *,
    music_plan: Dict[str, Any],
    mode: str,
    language_hint: Optional[str],
    duet_layout: str,
    quality: str,
    seed: Optional[int],
    exports: Any,
    audio_duration_ms: Optional[int],
    no_face: bool,
) -> Dict[str, Any]:
    """
    Agentic “Director” output:
      - deterministic clip list derived from plan
      - platform-aware variants derived from plan.deliverables / flags
      - designed to be executed later by svc-fusion-extension

    Back-compat:
      - top-level "clips" still exists and uses a primary export list
      - new "platform_variants" includes per-platform exports + per-platform clip prompts
    """
    mp = _as_dict(music_plan)
    plan_core = _extract_plan_core(mp)

    selected_presets = _extract_selected_presets(mp)
    style_suffix = _style_suffix_from_presets(selected_presets)

    prompts = _extract_deliverable_prompts(plan_core)
    segments = _extract_segments(plan_core)

    # If no prompts exist, create a minimal deterministic set
    if not prompts:
        summary = str(mp.get("summary") or plan_core.get("summary") or "").strip() or "Music video"
        prompts = [summary, f"{summary} — verse", f"{summary} — chorus", f"{summary} — bridge", f"{summary} — outro"]

    lang = _normalize_language(language_hint)

    dur_ms = _coerce_int(audio_duration_ms, 0)
    if dur_ms <= 0:
        dur_ms = 30_000  # safe default

    # Platform-aware exports (now canonical)
    platform_targets, platform_exports_map, safe_zone = _extract_platform_targets_exports(
        music_plan=mp,
        plan_core=plan_core,
        exports_arg=exports,
    )

    # Pick a primary exports for back-compat "clips" list
    primary_platform = platform_targets[0] if platform_targets else "default"
    primary_exports = platform_exports_map.get(primary_platform) or platform_exports_map.get("default") or ["9:16"]
    primary_exports_norm = _normalize_exports(primary_exports)

    # Clip count bounded (mobile-friendly)
    max_clips = 8
    base_prompts = prompts[:max_clips]

    # Allocate durations by stable weighting across N clips
    n = len(base_prompts)
    if n == 1:
        weights = [1.0]
    else:
        weights = []
        for i in range(n):
            x = i / max(1, n - 1)
            w = 0.6 + (1.0 - abs(x - 0.5) * 2.0) * 0.8
            weights.append(w)
    wsum = sum(weights) or 1.0
    durations = [max(1500, int(round(dur_ms * (w / wsum)))) for w in weights]

    # Normalize rounding error to exact dur_ms (best effort)
    delta = dur_ms - sum(durations)
    if durations and delta != 0:
        durations[-1] = max(1500, durations[-1] + delta)

    def _build_clips_for_exports(*, exports_list: List[str], platform: str) -> List[Dict[str, Any]]:
        platform_c = _canon_platform(platform) if platform else "default"
        suffix_platform = _platform_prompt_suffix_from_deliverables(plan_core, platform=platform_c)
        clips_local: List[Dict[str, Any]] = []
        for i, p in enumerate(base_prompts):
            seg_name = None
            if i < len(segments):
                seg_name = str(segments[i].get("name") or segments[i].get("segment") or "").strip() or None

            prompt = str(p).strip()
            if style_suffix:
                prompt = f"{prompt}\n{style_suffix}".strip()
            if suffix_platform:
                prompt = f"{prompt}\n{suffix_platform}".strip()

            prompt = _apply_no_face(prompt, no_face=no_face)
            prompt = _snip(prompt, 900)

            clips_local.append(
                {
                    "clip_id": f"{platform_c}_clip_{i+1}" if platform_c else f"clip_{i+1}",
                    "segment": seg_name or f"segment_{i+1}",
                    "platform": platform_c or None,
                    "prompt": prompt,
                    "duration_ms": int(durations[i]),
                    "exports": _normalize_exports(exports_list),
                    "quality": str(quality or "standard"),
                    "language_hint": lang,
                    "duet_layout": str(duet_layout or "split_screen"),
                    "safe_zone": safe_zone,
                    "preset_refs": {
                        "style": [
                            {"id": str(x.get("id")), "name": x.get("name")}
                            for x in (selected_presets.get("style") or [])[:6]
                        ]
                    },
                }
            )
        return clips_local

    # Back-compat primary clips list
    clips_primary = _build_clips_for_exports(exports_list=primary_exports_norm, platform=primary_platform)

    # Platform variants (keys already canonical)
    platform_variants: Dict[str, Any] = {}
    for p in platform_targets:
        exps = platform_exports_map.get(p) or platform_exports_map.get("default") or ["9:16"]
        platform_variants[p] = {
            "exports": _normalize_exports(exps),
            "safe_zone": safe_zone,
            "clips": _build_clips_for_exports(exports_list=exps, platform=p),
        }

    summary_txt = str(mp.get("summary") or plan_core.get("summary") or "").strip()

    manifest = {
        "version": 2,
        "generated_at": int(time.time()),
        "source": "music_plan",
        "mode": str(mode or ""),
        "language_hint": lang,
        "audio_duration_ms": int(dur_ms),
        "no_face": bool(no_face),
        "seed": seed,
        # Back-compat
        "exports": primary_exports_norm,
        "clips": clips_primary,
        # New: platform-aware structure
        "platform_targets": platform_targets,
        "platform_exports": platform_exports_map,
        "safe_zone": safe_zone,
        "platform_variants": platform_variants,
        "summary": summary_txt,
        "hash": _hash(
            {
                "mode": mode,
                "lang": lang,
                "primary_exports": primary_exports_norm,
                "dur": dur_ms,
                "no_face": no_face,
                "platform_targets": platform_targets,
                "platform_exports": platform_exports_map,
                "clips": [
                    {"segment": c["segment"], "duration_ms": c["duration_ms"], "prompt": c["prompt"], "exports": c["exports"]}
                    for c in clips_primary
                ],
            }
        ),
    }
    return manifest


# ---------------------------------------------------------------------
# Validation helpers (warn-only, never raise)
# ---------------------------------------------------------------------
def validate_music_plan(music_plan: Any) -> List[str]:
    """
    Best-effort validation of the incoming plan/envelope before manifest build.
    Returns list of warnings. Never raises.
    """
    warnings: List[str] = []
    mp = _as_dict(music_plan)
    if not mp:
        return ["music_plan: empty or not a dict"]

    plan_core = _extract_plan_core(mp)
    if not isinstance(plan_core, dict) or not plan_core:
        warnings.append("music_plan: plan_core missing/empty")

    # deliverables keys canonical check (warn only)
    deliverables = plan_core.get("deliverables")
    if isinstance(deliverables, dict) and deliverables:
        for k in list(deliverables.keys())[:25]:
            ck = _canon_platform(k)
            if ck != k:
                warnings.append(f"music_plan.deliverables key not canonical: '{k}' -> '{ck}'")

    return warnings


def validate_manifest(manifest: Any) -> List[str]:
    """
    Validate manifest structure + canonical platform keys.
    Returns list of warnings. Never raises. Safe to call in workers/logging.
    """
    w: List[str] = []
    m = _as_dict(manifest)
    if not m:
        return ["manifest: empty or not a dict"]

    # top-level keys
    if _coerce_int(m.get("version"), 0) < 1:
        w.append("manifest.version missing/invalid")
    if not isinstance(m.get("clips"), list) or not m.get("clips"):
        w.append("manifest.clips missing/empty list")

    # canonical platform_targets
    pts = m.get("platform_targets")
    if pts is not None:
        if not isinstance(pts, list):
            w.append("manifest.platform_targets is not a list")
        else:
            for p in pts[:50]:
                ps = str(p or "").strip()
                if not ps:
                    w.append("manifest.platform_targets contains empty value")
                    continue
                cp = _canon_platform(ps)
                if cp != ps:
                    w.append(f"manifest.platform_targets not canonical: '{ps}' -> '{cp}'")
                if cp not in _CANONICAL_PLATFORMS_SET:
                    # allow future platforms; still warn to surface typos
                    w.append(f"manifest.platform_targets unknown platform key: '{cp}'")

    # platform_exports shape
    pe = m.get("platform_exports")
    if pe is not None:
        if not isinstance(pe, dict):
            w.append("manifest.platform_exports is not a dict")
        else:
            for k, v in list(pe.items())[:50]:
                ck = _canon_platform(k)
                if ck != k:
                    w.append(f"manifest.platform_exports key not canonical: '{k}' -> '{ck}'")
                exps = _normalize_exports(v)
                if not exps:
                    w.append(f"manifest.platform_exports['{k}'] empty exports list")
                for a in exps:
                    if a not in _ALLOWED_ASPECTS:
                        w.append(f"manifest.platform_exports['{k}'] has non-standard aspect '{a}'")

    # validate clips
    clips = m.get("clips")
    if isinstance(clips, list):
        for i, c in enumerate(clips[:50]):
            cd = _as_dict(c)
            if not cd:
                w.append(f"manifest.clips[{i}] not a dict")
                continue

            clip_id = str(cd.get("clip_id") or "").strip()
            if not clip_id:
                w.append(f"manifest.clips[{i}].clip_id missing")

            platform = cd.get("platform")
            if platform is not None:
                ps = str(platform).strip()
                cp = _canon_platform(ps)
                if cp != ps:
                    w.append(f"manifest.clips[{i}].platform not canonical: '{ps}' -> '{cp}'")

            dms = _coerce_int(cd.get("duration_ms"), 0)
            if dms <= 0:
                w.append(f"manifest.clips[{i}].duration_ms invalid: {cd.get('duration_ms')}")

            exports = cd.get("exports")
            if exports is None:
                w.append(f"manifest.clips[{i}].exports missing")
            else:
                exps = _normalize_exports(exports)
                for a in exps:
                    if a not in _ALLOWED_ASPECTS:
                        w.append(f"manifest.clips[{i}].exports has non-standard aspect '{a}'")

            prompt = str(cd.get("prompt") or "").strip()
            if not prompt:
                w.append(f"manifest.clips[{i}].prompt missing/empty")

    # platform_variants coherence
    pv = m.get("platform_variants")
    if pv is not None:
        if not isinstance(pv, dict):
            w.append("manifest.platform_variants is not a dict")
        else:
            for pk, cfg in list(pv.items())[:50]:
                cpk = _canon_platform(pk)
                if cpk != pk:
                    w.append(f"manifest.platform_variants key not canonical: '{pk}' -> '{cpk}'")
                dcfg = _as_dict(cfg)
                if not dcfg:
                    w.append(f"manifest.platform_variants['{pk}'] not a dict")
                    continue
                if not isinstance(dcfg.get("clips"), list) or not dcfg.get("clips"):
                    w.append(f"manifest.platform_variants['{pk}'].clips missing/empty list")

    return w