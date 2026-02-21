from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.repos.music_style_presets_repo import MusicStylePresetsRepo
from app.services.shot_cookbook_generator import generate_shot_cookbook_from_preset_row

JsonDict = Dict[str, Any]

# Fallback catalog ONLY when DB is missing tags/rows.
_PRESET_CATALOG: List[Dict[str, Any]] = [
    {"name": "Monsoon Journey (Rain + Roads)", "tags": ["monsoon", "rain", "roads", "broll"]},
    {"name": "Rural Harvest Warmth", "tags": ["rural", "village", "warm", "broll"]},
    {"name": "Urban Neon Hustle Peak", "tags": ["urban", "city", "neon", "stage"]},
    {"name": "Festival Burst (Colors + Fireworks)", "tags": ["festival", "colors", "fireworks", "stage"]},
    {"name": "Temple Devotional Serenity", "tags": ["temple", "devotional", "serene", "broll"]},
    {"name": "Goa Fishing Life (B-roll Story)", "tags": ["goa", "ocean", "broll", "no_face"]},
    {"name": "Wedding Sangeet Glam", "tags": ["wedding", "sangeet", "glam", "stage"]},
    {"name": "Street Food Night Market", "tags": ["street_food", "night_market", "urban", "broll"]},
    {"name": "Patriotic India Montage (Landscapes)", "tags": ["patriotic", "landscapes", "broll", "no_face"]},
    {"name": "Epic Trailer Montage (No Faces)", "tags": ["epic", "trailer", "no_face", "broll"]},
    {"name": "Minimal Lyric Video (Clean)", "tags": ["lyrics", "minimal", "no_face"]},
    {"name": "Kinetic Lyric Video (Bold Punch)", "tags": ["lyrics", "kinetic", "no_face"]},
    {"name": "Abstract Visualizer (Particles)", "tags": ["abstract", "visualizer", "no_face"]},
]

_DEFAULT_PRESET_FALLBACK = "Urban Neon Hustle Peak"

_SCENE_PRIMARY_PRIORITY = [
    "festival",
    "wedding",
    "devotional",
    "temple",
    "patriotic",
    "rural",
    "village",
    "goa",
    "desert",
    "heritage",
    "himalayan",
    "city",
    "urban",
    "neon",
    "stage",
    "broll",
    "lyrics",
    "abstract",
]


def _as_dict(x: Any) -> JsonDict:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, list) else []
            except Exception:
                return []
        return [s]
    return []


def _as_str(x: Any) -> str:
    return str(x).strip() if x is not None else ""


def _coerce_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def _coerce_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _norm_section(name: str) -> str:
    n = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if n in ("prechorus", "pre_chorus", "build"):
        return "pre_chorus"
    if n in ("hook", "drop", "refrain", "chorus2", "chorus_2"):
        return "chorus"
    if n in ("breakdown", "interlude"):
        return "bridge"
    if not n:
        return "verse"
    return n


def _norm_tag(t: Any) -> str:
    s = str(t).strip().lower().replace("-", "_").replace(" ", "_")
    return s


def _as_list_tags(x: Any) -> List[str]:
    raw = _as_list(x)
    out: List[str] = []
    for t in raw:
        nt = _norm_tag(t)
        if nt:
            out.append(nt)
    return out


def _dedupe_preserve_order(xs: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in xs:
        if x and x not in seen:
            out.append(x)
            seen.add(x)
    return out


def _preset_row_from_catalog(preset_name: str) -> Optional[Dict[str, Any]]:
    pn = (preset_name or "").strip()
    for r in _PRESET_CATALOG:
        if str(r.get("name") or "").strip() == pn:
            return r
    return None


def _extract_selected_preset_name(input_obj: JsonDict) -> Tuple[str, str]:
    """
    Preset precedence (to ensure DB-driven choice from orchestrator is honored):
      1) style.preset_name / input.preset_name (explicit UI)
      2) computed.preset_name (selected earlier from DB)
      3) computed.preset_selection.preset_name
      4) "" (caller will tag-resolve fallback)
    Returns: (preset_name, source)
    """
    style = _as_dict(input_obj.get("style"))
    explicit = _as_str(style.get("preset_name") or input_obj.get("preset_name"))
    if explicit:
        return explicit, "explicit"

    computed = _as_dict(input_obj.get("computed"))
    cpn = _as_str(computed.get("preset_name"))
    if cpn:
        return cpn, "computed"

    sel = _as_dict(computed.get("preset_selection"))
    spn = _as_str(sel.get("preset_name"))
    if spn:
        return spn, "computed_selection"

    return "", "none"


def _extract_tags_used(input_obj: JsonDict) -> List[str]:
    """
    Tags precedence:
      - explicit style.tags / provider_hints.tags / provider_hints.scene_tags
      - computed.scene_primary_tag / computed.scene_secondary_tags / computed.preset_tags_used
      - music_plan tags (mp.tags + mp.brief.tags/style_tags)
    """
    computed = _as_dict(input_obj.get("computed"))
    mp = _as_dict(computed.get("music_plan"))
    brief = _as_dict(mp.get("brief"))

    style = _as_dict(input_obj.get("style"))
    hints = _as_dict(input_obj.get("provider_hints"))

    tags: List[str] = []

    tags += _as_list_tags(style.get("tags"))
    tags += _as_list_tags(hints.get("tags"))
    tags += _as_list_tags(hints.get("scene_tags"))

    # computed scene / preset tags (these come from DB-driven preset selection step)
    st = _as_str(computed.get("scene_primary_tag"))
    if st:
        tags += [_norm_tag(st)]
    tags += _as_list_tags(computed.get("scene_secondary_tags"))
    tags += _as_list_tags(computed.get("preset_tags_used"))

    tags += _as_list_tags(mp.get("tags"))
    tags += _as_list_tags(brief.get("tags"))
    tags += _as_list_tags(brief.get("style_tags"))

    return _dedupe_preserve_order([_norm_tag(t) for t in tags if _norm_tag(t)])


def _resolve_scene_tags(tags_used: List[str], preset_tags: List[str]) -> Tuple[str, List[str], bool]:
    tags_all = _dedupe_preserve_order([*tags_used, *preset_tags])
    no_face = ("no_face" in tags_all) or ("nohuman" in tags_all) or ("no_human" in tags_all)

    if not tags_all:
        return ("stage", [], no_face)

    primary = None
    for p in _SCENE_PRIMARY_PRIORITY:
        if p in tags_all:
            primary = p
            break
    if not primary:
        primary = tags_all[0]

    secondary = [t for t in tags_all if t != primary]
    secondary = secondary[:6]
    return (primary, secondary, no_face)


def _resolve_preset_name_from_tags(tags_used: List[str]) -> str:
    """
    Deterministic preset resolver using fallback catalog:
      - score by overlap count with catalog tags
      - tie-break by catalog order (stable)
      - fallback to _DEFAULT_PRESET_FALLBACK
    """
    if not tags_used:
        return _DEFAULT_PRESET_FALLBACK

    best_name = _DEFAULT_PRESET_FALLBACK
    best_score = -1

    tagset = set(tags_used)
    for r in _PRESET_CATALOG:
        rtags = [str(x).strip().lower().replace("-", "_").replace(" ", "_") for x in (r.get("tags") or [])]
        score = len(tagset.intersection(set(rtags)))
        if score > best_score:
            best_score = score
            best_name = str(r.get("name") or _DEFAULT_PRESET_FALLBACK)
    return best_name or _DEFAULT_PRESET_FALLBACK


def _default_exports() -> List[JsonDict]:
    return [
        {"name": "16:9", "w": 1920, "h": 1080},
        {"name": "9:16", "w": 1080, "h": 1920},
        {"name": "1:1", "w": 1080, "h": 1080},
    ]


def _hmac_hex(key: str, msg: str) -> str:
    return hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _quantize_to_bar(beats: float, beats_per_bar: int) -> int:
    q = int(round(beats / beats_per_bar) * beats_per_bar)
    return max(beats_per_bar, q)


def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def _extract_preset_tags_from_db_row(row: Optional[Dict[str, Any]]) -> List[str]:
    """
    Authoritative tags from DB preset row (public.music_style_presets).
    Uses columns:
      tags, scene_primary_tag, scene_secondary_tags, mood_tag, energy_tag, face_mode, grade
    Also looks into shot_cookbook_json for additional tags.
    """
    if not isinstance(row, dict) or not row:
        return []

    tags: List[str] = []

    # 1) explicit tags column (likely text[] or jsonb-ish)
    tags += _as_list_tags(row.get("tags"))

    # 2) scene tags
    pt = _as_str(row.get("scene_primary_tag"))
    if pt:
        tags.append(_norm_tag(pt))

    tags += _as_list_tags(row.get("scene_secondary_tags"))

    # 3) mood/energy (useful for motion + b-roll tone)
    mt = _as_str(row.get("mood_tag"))
    if mt:
        tags.append(_norm_tag(mt))

    et = _as_str(row.get("energy_tag"))
    if et:
        tags.append(_norm_tag(et))

    # 4) face_mode / grade (often used as constraints)
    fm = _as_str(row.get("face_mode"))
    if fm:
        tags.append(_norm_tag(fm))

    gr = _as_str(row.get("grade"))
    if gr:
        tags.append(_norm_tag(gr))

    # 5) cookbook tags if present
    cb = row.get("shot_cookbook_json")
    if isinstance(cb, dict):
        tags += _as_list_tags(cb.get("tags"))
        scene = _as_dict(cb.get("scene"))
        pt2 = _as_str(scene.get("primary_tag"))
        if pt2:
            tags.append(_norm_tag(pt2))
        tags += _as_list_tags(scene.get("secondary_tags"))
        tags += _as_list_tags(scene.get("tags"))

    return _dedupe_preserve_order([_norm_tag(t) for t in tags if _norm_tag(t)])


@dataclass(frozen=True)
class SectionRange:
    name: str
    start_beat: int
    end_beat: int

    @property
    def beats(self) -> int:
        return max(0, self.end_beat - self.start_beat)


class ClipManifestService:
    """
    Music-driven clip durations:
      - Cookbook templates define duration_beats_default
      - Runtime maps templates to song sections (music_plan if present)
      - Assigns beat lengths by section pace + clamps to cinematic min/max seconds
      - Converts beats -> seconds, snaps to bars, stores start/end beat + seconds
    """

    def __init__(self) -> None:
        self._presets = MusicStylePresetsRepo()

    async def _load_or_self_heal_cookbook(self, *, preset_name: str, preset_row: Optional[Dict[str, Any]] = None) -> JsonDict:
        row = preset_row if isinstance(preset_row, dict) else await self._presets.get_by_name(name=preset_name)
        if not row:
            return generate_shot_cookbook_from_preset_row(preset={"name": preset_name}, cookbook_version=1)

        v = _coerce_int(row.get("shot_cookbook_version"), 0)
        cb = row.get("shot_cookbook_json")
        if v >= 1 and isinstance(cb, dict) and isinstance(cb.get("template_clips"), list) and cb["template_clips"]:
            return cb

        cookbook = generate_shot_cookbook_from_preset_row(preset=row, cookbook_version=1)
        try:
            await self._presets.update_shot_cookbook(
                preset_id=row["id"],
                cookbook_version=1,
                cookbook_json=cookbook,
            )
        except Exception:
            pass

        return cookbook

    def _get_bpm_and_bpb(self, *, input_obj: JsonDict) -> Tuple[float, int]:
        computed = _as_dict(input_obj.get("computed"))
        mp = _as_dict(computed.get("music_plan"))
        ap = _as_dict(computed.get("audio_probe"))

        bpm_raw = mp.get("bpm") if mp.get("bpm") is not None else ap.get("bpm")
        bpb_raw = mp.get("beats_per_bar") if mp.get("beats_per_bar") is not None else ap.get("beats_per_bar")

        bpm_f = _coerce_float(bpm_raw, 0.0)
        if bpm_f <= 0:
            bpm_f = 120.0

        bpb_i = _coerce_int(bpb_raw, 0)
        if bpb_i <= 0:
            bpb_i = 4

        bpm_f = max(40.0, min(220.0, float(bpm_f)))
        bpb_i = max(2, min(12, int(bpb_i)))
        return bpm_f, bpb_i

    def _get_duration_sec(self, *, input_obj: JsonDict) -> float:
        computed = _as_dict(input_obj.get("computed"))
        ap = _as_dict(computed.get("audio_probe"))
        dur = ap.get("duration_sec")
        if isinstance(dur, (int, float)) and dur and dur > 0:
            return float(dur)

        dur2 = computed.get("track_duration_sec") or input_obj.get("track_duration_sec")
        if isinstance(dur2, (int, float)) and dur2 and dur2 > 0:
            return float(dur2)

        return 60.0

    def _sections_from_music_plan(
        self,
        *,
        input_obj: JsonDict,
        total_beats: int,
        beats_per_bar: int,
        beats_per_sec: float,
    ) -> Optional[List[SectionRange]]:
        computed = _as_dict(input_obj.get("computed"))
        mp = _as_dict(computed.get("music_plan"))
        sections = mp.get("sections")
        if not isinstance(sections, list) or not sections:
            return None

        raw: List[SectionRange] = []
        for s in sections:
            if not isinstance(s, dict):
                continue

            nm = _norm_section(str(s.get("name") or ""))

            sb: Optional[int] = None
            eb: Optional[int] = None

            if isinstance(s.get("start_beat"), (int, float)) and isinstance(s.get("end_beat"), (int, float)):
                sb = int(round(float(s["start_beat"])))
                eb = int(round(float(s["end_beat"])))
            elif isinstance(s.get("bars"), list) and len(s["bars"]) == 2:
                sb = int(round(float(s["bars"][0]) * beats_per_bar))
                eb = int(round(float(s["bars"][1]) * beats_per_bar))
            elif isinstance(s.get("start_sec"), (int, float)) and isinstance(s.get("end_sec"), (int, float)):
                sb = int(round(float(s["start_sec"]) * beats_per_sec))
                eb = int(round(float(s["end_sec"]) * beats_per_sec))

            if sb is None or eb is None:
                continue

            sb = _clamp_int(sb, 0, total_beats)
            eb = _clamp_int(eb, 0, total_beats)
            if eb <= sb:
                continue

            raw.append(SectionRange(nm, sb, eb))

        if not raw:
            return None

        raw.sort(key=lambda x: x.start_beat)

        out: List[SectionRange] = []
        cur = 0
        for r in raw:
            if r.start_beat > cur:
                out.append(SectionRange("verse", cur, r.start_beat))
            sb = max(cur, r.start_beat)
            eb = max(sb, r.end_beat)
            if eb > sb:
                out.append(SectionRange(r.name, sb, eb))
                cur = eb

        if cur < total_beats:
            out.append(SectionRange("outro", cur, total_beats))

        cleaned: List[SectionRange] = []
        for r in out:
            sb = _clamp_int(r.start_beat, 0, total_beats)
            eb = _clamp_int(r.end_beat, 0, total_beats)
            if eb <= sb:
                continue
            if cleaned and sb < cleaned[-1].end_beat:
                sb = cleaned[-1].end_beat
            if eb <= sb:
                continue
            cleaned.append(SectionRange(r.name, sb, eb))

        return cleaned or None

    def _fallback_sections(self, total_beats: int) -> List[SectionRange]:
        names = ["intro", "verse", "pre_chorus", "chorus", "bridge", "chorus", "outro"]
        weights = [0.10, 0.35, 0.10, 0.25, 0.10, 0.05, 0.05]
        beats = [int(round(total_beats * w)) for w in weights]
        beats[-1] += (total_beats - sum(beats))

        out: List[SectionRange] = []
        cur = 0
        for nm, b in zip(names, beats):
            b = max(4, b)
            out.append(SectionRange(nm, cur, min(total_beats, cur + b)))
            cur = out[-1].end_beat
        if out and out[-1].end_beat < total_beats:
            out[-1] = SectionRange(out[-1].name, out[-1].start_beat, total_beats)
        return out

    def _beats_limits(self, *, bpm: float, min_sec: float, max_sec: float, beats_per_bar: int) -> Tuple[int, int]:
        beats_per_sec = bpm / 60.0
        min_beats = int(math.ceil((min_sec * beats_per_sec) / beats_per_bar) * beats_per_bar)
        max_beats = int(math.floor((max_sec * beats_per_sec) / beats_per_bar) * beats_per_bar)
        min_beats = max(beats_per_bar, min_beats)
        max_beats = max(min_beats, max_beats)
        return min_beats, max_beats

    def _section_for_hint(self, sections: List[SectionRange], hint: str) -> SectionRange:
        h = _norm_section(hint)
        for s in sections:
            if s.name == h:
                return s
        if h == "pre_chorus":
            for s in sections:
                if s.name == "verse":
                    return s
        if h == "bridge":
            for s in sections:
                if s.name == "verse":
                    return s
        return sections[min(len(sections) - 1, 1)]

    def _desired_clip_count(self, *, duration_sec: float) -> int:
        avg = 4.0
        n = int(round(max(1.0, float(duration_sec or 60.0)) / avg))
        return _clamp_int(n, 6, 16)

    async def build_manifest(
        self,
        *,
        music_video_job_id: UUID,
        project_id: UUID,
        input_json: Any,
    ) -> JsonDict:
        input_obj = _as_dict(input_json)

        # ---------------------------
        # Preset selection (DB-driven orchestrator must win)
        # ---------------------------
        tags_used = _extract_tags_used(input_obj)

        preset_name, preset_source = _extract_selected_preset_name(input_obj)
        if not preset_name:
            preset_name = _resolve_preset_name_from_tags(tags_used)
            preset_source = "tags_fallback"

        preset_name = _as_str(preset_name) or _DEFAULT_PRESET_FALLBACK

        # Load preset row from DB (authoritative) to pull tags & cookbook
        preset_row = await self._presets.get_by_name(name=preset_name)

        preset_db_tags = _extract_preset_tags_from_db_row(preset_row)
        catalog_row = _preset_row_from_catalog(preset_name) or {}
        preset_catalog_tags = [_norm_tag(t) for t in (catalog_row.get("tags") or []) if _norm_tag(t)]

        # Prefer DB tags when available; otherwise fallback catalog tags
        preset_tags = preset_db_tags or preset_catalog_tags

        # If caller provided no tags, fall back to preset tags (deterministic)
        if not tags_used and preset_tags:
            tags_used = preset_tags[:]

        primary_tag, secondary_tags, no_face = _resolve_scene_tags(tags_used, preset_tags)

        cookbook = await self._load_or_self_heal_cookbook(preset_name=preset_name, preset_row=preset_row)

        bpm, beats_per_bar = self._get_bpm_and_bpb(input_obj=input_obj)
        duration_sec = self._get_duration_sec(input_obj=input_obj)

        beats_per_sec = bpm / 60.0
        total_beats_raw = max(beats_per_bar * 8, int(round(duration_sec * beats_per_sec)))
        total_beats = _quantize_to_bar(total_beats_raw, beats_per_bar)

        edit = _as_dict(cookbook.get("edit_defaults"))
        sync = _as_dict(edit.get("audio_sync"))

        min_sec = float(sync.get("min_shot_len_sec") or 2.5)
        max_sec = float(sync.get("max_shot_len_sec") or 6.0)
        min_beats, max_beats = self._beats_limits(
            bpm=bpm,
            min_sec=min_sec,
            max_sec=max_sec,
            beats_per_bar=beats_per_bar,
        )

        sections = self._sections_from_music_plan(
            input_obj=input_obj,
            total_beats=total_beats,
            beats_per_bar=beats_per_bar,
            beats_per_sec=beats_per_sec,
        ) or self._fallback_sections(total_beats)

        templates = cookbook.get("template_clips") or []
        if not isinstance(templates, list):
            templates = []
        templates = [t for t in templates if isinstance(t, dict)]

        desired = self._desired_clip_count(duration_sec=duration_sec)
        if templates:
            if len(templates) >= desired:
                templates = templates[:desired]
            else:
                base = list(templates)
                while len(templates) < desired:
                    templates.append(base[len(templates) % len(base)])
        else:
            templates = []
            sec_names = [s.name for s in sections]
            while len(templates) < desired:
                nm = sec_names[len(templates) % len(sec_names)]
                templates.append(
                    {
                        "template_id": f"fallback_{len(templates)+1}",
                        "section_hint": nm,
                        "role": "broll",
                        "duration_beats_default": 16,
                        "prompt_hints": [nm],
                    }
                )

        cursors = {s.name: s.start_beat for s in sections}
        job_seed = _hmac_hex(str(project_id), f"{music_video_job_id}:{preset_name}")[:32]

        clips: List[JsonDict] = []

        def add_clip(*, idx: int, t: JsonDict, sec_range: SectionRange, cur: int, end_beat: int) -> None:
            start_sec = cur / beats_per_sec
            end_sec = end_beat / beats_per_sec
            clip_id = f"c{idx:02d}"
            clip_seed = _hmac_hex(job_seed, f"{clip_id}:{t.get('template_id') or ''}")[:32]
            clips.append(
                {
                    "clip_id": clip_id,
                    "template_id": t.get("template_id"),
                    "section": sec_range.name,
                    "role": t.get("role") or "broll",
                    "start_beat": cur,
                    "end_beat": end_beat,
                    "duration_beats": max(0, end_beat - cur),
                    "start_sec": float(start_sec),
                    "end_sec": float(end_sec),
                    "duration_sec": float(max(0.0, end_sec - start_sec)),
                    "clip_seed": clip_seed,
                    "video": _as_dict(t.get("video")),
                    "camera": _as_dict(t.get("camera")),
                    "prompt_hints": _as_list(t.get("prompt_hints")),
                }
            )

        for idx, t in enumerate(templates, start=1):
            sec_hint = str(t.get("section_hint") or "verse")
            sec_range = self._section_for_hint(sections, sec_hint)

            cur = cursors.get(sec_range.name, sec_range.start_beat)
            if cur >= sec_range.end_beat:
                alt = None
                for s in sections:
                    c2 = cursors.get(s.name, s.start_beat)
                    if c2 < s.end_beat:
                        alt = s
                        break
                if not alt:
                    break
                sec_range = alt
                cur = cursors.get(sec_range.name, sec_range.start_beat)

            beats_default = _coerce_int(t.get("duration_beats_default"), 16)

            sec_name = sec_range.name
            if sec_name == "chorus":
                beats_target = int(round(beats_default * 0.85))
            elif sec_name == "pre_chorus":
                beats_target = int(round(beats_default * 0.90))
            elif sec_name == "bridge":
                beats_target = int(round(beats_default * 1.15))
            else:
                beats_target = beats_default

            beats_target = _quantize_to_bar(beats_target, beats_per_bar)
            beats_target = _clamp_int(beats_target, min_beats, max_beats)

            end_beat = min(sec_range.end_beat, cur + beats_target)
            if end_beat - cur < beats_per_bar:
                end_beat = min(sec_range.end_beat, cur + beats_per_bar)
            if end_beat <= cur:
                continue

            add_clip(idx=idx, t=t, sec_range=sec_range, cur=cur, end_beat=end_beat)
            cursors[sec_range.name] = end_beat

        # Coverage self-heal (unchanged)
        if clips and int(clips[0].get("start_beat") or 0) > 0:
            first_sb = int(clips[0]["start_beat"])
            intro_sec = next((s for s in sections if s.name == "intro"), sections[0])
            filler: List[JsonDict] = []
            cur = 0
            end_beat = first_sb
            start_sec = cur / beats_per_sec
            end_sec = end_beat / beats_per_sec
            clip_id = "c00"
            clip_seed = _hmac_hex(job_seed, f"{clip_id}:filler_intro")[:32]
            filler.append(
                {
                    "clip_id": clip_id,
                    "template_id": "filler_intro",
                    "section": intro_sec.name,
                    "role": "broll",
                    "start_beat": 0,
                    "end_beat": end_beat,
                    "duration_beats": end_beat,
                    "start_sec": float(start_sec),
                    "end_sec": float(end_sec),
                    "duration_sec": float(max(0.0, end_sec - start_sec)),
                    "clip_seed": clip_seed,
                    "video": {},
                    "camera": {},
                    "prompt_hints": ["intro"],
                }
            )
            renumbered: List[JsonDict] = []
            for i, c in enumerate(clips, start=1):
                c = dict(c)
                c["clip_id"] = f"c{i:02d}"
                renumbered.append(c)
            clips = filler + renumbered

        if clips:
            last_end = int(clips[-1].get("end_beat") or 0)
            if last_end < total_beats:
                outro_sec = next((s for s in sections if s.name == "outro"), sections[-1])
                remaining = total_beats - last_end
                fillers_to_add = 0
                while remaining > 0 and len(clips) < 18 and fillers_to_add < 2:
                    take = _clamp_int(_quantize_to_bar(remaining, beats_per_bar), beats_per_bar, max_beats)
                    start = last_end
                    end = min(total_beats, start + take)
                    idx = len(clips)
                    clip_id = f"c{idx:02d}"
                    clip_seed = _hmac_hex(job_seed, f"{clip_id}:filler_outro")[:32]
                    start_sec = start / beats_per_sec
                    end_sec = end / beats_per_sec
                    clips.append(
                        {
                            "clip_id": clip_id,
                            "template_id": "filler_outro",
                            "section": outro_sec.name,
                            "role": "broll",
                            "start_beat": start,
                            "end_beat": end,
                            "duration_beats": max(0, end - start),
                            "start_sec": float(start_sec),
                            "end_sec": float(end_sec),
                            "duration_sec": float(max(0.0, end_sec - start_sec)),
                            "clip_seed": clip_seed,
                            "video": {},
                            "camera": {},
                            "prompt_hints": ["outro"],
                        }
                    )
                    last_end = end
                    remaining = total_beats - last_end
                    fillers_to_add += 1

        target_defaults = _as_dict(cookbook.get("target_defaults"))
        ar_raw = target_defaults.get("aspect_ratio")
        aspect_ratio = ar_raw.strip() if isinstance(ar_raw, str) and ar_raw.strip() else "16:9"

        fps = _coerce_int(target_defaults.get("fps"), 30)
        fps = _clamp_int(fps, 24, 60)

        manifest: JsonDict = {
            "manifest_version": 1,

            # preset fields
            "preset_name": preset_name,
            "preset_source": preset_source,
            "preset_tags_used": _dedupe_preserve_order([*tags_used, *preset_tags]),
            "preset_catalog_tags": preset_tags,

            "scene": {
                "primary_tag": primary_tag,
                "secondary_tags": secondary_tags,
                "no_face": bool(no_face),
                "tags_used": _dedupe_preserve_order([*tags_used, *preset_tags]),
            },
            "exports": _default_exports(),

            "music_video_job_id": str(music_video_job_id),
            "job_seed": job_seed,
            "target": {
                "aspect_ratio": aspect_ratio,
                "fps": int(fps),
                "look": _as_dict(target_defaults.get("look")),
            },
            "timeline": {
                "bpm": float(bpm),
                "beats_per_bar": int(beats_per_bar),
                "total_beats": int(total_beats),
                "duration_sec": float(duration_sec),
                "sections": [{"name": s.name, "start_beat": s.start_beat, "end_beat": s.end_beat} for s in sections],
            },
            "edit": _as_dict(cookbook.get("edit_defaults")),
            "constraints": _as_dict(cookbook.get("global_constraints")),
            "clips": clips,
        }

        return manifest