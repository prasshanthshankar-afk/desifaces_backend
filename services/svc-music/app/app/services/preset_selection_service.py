from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Union, Optional

JsonDict = Dict[str, Any]


def _as_dict(x: Any) -> JsonDict:
    if isinstance(x, dict):
        return x
    return {}


def _as_list(x: Any) -> List[Any]:
    if isinstance(x, list):
        return x
    # common case: postgres may return tuple-like
    if isinstance(x, tuple):
        return list(x)
    if x is None:
        return []
    # if stored as json string
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            try:
                import json

                obj = json.loads(s)
                return obj if isinstance(obj, list) else []
            except Exception:
                return []
    return []


def _as_str(x: Any) -> str:
    return str(x).strip() if x is not None else ""


def _norm_tag(t: str) -> str:
    return (t or "").strip().lower().replace("-", "_").replace(" ", "_")


def _dedupe(xs: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in xs:
        x = _norm_tag(x)
        if x and x not in seen:
            out.append(x)
            seen.add(x)
    return out


def _stable_hash_int(s: str) -> int:
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()
    return int(h[:8], 16)


_PRESET_TAG_HINTS: List[Tuple[str, List[str]]] = [
    ("Rural", ["rural", "village", "warm"]),
    ("Urban", ["city", "neon", "street"]),
    ("Festival", ["festival", "colors", "fireworks"]),
    ("Temple", ["devotional", "heritage", "ritual"]),
    ("Himalayan", ["mountains", "nature", "serene"]),
    ("Goa", ["beach", "ocean", "broll", "no_face"]),
    ("Night Drive", ["night", "city", "motion"]),
    ("EDM Club", ["stage", "concert", "neon"]),
    ("Stadium Concert", ["stage", "concert"]),
    ("Epic Trailer", ["broll", "no_face", "dramatic"]),
    ("Minimal Lyric", ["no_face", "clean"]),
    ("Kinetic Lyric", ["no_face", "typography"]),
]


@dataclass(frozen=True)
class PresetSelection:
    preset_name: str
    primary_tag: str
    secondary_tags: List[str]


class PresetSelectionService:
    """
    v2:
      - If style.preset_name given -> use it.
      - Else infer desired tags from music_plan + hints.
      - Score presets using DB tags when available (preferred).
      - Fallback: infer tags from preset name keywords.
      - Deterministic tie-break.
    """

    def infer_tags(self, *, input_json: JsonDict) -> List[str]:
        hints = _as_dict(input_json.get("provider_hints"))
        computed = _as_dict(input_json.get("computed"))
        mp = _as_dict(computed.get("music_plan"))
        brief = _as_dict(mp.get("brief"))

        tags: List[str] = []
        for src in (
            _as_list(hints.get("scene_tags")),
            _as_list(hints.get("tags")),
            _as_list(brief.get("tags")),
            _as_list(brief.get("style_tags")),
        ):
            for t in src:
                nt = _norm_tag(str(t))
                if nt and nt not in tags:
                    tags.append(nt)

        genre = _norm_tag(str(brief.get("genre") or hints.get("genre") or ""))
        mood = _norm_tag(str(brief.get("mood") or hints.get("mood") or ""))
        tempo = _norm_tag(str(brief.get("tempo") or hints.get("tempo") or ""))

        # Simple v1 mapping (keep)
        if genre in ("devotional", "bhajan"):
            tags += ["devotional", "heritage"]
        if mood in ("uplifting", "happy", "joy"):
            tags += ["bright"]
        if mood in ("dramatic", "epic"):
            tags += ["dramatic"]
        if tempo in ("fast", "high", "peak"):
            tags += ["energy"]
        if tempo in ("slow", "calm"):
            tags += ["serene"]

        return _dedupe([str(t) for t in tags])

    def _infer_tags_from_name(self, preset_name: str) -> List[str]:
        pname = preset_name or ""
        inferred: List[str] = []
        for key, tags in _PRESET_TAG_HINTS:
            if key.lower() in pname.lower():
                inferred.extend(tags)
        return _dedupe([str(t) for t in inferred])

    def _tags_from_preset_row(self, row: JsonDict) -> List[str]:
        """
        Uses schema from public.music_style_presets via MusicStylePresetsRepo:
          tags, scene_primary_tag, scene_secondary_tags, mood_tag, energy_tag, face_mode, grade, shot_cookbook_json
        """
        tags: List[str] = []

        tags += [str(t) for t in _as_list(row.get("tags"))]

        pt = _as_str(row.get("scene_primary_tag"))
        if pt:
            tags.append(pt)

        tags += [str(t) for t in _as_list(row.get("scene_secondary_tags"))]

        mt = _as_str(row.get("mood_tag"))
        if mt:
            tags.append(mt)

        et = _as_str(row.get("energy_tag"))
        if et:
            tags.append(et)

        fm = _as_str(row.get("face_mode"))
        if fm:
            tags.append(fm)

        gr = _as_str(row.get("grade"))
        if gr:
            tags.append(gr)

        cb = row.get("shot_cookbook_json")
        if isinstance(cb, dict):
            tags += [str(t) for t in _as_list(cb.get("tags"))]
            scene = _as_dict(cb.get("scene"))
            pt2 = _as_str(scene.get("primary_tag"))
            if pt2:
                tags.append(pt2)
            tags += [str(t) for t in _as_list(scene.get("secondary_tags"))]
            tags += [str(t) for t in _as_list(scene.get("tags"))]

        return _dedupe(tags)

    def _score(self, *, inferred_tags: List[str], desired_tags: List[str]) -> int:
        desired = set(_dedupe(desired_tags))
        inferred = _dedupe(inferred_tags)

        score = 0
        for t in inferred:
            if t in desired:
                score += 10

        # Bias for no_face/broll consistency
        if "no_face" in desired and ("no_face" in inferred or "broll" in inferred):
            score += 6
        if "stage" in desired and "stage" in inferred:
            score += 3
        return score

    def _primary_secondary_from_tags(self, inferred: List[str], desired: List[str]) -> Tuple[str, List[str]]:
        inf = _dedupe(inferred)
        des = _dedupe(desired)

        primary = ""
        # Prefer a "scene-like" tag as primary
        for cand in ("festival", "wedding", "devotional", "temple", "patriotic", "rural", "goa", "urban", "city", "neon", "stage", "broll", "lyrics", "abstract"):
            if cand in inf:
                primary = cand
                break
            if cand in des:
                primary = cand
                break

        if not primary:
            primary = inf[0] if inf else (des[0] if des else "shot")

        secondary = [t for t in (des + inf) if t and t != primary]
        secondary = _dedupe(secondary)[:6]
        return primary, secondary

    def select_preset(
        self,
        *,
        project_id: str,
        job_id: str,
        input_json: JsonDict,
        available_presets: Union[List[str], List[JsonDict]],
    ) -> PresetSelection:
        style = _as_dict(input_json.get("style"))
        explicit = (style.get("preset_name") or input_json.get("preset_name") or "").strip()
        if explicit:
            # Explicit means "do not second-guess"
            return PresetSelection(preset_name=explicit, primary_tag="shot", secondary_tags=[])

        desired_tags = self.infer_tags(input_json=input_json)
        seed = _stable_hash_int(f"{project_id}:{job_id}")

        best_name: Optional[str] = None
        best_score = -10**18
        best_inferred: List[str] = []

        # Support either list[str] (names) or list[dict] (rows)
        if available_presets and isinstance(available_presets[0], dict):
            rows: List[JsonDict] = [r for r in available_presets if isinstance(r, dict)]
            for i, row in enumerate(rows):
                name = _as_str(row.get("name"))
                if not name:
                    continue
                inferred = self._tags_from_preset_row(row)
                s = self._score(inferred_tags=inferred, desired_tags=desired_tags)
                tie = (seed + i) % 7
                s2 = s * 100 + tie
                if s2 > best_score:
                    best_score = s2
                    best_name = name
                    best_inferred = inferred
        else:
            names: List[str] = [str(x).strip() for x in (available_presets or []) if str(x).strip()]
            for i, name in enumerate(names):
                inferred = self._infer_tags_from_name(name)
                s = self._score(inferred_tags=inferred, desired_tags=desired_tags)
                tie = (seed + i) % 7
                s2 = s * 100 + tie
                if s2 > best_score:
                    best_score = s2
                    best_name = name
                    best_inferred = inferred

        if not best_name:
            best_name = "Urban Neon Hustle Peak"
            best_inferred = self._infer_tags_from_name(best_name)

        primary, secondary = self._primary_secondary_from_tags(best_inferred, desired_tags)
        return PresetSelection(preset_name=best_name, primary_tag=primary, secondary_tags=secondary)