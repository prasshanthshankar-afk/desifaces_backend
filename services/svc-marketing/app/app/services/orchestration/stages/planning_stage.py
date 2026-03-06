# services/svc-marketing/app/app/services/orchestration/stages/planning_stage.py
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date, datetime
from typing import Any, Dict, Optional
from uuid import UUID

from app.domain.models import MarketingRunIn, UseCaseSpec
from app.repos.marketing_runs_repo import MarketingRunsRepo
from app.repos.marketing_use_cases_repo import MarketingUseCasesRepo
from app.services.orchestration.errors import MarketingRunFailed
from app.services.orchestration.utils.jsonx import as_dict
from app.services.planning.usecase_planner import UseCasePlanner

logger = logging.getLogger("svc-marketing-planning-stage")

# Optional import: keep service boot-safe if you haven't added the repo file yet.
try:
    # Expected: services/svc-marketing/app/app/repos/festival_calendar_repo.py
    from app.repos.festival_calendar_repo import FestivalCalendarRepo  # type: ignore
except Exception:  # pragma: no cover
    FestivalCalendarRepo = None  # type: ignore[misc,assignment]


def _usecase_to_json_dict(uc: UseCaseSpec) -> Dict[str, Any]:
    """
    Ensure UseCaseSpec is JSON-serializable for DB persistence.
    Pydantic v2: model_dump(mode="json") converts UUID/enums to strings.
    """
    # Best path: pydantic v2 JSON mode
    try:
        d = uc.model_dump(mode="json")  # type: ignore[call-arg]
        return d if isinstance(d, dict) else {}
    except Exception:
        pass

    # Fallback: pydantic v2 model_dump_json()
    try:
        txt = uc.model_dump_json()  # type: ignore[attr-defined]
        obj = json.loads(txt) if isinstance(txt, str) else {}
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass

    # Last resort: raw dump (may contain UUID objects)
    try:
        d2 = uc.model_dump()  # type: ignore[call-arg]
        return d2 if isinstance(d2, dict) else {}
    except Exception:
        return {}


def _stable_u32_from_run_id(run_id: UUID) -> int:
    """
    Deterministic 32-bit-ish seed from run_id.
    (Local fallback; avoids relying on additional imports.)
    """
    h = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:8]
    return int(h, 16)


def _safe_today_in_tz(tzname: str) -> date:
    """
    Best-effort timezone date. If ZoneInfo isn't available / tz invalid,
    fall back to UTC date.
    """
    tzname = (tzname or "").strip() or "UTC"
    try:
        from zoneinfo import ZoneInfo  # py3.9+

        return datetime.now(ZoneInfo(tzname)).date()
    except Exception:
        return datetime.utcnow().date()


def _pick(seed: int, key: str, items: list[str]) -> str:
    if not items:
        return ""
    h = hashlib.sha256(f"{seed}:{key}".encode("utf-8")).hexdigest()
    return items[int(h[:8], 16) % len(items)]


def _build_creative_direction(
    *,
    seed: int,
    country_code: str,
    locale: Optional[str],
    festival_name: Optional[str],
    motifs: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Deterministic "Creative Direction Pack" that forces dynamism even if LLM plan is generic.
    This is safe and can be consumed downstream by Face/Audio/Video generation.
    """
    motifs = motifs or {}

    scenes = [
        "home_living_room", "home_dining", "home_balcony_rain", "home_bedroom_soft_light", "home_backyard_evening",
        "gym_mirror", "park_morning_walk", "college_corridor", "library_study", "campus_canteen",
        "office_desk", "coworking_startup", "cafe_table", "movie_theatre_lobby",
        "bus_stop_evening", "in_bus_window_seat", "railway_platform", "in_train_window_seat",
        "airport_checkin", "airport_lounge",
        "village_lane_morning", "local_bazaar_market", "community_event_stage", "festival_pandal", "street_fair",
    ]
    times = ["morning", "afternoon", "golden_hour", "night_city_lights", "rainy_evening"]
    pose_actions = [
        "talking_to_camera_natural_smile",
        "walking_and_talking_with_hand_gestures",
        "pointing_to_on_screen_text_overlay",
        "showing_phone_screen_then_back_to_camera",
        "sipping_chai_then_hook_line",
        "laugh_reaction_then_explain",
        "calm_confident_explainer_style",
    ]
    cameras = [
        "handheld_phone_selfie_feel",
        "gentle_push_in",
        "wide_to_mid_cut",
        "mid_to_close_cut",
        "quick_whip_pan_reveal",
        "split_screen_before_after",
    ]
    energies = ["calm", "warm", "confident", "high_energy", "cinematic", "funny_fast"]
    attires = ["casual_citywear", "office_casual", "traditional_simple", "festival_traditional", "college_casual", "gym_wear"]

    scene_primary = _pick(seed, "scene_primary", scenes)
    scene_secondary_1 = _pick(seed, "scene_secondary_1", scenes)
    scene_secondary_2 = _pick(seed, "scene_secondary_2", scenes)
    scene_secondary = [s for s in [scene_secondary_1, scene_secondary_2] if s and s != scene_primary]

    time_of_day = _pick(seed, "time_of_day", times)
    pose_action = _pick(seed, "pose_action", pose_actions)

    cam1 = _pick(seed, "camera_1", cameras)
    cam2 = _pick(seed, "camera_2", cameras)
    camera = [c for c in [cam1, cam2] if c]
    # dedupe while preserving order
    camera = [c for i, c in enumerate(camera) if c not in camera[:i]]

    energy = _pick(seed, "energy", energies)

    attire = _pick(seed, "attire", attires)
    if festival_name:
        attire = "festival_traditional"

    palette = motifs.get("palette") or motifs.get("colors") or []
    if isinstance(palette, str):
        palette = [palette]
    if not isinstance(palette, list):
        palette = []
    palette = [str(x) for x in palette if x]

    props = motifs.get("props") or []
    if isinstance(props, str):
        props = [props]
    if not isinstance(props, list):
        props = []
    props = [str(x) for x in props if x]

    fest_line = f"Festival vibe: {festival_name}. " if festival_name else ""
    palette_line = f"Color palette: {', '.join(palette[:4])}. " if palette else ""
    props_line = f"Props: {', '.join(props[:4])}. " if props else ""

    face_prompt = (
        f"{fest_line}{palette_line}{props_line}"
        f"Setting: {scene_primary} at {time_of_day}. "
        f"Attire: {attire}. "
        f"Pose/action: {pose_action}. "
        f"Camera: {', '.join(camera)}. "
        f"Energy: {energy}. "
        f"Country context: {country_code}. "
        f"Ultra-realistic, authentic local vibe, dynamic background depth, no studio backdrop."
    )

    video_direction = (
        f"{fest_line}"
        f"Background context: {scene_primary} at {time_of_day}. "
        f"Perform with {energy} energy. Natural hand gestures, subtle head movement, friendly eye contact. "
        f"Hook in first 2 seconds, pause for emphasis, warm closing smile."
    )

    audio_style = {
        "energy": energy,
        "pace": "fast" if energy in ("high_energy", "funny_fast") else ("slow" if energy == "calm" else "medium"),
        "tone": "friendly",
        "locale": (locale or "").strip(),
    }

    return {
        "scene_primary": scene_primary,
        "scene_secondary": scene_secondary,
        "time_of_day": time_of_day,
        "pose_action": pose_action,
        "camera": camera,
        "energy": energy,
        "attire": attire,
        "palette": palette,
        "props": props,
        "face_prompt": face_prompt,
        "video_direction": video_direction,
        "audio_style": audio_style,
    }


class PlanningStage:
    """
    Stage: planning
      - Reuse cached planning_json.use_case when valid
      - Otherwise invoke UseCasePlanner (LLM/RAG story-first)
      - Enrich with (a) Festival selection (scope-aware) (b) deterministic Creative Direction Pack
      - Persist planning_json as JSON-safe dict (UUID/enums -> strings)
      - Best-effort bump_usage(use_case_id)

    Notes:
      - This stage stays stable even if festival tables are empty or FestivalCalendarRepo is not wired.
      - Creative direction is deterministic and requires no external dependencies.
    """

    def __init__(
        self,
        runs_repo: MarketingRunsRepo,
        usecases_repo: MarketingUseCasesRepo,
        festivals_repo: Optional[Any] = None,  # expected FestivalCalendarRepo
    ):
        self.runs = runs_repo
        self.usecases = usecases_repo
        self.planner = UseCasePlanner(usecases_repo)

        # optional; safe if not provided
        self.festivals = festivals_repo

    async def _enrich_with_festival_and_creative(
        self,
        *,
        run_id: UUID,
        inp: MarketingRunIn,
        uc: UseCaseSpec,
        existing_planning_json: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Returns a merged planning_json dict containing:
          - use_case (json-safe)
          - festival (if any)
          - creative_direction (always)
        Also updates uc.computed in-place (best-effort).
        """
        pj = dict(as_dict(existing_planning_json) or {})
        inputs = as_dict((inp.inputs or {}))

        seed = _stable_u32_from_run_id(run_id)

        country_code = str(inputs.get("country_code") or "IN").strip().upper()
        region_code = inputs.get("region_code")
        religion = inputs.get("religion")
        locale = inputs.get("locale") or inputs.get("target_locale") or inputs.get("language_hint")
        observance_variant = inputs.get("observance_variant") or inputs.get("festival_variant")

        tz = str(inputs.get("timezone") or "Asia/Kolkata").strip() or "Asia/Kolkata"
        today = _safe_today_in_tz(tz)

        festival_block: Dict[str, Any] = {}
        motifs: Dict[str, Any] = {}
        festival_name: Optional[str] = None

        # Festival selection is optional and must never break planning.
        try:
            if self.festivals and getattr(self.festivals, "pick_best_for_day", None):
                lookahead = int(inputs.get("festival_lookahead_days") or 10)
                lookback = int(inputs.get("festival_lookback_days") or 3)

                fest = await self.festivals.pick_best_for_day(
                    today=today,
                    country_code=country_code,
                    region_code=region_code,
                    religion=religion,
                    locale=locale,
                    observance_variant=observance_variant,
                    lookahead_days=lookahead,
                    lookback_days=lookback,
                    strict_when_none=True,
                )
                if fest:
                    festival_name = str(getattr(fest, "name", "") or "") or None
                    motifs = dict(getattr(fest, "motifs", {}) or {})
                    festival_block = {
                        "festival_id": str(getattr(fest, "festival_id", "") or ""),
                        "scope_id": str(getattr(fest, "scope_id", "") or ""),
                        "occurrence_id": str(getattr(fest, "occurrence_id", "") or ""),
                        "slug": str(getattr(fest, "slug", "") or ""),
                        "name": festival_name or "",
                        "festival_date": getattr(fest, "festival_date").isoformat() if getattr(fest, "festival_date", None) else "",
                        "timezone": str(getattr(fest, "timezone", "") or tz),
                        "country_code": str(getattr(fest, "country_code", "") or country_code),
                        "region_code": getattr(fest, "region_code", None),
                        "religion": getattr(fest, "religion", None),
                        "locale": getattr(fest, "locale", None),
                        "observance_variant": getattr(fest, "observance_variant", None),
                        "motifs": motifs,
                    }
        except Exception as e:
            logger.warning("run=%s festival enrichment skipped err=%s", str(run_id), str(e))

        creative = _build_creative_direction(
            seed=seed,
            country_code=country_code,
            locale=str(locale) if locale else None,
            festival_name=festival_name,
            motifs=motifs,
        )

        # Update uc.computed (best-effort; does not break if model is strict)
        try:
            computed = getattr(uc, "computed", None)
            if not isinstance(computed, dict):
                computed = {}
            computed = dict(computed)
            computed["creative_direction"] = creative
            if festival_block:
                computed["festival"] = festival_block
            setattr(uc, "computed", computed)
        except Exception:
            pass

        # If festival exists and season_event is empty, set it (best-effort)
        try:
            if festival_name and hasattr(uc, "season_event"):
                if not getattr(uc, "season_event", None):
                    setattr(uc, "season_event", festival_name)
        except Exception:
            pass

        merged = dict(pj)
        merged["use_case"] = _usecase_to_json_dict(uc)
        merged["creative_direction"] = creative
        if festival_block:
            merged["festival"] = festival_block
        else:
            merged.setdefault("festival", {})

        return merged

    async def run(
        self,
        *,
        run_id: UUID,
        inp: MarketingRunIn,
        planning_json: Dict[str, Any],
        timeout_s: int,
    ) -> UseCaseSpec:
        pj = as_dict(planning_json)

        # 1) reuse cached plan if parseable
        cached = pj.get("use_case")
        if isinstance(cached, dict) and cached:
            try:
                uc_cached = UseCaseSpec(**cached)

                # Ensure creative_direction is present (and festival block if eligible).
                # This is safe and avoids re-planning.
                needs_cd = True
                try:
                    comp = getattr(uc_cached, "computed", None)
                    if isinstance(comp, dict) and isinstance(comp.get("creative_direction"), dict):
                        needs_cd = False
                except Exception:
                    needs_cd = True

                if needs_cd or not isinstance(pj.get("creative_direction"), dict):
                    merged_pj = await self._enrich_with_festival_and_creative(
                        run_id=run_id,
                        inp=inp,
                        uc=uc_cached,
                        existing_planning_json=pj,
                    )
                    try:
                        await self.runs.set_planning_json(run_id, merged_pj)
                    except Exception as e:
                        raise MarketingRunFailed("PLANNING_PERSIST_FAILED", str(e))

                return uc_cached
            except Exception as e:
                logger.warning(
                    "run=%s invalid cached planning_json.use_case; replanning. err=%s",
                    str(run_id),
                    str(e),
                )

        # 2) plan now (LLM/RAG)
        try:
            uc = await asyncio.wait_for(self.planner.plan(inp), timeout=float(timeout_s))
        except asyncio.TimeoutError:
            raise MarketingRunFailed("PLANNING_TIMEOUT", f"planner timed out after {timeout_s}s")
        except MarketingRunFailed:
            raise
        except Exception as e:
            raise MarketingRunFailed("PLANNING_FAILED", str(e))

        # 3) enrich with festival + deterministic creative direction (must not fail planning)
        merged_pj = await self._enrich_with_festival_and_creative(
            run_id=run_id,
            inp=inp,
            uc=uc,
            existing_planning_json=pj,
        )

        # 4) persist JSON-safe planning_json
        try:
            await self.runs.set_planning_json(run_id, merged_pj)
        except Exception as e:
            raise MarketingRunFailed("PLANNING_PERSIST_FAILED", str(e))

        # 5) bump usage (best-effort)
        try:
            if uc.use_case_id:
                await self.usecases.bump_usage(uc.use_case_id)
        except Exception:
            pass

        return uc