# services/svc-marketing/app/app/services/orchestration/recipes/face_audio_video.py
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
from PIL import Image, ImageFilter

from app.config import settings
from app.domain.enums import MarketingRunMode
from app.domain.models import MarketingRunIn, UseCaseSpec
from app.services.storage.blob_uploader import BlobUploader
from app.services.orchestration.errors import MarketingRunFailed
from app.services.orchestration.run_context import RunContext
from app.services.orchestration.utils.azure_sas import maybe_add_azure_read_sas
from app.services.orchestration.utils.config import cfg_bool, cfg_float, cfg_int, cfg_str
from app.services.orchestration.utils.determinism import pick_from_seed, stable_pick_index
from app.services.orchestration.utils.jsonx import as_dict, deep_find_url, truncate_json
from app.services.orchestration.utils.media_extract import deep_find_audio_url, extract_image_urls, extract_media
from app.services.orchestration.qc.gender_qc import (
    default_voice_for_locale_gender,
    infer_voice_gender,
    norm_gender,
    qc_voice_matches_gender_or_fail,
)

logger = logging.getLogger("svc-marketing-recipe-face-audio-video")


# -------------------------
# URL + SAS helpers (CRITICAL)
# -------------------------


def _strip_query(url: str) -> str:
    try:
        s = urlsplit(url)
        return urlunsplit((s.scheme, s.netloc, s.path, "", ""))
    except Exception:
        return url.split("?", 1)[0]


def _refresh_read_sas(url: str, hours: int) -> str:
    """
    IMPORTANT: Always strip existing SAS and re-sign.
    """
    if not isinstance(url, str) or not url:
        return ""
    base = _strip_query(url)
    return maybe_add_azure_read_sas(base, expiry_hours=int(hours or 24)) or url


# -------------------------
# aspect + locales
# -------------------------


def _aspect_ratio(fmt: str) -> str:
    f = (fmt or "reel").strip().lower()
    if f == "yt_long":
        return "16:9"
    return "9:16"


def _aspect_canvas(ar: str) -> Tuple[int, int]:
    ar = (ar or "9:16").strip()
    if ar == "16:9":
        return 1920, 1080
    if ar == "1:1":
        return 1080, 1080
    return 1080, 1920


def _supported_locales() -> List[str]:
    raw = cfg_str("MARKETING_SUPPORTED_LOCALES", "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    return ["en-US", "en-IN", "hi-IN", "ta-IN", "te-IN", "pa-IN", "bn-IN", "gu-IN", "mr-IN", "kn-IN", "ml-IN"]


def _pick_locale(seed: int, candidates: List[str]) -> str:
    supported = set(_supported_locales())
    for c in candidates:
        if c in supported:
            return c
    supp = sorted(list(supported)) or ["en-US"]
    return pick_from_seed(seed, "fallback_locale", supp)


# -------------------------
# creative direction + festival (from planning_stage)
# -------------------------


def _creative_direction(use_case: UseCaseSpec) -> Dict[str, Any]:
    """
    planning_stage writes:
      use_case.computed["creative_direction"] = { scene_primary, scene_secondary, time_of_day, pose_action, camera, energy, attire, ... }
      use_case.computed["festival"] = {...} (optional)
    """
    try:
        computed = as_dict(getattr(use_case, "computed", None))
    except Exception:
        computed = {}
    cd = as_dict(computed.get("creative_direction"))
    return cd if isinstance(cd, dict) else {}


def _festival_block(use_case: UseCaseSpec) -> Dict[str, Any]:
    try:
        computed = as_dict(getattr(use_case, "computed", None))
    except Exception:
        computed = {}
    f = as_dict(computed.get("festival"))
    return f if isinstance(f, dict) else {}


def _friendly_scene(x: str) -> str:
    s = (x or "").strip()
    if not s:
        return ""
    return s.replace("_", " ").replace("-", " ").strip()


def _coerce_str_list(x: Any) -> List[str]:
    if x is None:
        return []
    if isinstance(x, str):
        s = x.strip()
        return [s] if s else []
    if isinstance(x, list):
        out: List[str] = []
        for v in x:
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        return out
    return []


def _merge_nonempty(*vals: Optional[str]) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _dedupe_bits(bits: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for b in bits:
        s = (b or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _apply_creative_direction(
    *,
    mp_tts: Dict[str, Any],
    mp_visual: Dict[str, Any],
    mp_demo: Dict[str, Any],
    mp_video: Dict[str, Any],
    cd: Dict[str, Any],
    festival: Dict[str, Any],
    prefer_creative: bool,
) -> None:
    """
    Blends creative_direction into marketing_plan blocks.
    prefer_creative=True makes creative_direction win over weaker defaults.
    """
    if not isinstance(cd, dict) or not cd:
        return

    scene_primary = _friendly_scene(str(cd.get("scene_primary") or ""))
    time_of_day = str(cd.get("time_of_day") or "").strip()
    pose_action = str(cd.get("pose_action") or "").strip()
    energy = str(cd.get("energy") or "").strip()
    attire = str(cd.get("attire") or "").strip()
    camera = _coerce_str_list(cd.get("camera"))
    palette = _coerce_str_list(cd.get("palette"))
    props = _coerce_str_list(cd.get("props"))

    festival_name = str(festival.get("name") or "").strip()
    if festival_name and (not str(mp_visual.get("occasion") or "").strip() or prefer_creative):
        mp_visual["occasion"] = festival_name

    # Visual/background/scene/activity/camera/mood
    if scene_primary and (not str(mp_visual.get("background") or "").strip() or prefer_creative):
        # Keep it "background-y" rather than overly structured
        bg = scene_primary
        if time_of_day:
            bg = f"{bg}, {time_of_day}"
        mp_visual["background"] = bg

    if pose_action and (not str(mp_visual.get("activity") or "").strip() or prefer_creative):
        mp_visual["activity"] = pose_action

    # "scene" is often used by downstream prompt builder
    if scene_primary and (not str(mp_visual.get("scene") or "").strip() or prefer_creative):
        mp_visual["scene"] = scene_primary

    if camera and (not str(mp_visual.get("camera") or "").strip() or prefer_creative):
        mp_visual["camera"] = ", ".join(camera[:2])

    if energy and (not str(mp_visual.get("mood") or "").strip() or prefer_creative):
        mp_visual["mood"] = energy

    # Demographics/attire
    if attire and (not str(mp_demo.get("attire") or "").strip() or prefer_creative):
        mp_demo["attire"] = attire

    # Video defaults (fusion)
    if energy and (not str(mp_video.get("emotion") or "").strip() or prefer_creative):
        if energy in ("high_energy", "funny_fast"):
            mp_video["emotion"] = "high energy, playful, confident"
        elif energy in ("cinematic",):
            mp_video["emotion"] = "cinematic, confident, warm"
        elif energy in ("calm",):
            mp_video["emotion"] = "calm, warm, confident"
        else:
            mp_video["emotion"] = "friendly, confident"

    if energy and (not str(mp_video.get("motion_style") or "").strip() or prefer_creative):
        if energy in ("high_energy", "funny_fast"):
            mp_video["motion_style"] = "dynamic, lively hand gestures, natural head movement"
        else:
            mp_video["motion_style"] = "dynamic, natural hand gestures, confident"

    # Add palette/props into visual hints (non-breaking)
    if palette and (not str(mp_visual.get("colors") or "").strip()):
        mp_visual["colors"] = ", ".join(palette[:4])
    if props and (not str(mp_visual.get("props") or "").strip()):
        mp_visual["props"] = ", ".join(props[:4])

    # Locale: only set if missing (avoid fighting explicit locale)
    cd_audio = as_dict(cd.get("audio_style"))
    cd_locale = str(cd_audio.get("locale") or "").strip()
    if cd_locale and not str(mp_tts.get("target_locale") or "").strip():
        mp_tts["target_locale"] = cd_locale


# -------------------------
# dynamic activities + diversity
# -------------------------


def _activity_catalog() -> List[Dict[str, Any]]:
    """
    Expanded activity palette (more dynamic contexts). Per-run we pick ONE,
    but across runs you get wide diversity.
    """
    return [
        {"key": "jogging", "requires_full_body": True, "scene": "jogging in a park, early morning light", "background": "city park trail, soft sunrise"},
        {"key": "gym", "requires_full_body": True, "scene": "in the gym, energetic workout", "background": "modern gym interior, premium lighting"},
        {"key": "sports", "requires_full_body": True, "scene": "playing sports, action moment", "background": "sports ground, dynamic energy"},
        {"key": "cooking", "requires_full_body": True, "scene": "cooking at home, lifestyle content", "background": "clean modern kitchen, warm light"},
        {"key": "living_room", "requires_full_body": False, "scene": "at home in the living room, cozy vibe", "background": "modern living room, warm lamp light"},
        {"key": "dining", "requires_full_body": False, "scene": "at home dining table, casual chat", "background": "dining room, soft daylight"},
        {"key": "balcony_rain", "requires_full_body": False, "scene": "on balcony during rain, cinematic", "background": "rainy city skyline, wet reflections"},
        {"key": "bedroom_soft", "requires_full_body": False, "scene": "in bedroom, soft aesthetic vlog", "background": "bedroom, soft window light"},
        {"key": "cafe", "requires_full_body": False, "scene": "at a cafe, casual creator vibe", "background": "cozy cafe interior, warm light"},
        {"key": "campus", "requires_full_body": True, "scene": "college campus, student creator vibe", "background": "campus walkway, daylight"},
        {"key": "library", "requires_full_body": False, "scene": "in a library, focused vibe", "background": "library shelves, daylight"},
        {"key": "metro", "requires_full_body": False, "scene": "commuting, city lifestyle", "background": "metro station, modern city"},
        {"key": "railway_platform", "requires_full_body": False, "scene": "railway platform, travel documentary", "background": "platform signage, candid look"},
        {"key": "in_train", "requires_full_body": False, "scene": "in a train, window seat creator vibe", "background": "train interior, motion blur outside"},
        {"key": "bus_stop", "requires_full_body": False, "scene": "at a bus stop, real life moment", "background": "bus stop, city street"},
        {"key": "airport", "requires_full_body": False, "scene": "airport terminal, modern travel vibe", "background": "airport interior, premium lighting"},
        {"key": "office", "requires_full_body": False, "scene": "professional creator, confident", "background": "modern office lobby, premium look"},
        {"key": "coworking", "requires_full_body": False, "scene": "startup coworking, tech vibe", "background": "coworking space, neon accents"},
        {"key": "village_lane", "requires_full_body": True, "scene": "village lane, authentic daily life", "background": "village street, warm sunlight"},
        {"key": "bazaar", "requires_full_body": True, "scene": "local bazaar market, vibrant", "background": "market stalls, colorful signage"},
        {"key": "community_event", "requires_full_body": True, "scene": "community event, stage vibe", "background": "community hall, festive lights"},
        {"key": "festival_pandal", "requires_full_body": True, "scene": "festival pandal, celebration vibe", "background": "festival lights, crowd bokeh"},
        {"key": "friends_get_together", "requires_full_body": False, "scene": "friends get-together, lively", "background": "apartment party, warm lights"},
        {"key": "movie_theatre", "requires_full_body": False, "scene": "movie theatre lobby, fun vibe", "background": "cinema lobby, neon signs"},
    ]


def _pick_activity(seed: int) -> Dict[str, Any]:
    return pick_from_seed(seed, "activity", _activity_catalog())


def _diversity_profiles() -> List[Dict[str, Any]]:
    """
    Kept: your curated region bundles.
    (Creative Direction Pack now adds even more variety across runs.)
    """
    return [
        {
            "region": "Tamil Nadu",
            "locale_candidates": ["ta-IN", "en-IN", "en-US"],
            "language_hint": "ta",
            "attire": [
                "traditional silk saree, minimal gold jewelry",
                "salwar kameez, simple dupatta",
                "town casual: kurti and jeans",
                "college casual: t-shirt and jeans, sneakers",
                "office wear: formal saree with blazer",
            ],
            "background": [
                "Chennai city street, evening hustle",
                "monsoon drizzle, wet road reflections",
                "railway station platform, candid documentary look",
                "airport terminal interior, modern travel vibe",
                "Marina beach morning light, soft breeze",
                "college campus walkway, daylight",
                "home living room, warm light",
                "local bazaar market, vibrant signage",
            ],
            "occasion": ["college day", "festival season", "internship announcement", "new reel drop", "startup demo"],
        },
        {
            "region": "Punjab",
            "locale_candidates": ["pa-IN", "hi-IN", "en-IN", "en-US"],
            "language_hint": "pa",
            "attire": [
                "traditional Punjabi suit, bright dupatta",
                "town casual: kurta and jeans",
                "winter jacket with scarf, street style",
                "college casual: hoodie and jeans, sneakers",
                "formal: blazer over kurta",
            ],
            "background": [
                "Amritsar market street, vibrant signage",
                "bus station, travel documentary vibe",
                "airport terminal, modern travel vibe",
                "rural fields, golden hour, cinematic",
                "rainy city street, umbrellas, reflections",
                "college campus courtyard, daylight",
                "home balcony in rain, cinematic mood",
            ],
            "occasion": ["college fest", "weekend vlog", "travel reel", "festival season", "career update"],
        },
        {
            "region": "West Bengal",
            "locale_candidates": ["bn-IN", "en-IN", "hi-IN"],
            "language_hint": "bn",
            "attire": [
                "traditional saree, subtle jewelry",
                "town casual: t-shirt and jeans",
                "smart casual: jacket and sneakers",
                "college casual: oversized hoodie",
            ],
            "background": [
                "Kolkata street, tram vibe, cinematic",
                "cafe interior, warm light",
                "monsoon rain, reflections",
                "campus library exterior, daylight",
                "movie theatre lobby, neon signs",
            ],
            "occasion": ["college day", "new reel drop", "festive look", "creator collab"],
        },
        {
            "region": "Kerala",
            "locale_candidates": ["ml-IN", "en-IN"],
            "language_hint": "ml",
            "attire": [
                "traditional kasavu saree, minimal jewelry",
                "salwar kameez, simple dupatta",
                "town casual: kurti and jeans",
                "college casual: t-shirt and jeans",
            ],
            "background": [
                "green backwaters, golden hour",
                "rainy street, umbrellas, reflections",
                "cafe interior, warm light",
                "campus walkway, daylight",
                "home dining room, soft daylight",
            ],
            "occasion": ["college fest", "festival season", "weekend vlog", "product demo"],
        },
        {
            "region": "English (Pan-India)",
            "locale_candidates": ["en-IN", "en-US"],
            "language_hint": "en",
            "attire": [
                "office wear: blazer and blouse/shirt",
                "town casual: t-shirt and jeans",
                "smart casual: jacket and sneakers",
                "traditional fusion: kurta with blazer",
                "college casual: hoodie and sneakers",
            ],
            "background": [
                "modern office lobby, premium look",
                "city street, evening hustle",
                "airport terminal, modern travel vibe",
                "rainy street, reflections, cinematic",
                "cafe interior, warm light",
                "college campus walkway, daylight",
                "railway platform, candid documentary look",
            ],
            "occasion": ["internship announcement", "college day", "creator collab", "new reel drop", "startup demo"],
        },
    ]


def _pick_diversity_profile(seed: int) -> Dict[str, Any]:
    profiles = _diversity_profiles()
    prof = profiles[int(hashlib.sha256(f"{seed}:profile".encode("utf-8")).hexdigest()[:8], 16) % len(profiles)]

    # bias toward 18–24
    age = pick_from_seed(seed, "age", ["18-24", "18-24", "25-35", "35-45"])
    gender = pick_from_seed(seed, "gender", ["female", "male"])
    locale = _pick_locale(seed, list(prof.get("locale_candidates") or ["en-US"]))
    attire = pick_from_seed(seed, "attire", list(prof.get("attire") or ["business casual"]))
    bg = pick_from_seed(seed, "background", list(prof.get("background") or ["studio, minimal premium backdrop"]))
    occasion = pick_from_seed(seed, "occasion", list(prof.get("occasion") or ["new reel drop"]))
    act = _pick_activity(seed)

    return {
        "region": str(prof.get("region") or "India"),
        "locale": locale,
        "language_hint": str(prof.get("language_hint") or "en"),
        "gender": gender,
        "age_range": age,
        "attire": attire,
        "background": bg,
        "occasion": occasion,
        "activity": act,
    }


def _identity_variations_with_scene(
    base_prompt: str,
    *,
    seed: int,
    n: int,
    attire_pool: List[str],
    bg_pool: List[str],
    camera_pool: Optional[List[str]] = None,
    pose_pool: Optional[List[str]] = None,
) -> List[str]:
    n = max(1, min(8, int(n)))
    bp = (base_prompt or "").strip().rstrip(" ,")
    if not bp:
        return []

    face_shapes = [
        "distinct oval face, soft jawline",
        "distinct round face, fuller cheeks",
        "distinct heart-shaped face",
        "distinct square jawline",
        "distinct narrow face, defined chin",
    ]
    hair = [
        "straight shoulder-length hair",
        "long wavy hair",
        "curly hair",
        "hair in a neat bun",
        "ponytail hairstyle",
        "side-parted hair",
    ]
    eyewear = ["no glasses", "thin-rim glasses", "rectangular glasses"]
    adorn = ["no bindi", "small subtle bindi"]
    camera_default = ["35mm portrait photo", "50mm portrait photo", "natural window-light portrait", "cinematic handheld phone look"]
    pose_default = [
        "open confident posture, relaxed shoulders",
        "walking candid moment, natural movement",
        "talking to camera, expressive hand gesture",
        "smiling, friendly approachable vibe",
    ]

    attire_pool2 = [a.strip() for a in (attire_pool or []) if a and a.strip()] or [
        "college casual: hoodie and jeans, sneakers",
        "town casual: t-shirt and jeans",
        "traditional outfit, minimal jewelry",
        "smart casual: jacket and sneakers",
        "professional: blazer and shirt/blouse",
    ]
    bg_pool2 = [b.strip() for b in (bg_pool or []) if b and b.strip()] or [
        "college campus walkway, daylight",
        "busy city street, evening hustle",
        "monsoon rain, wet road reflections",
        "airport terminal interior, modern travel vibe",
        "railway station platform, candid portrait",
        "modern office lobby, premium look",
        "cafe interior, warm light",
        "home living room, warm lamp light",
        "in a train window seat, travel vibe",
        "local bazaar market, vibrant",
    ]

    cam_pool = [c.strip() for c in (camera_pool or []) if isinstance(c, str) and c.strip()] or camera_default
    ps_pool = [p.strip() for p in (pose_pool or []) if isinstance(p, str) and p.strip()] or pose_default

    out: List[str] = []
    for i in range(n):
        s = int(seed) + (i * 1000003)
        attire = pick_from_seed(s, "attire2", attire_pool2)
        bg = pick_from_seed(s, "bg2", bg_pool2)
        cam = pick_from_seed(s, "cam2", cam_pool)
        ps = pick_from_seed(s, "pose2", ps_pool)

        suffix = ", ".join(
            [
                "distinct individual, unique facial identity",
                pick_from_seed(s, "face", face_shapes),
                pick_from_seed(s, "hair", hair),
                pick_from_seed(s, "eyewear", eyewear),
                pick_from_seed(s, "adorn", adorn),
                f"attire: {attire}",
                f"background: {bg}",
                f"camera: {cam}",
                f"pose: {ps}",
                "natural hand gestures; do NOT fold arms; do NOT clasp hands",
                "keep full head visible with headroom; do not crop forehead",
            ]
        )
        out.append(f"{bp}, {suffix}")
    return out


# -------------------------
# audio locale helpers
# -------------------------


def _default_target_locale(language_hint: str) -> str:
    forced = cfg_str("AUDIO_TARGET_LOCALE", "")
    if forced:
        return forced
    h = (language_hint or "en").strip().lower()
    if h in ("en", "en-us", "en_us"):
        return "en-US"
    if h in ("hi", "hi-in", "hi_in", "hindi"):
        return "hi-IN"
    if h in ("te", "te-in", "te_in", "telugu"):
        return "te-IN"
    if h in ("ta", "ta-in", "ta_in", "tamil"):
        return "ta-IN"
    if h in ("kn", "kn-in", "kn_in", "kannada"):
        return "kn-IN"
    if h in ("ml", "ml-in", "ml_in", "malayalam"):
        return "ml-IN"
    if h in ("mr", "mr-in", "mr_in", "marathi"):
        return "mr-IN"
    if h in ("bn", "bn-in", "bn_in", "bengali"):
        return "bn-IN"
    if h in ("gu", "gu-in", "gu_in", "gujarati"):
        return "gu-IN"
    if h in ("pa", "pa-in", "pa_in", "punjabi"):
        return "pa-IN"
    if len(h) == 2:
        return f"{h}-IN" if h != "en" else "en-US"
    return "en-US"


def _canned_voiceover(locale: str, industry: str, festival_name: str = "", scene_hint: str = "") -> str:
    """
    Used only when story_script is missing and user didn't provide text.
    Keep it short, high-energy, India-first, globally extendable.
    """
    product = "DesiFaces.ai"
    loc = (locale or "en-US").strip()
    fest = f"{festival_name} special! " if festival_name else ""
    scene = f"Right now I’m in {scene_hint}. " if scene_hint else ""

    if loc == "hi-IN":
        return f"नमस्ते! {fest}{scene}{product} से सेकंड्स में face, voice और talking video बनाइए — पोस्ट-रेडी reels. आज India, कल दुनिया. DM: DESIFACES."
    if loc == "ta-IN":
        return f"வணக்கம்! {fest}{scene}{product}-ல seconds-ல face, voice, talking video உருவாக்கலாம். இன்று India, நாளை உலகம். DM: DESIFACES."
    if loc == "te-IN":
        return f"నమస్కారం! {fest}{scene}{product} తో seconds లో face, voice, talking video తయారు చేయండి. ఈ రోజు India, రేపు ప్రపంచం. DM: DESIFACES."
    if loc == "bn-IN":
        return f"নমস্কার! {fest}{scene}{product} দিয়ে সেকেন্ডে face, voice আর talking video বানান। আজ India, কাল বিশ্ব। DM: DESIFACES."
    if loc == "gu-IN":
        return f"નમસ્તે! {fest}{scene}{product} થી seconds માં face, voice અને talking video બનાવો. આજે India, કાલે દુનિયા. DM: DESIFACES."
    if loc == "mr-IN":
        return f"नमस्कार! {fest}{scene}{product} वापरून सेकंदांत face, voice आणि talking video तयार करा. आज India, उद्या जग. DM: DESIFACES."
    if loc == "kn-IN":
        return f"ನಮಸ್ಕಾರ! {fest}{scene}{product} ಬಳಸಿ seconds ನಲ್ಲಿ face, voice ಮತ್ತು talking video ಮಾಡಿ. ಇಂದು India, ನಾಳೆ ಜಗತ್ತು. DM: DESIFACES."
    if loc == "ml-IN":
        return f"നമസ്കാരം! {fest}{scene}{product} ഉപയോഗിച്ച് seconds-ൽ face, voice, talking video ഉണ്ടാക്കൂ. ഇന്ന് India, നാളെ ലോകം. DM: DESIFACES."
    if loc == "pa-IN":
        return f"ਸਤ ਸ੍ਰੀ ਅਕਾਲ! {fest}{scene}{product} ਨਾਲ seconds ਵਿਚ face, voice ਤੇ talking video ਬਣਾਓ। ਅੱਜ India, ਕੱਲ੍ਹ ਦੁਨੀਆ। DM: DESIFACES."
    _ = industry
    return f"Hi! {fest}{scene}With {product}, create face, voice, and a talking video in seconds — post-ready reels. India today, world tomorrow. DM: DESIFACES."


# -------------------------
# download + image reframe (headroom)
# -------------------------


async def _download_url_to_file(url: str, out_path: str, timeout_s: int = 120) -> None:
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                async for chunk in r.aiter_bytes():
                    if chunk:
                        f.write(chunk)


async def _reframe_face_image_and_upload(
    *,
    uploader: BlobUploader,
    run_id: UUID,
    face_image_url: str,
    out_dir: str,
    aspect_ratio: str,
) -> str:
    os.makedirs(out_dir, exist_ok=True)
    in_path = os.path.join(out_dir, "face_in.png")
    out_path = os.path.join(out_dir, f"face_reframed_{aspect_ratio.replace(':','x')}.png")

    await _download_url_to_file(face_image_url, in_path, timeout_s=90)
    canvas_w, canvas_h = _aspect_canvas(aspect_ratio)

    try:
        resample = Image.Resampling.LANCZOS
    except Exception:
        resample = Image.LANCZOS

    img = Image.open(in_path).convert("RGB")
    bg = img.copy().resize((canvas_w, canvas_h), resample=resample)
    try:
        bg = bg.filter(ImageFilter.GaussianBlur(radius=18))
    except Exception:
        pass

    # Stronger headroom defaults; configurable
    if aspect_ratio == "9:16":
        top_pad_ratio = float(cfg_float("MARKETING_FACE_HEADROOM_TOP", 0.18))
        side_pad_ratio = float(cfg_float("MARKETING_FACE_HEADROOM_SIDE", 0.08))
        bottom_pad_ratio = float(cfg_float("MARKETING_FACE_HEADROOM_BOTTOM", 0.06))
    else:
        top_pad_ratio = float(cfg_float("MARKETING_FACE_HEADROOM_TOP", 0.12))
        side_pad_ratio = float(cfg_float("MARKETING_FACE_HEADROOM_SIDE", 0.06))
        bottom_pad_ratio = float(cfg_float("MARKETING_FACE_HEADROOM_BOTTOM", 0.06))

    max_w = int(canvas_w * (1.0 - 2.0 * side_pad_ratio))
    max_h = int(canvas_h * (1.0 - top_pad_ratio - bottom_pad_ratio))

    iw, ih = img.size
    scale = min(max_w / float(iw), max_h / float(ih))
    nw, nh = int(iw * scale), int(ih * scale)
    fg = img.resize((nw, nh), resample=resample)

    x = (canvas_w - nw) // 2
    y = int(canvas_h * top_pad_ratio)

    composed = Image.new("RGB", (canvas_w, canvas_h))
    composed.paste(bg, (0, 0))
    composed.paste(fg, (x, y))
    composed.save(out_path, format="PNG")

    up = await asyncio.to_thread(
        uploader.upload_file,
        out_path,
        f"{run_id}/fusion_face_reframed_{aspect_ratio.replace(':','x')}.png",
        "image/png",
    )
    url = getattr(up, "url", None) if up is not None else None
    if isinstance(up, dict) and not url:
        url = up.get("url")

    sas_hours = cfg_int("MARKETING_FUSION_ASSET_SAS_HOURS", 72)
    return _refresh_read_sas(str(url or ""), sas_hours)


# -------------------------
# montage fallback (FFMPEG)
# -------------------------


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _run_cmd(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"command failed rc={p.returncode}\ncmd={' '.join(cmd)}\nstdout={p.stdout}\nstderr={p.stderr}"
        )


async def _render_montage_fallback(
    *,
    uploader: BlobUploader,
    run_id: UUID,
    image_urls: List[str],
    audio_url: str,
    aspect_ratio: str,
    duration_sec: int,
) -> str:
    if not _which("ffmpeg"):
        raise RuntimeError("ffmpeg not found for montage fallback")

    duration_sec = max(6, min(45, int(duration_sec or 15)))
    img_urls = [u for u in (image_urls or []) if isinstance(u, str) and u.startswith("http")]
    img_urls = img_urls[:8] if len(img_urls) > 8 else img_urls
    if not img_urls:
        raise RuntimeError("no image_urls for montage fallback")
    if not (isinstance(audio_url, str) and audio_url.startswith("http")):
        raise RuntimeError("no audio_url for montage fallback")

    w, h = _aspect_canvas(aspect_ratio)
    per = max(1.0, float(duration_sec) / float(len(img_urls)))

    with tempfile.TemporaryDirectory() as td:
        img_paths: List[str] = []
        for i, u in enumerate(img_urls):
            p = os.path.join(td, f"img_{i:02d}.png")
            await _download_url_to_file(u, p, timeout_s=180)
            img_paths.append(p)

        audio_path = os.path.join(td, "voice.mp3")
        await _download_url_to_file(audio_url, audio_path, timeout_s=180)

        segs: List[str] = []
        for i, p in enumerate(img_paths):
            seg = os.path.join(td, f"seg_{i:02d}.mp4")
            vf = (
                f"scale={w}:{h}:force_original_aspect_ratio=cover,"
                f"crop={w}:{h},"
                f"zoompan=z='min(zoom+0.0008,1.08)':d={int(per*30)}:s={w}x{h},"
                f"fps=30,format=yuv420p"
            )
            _run_cmd(
                [
                    "ffmpeg",
                    "-y",
                    "-loop",
                    "1",
                    "-t",
                    f"{per:.3f}",
                    "-i",
                    p,
                    "-vf",
                    vf,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "20",
                    seg,
                ]
            )
            segs.append(seg)

        concat_list = os.path.join(td, "concat.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for s in segs:
                f.write(f"file '{s}'\n")

        video_no_audio = os.path.join(td, "montage_no_audio.mp4")
        _run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", video_no_audio])

        out_mp4 = os.path.join(td, "montage.mp4")
        _run_cmd(
            [
                "ffmpeg",
                "-y",
                "-i",
                video_no_audio,
                "-i",
                audio_path,
                "-shortest",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                out_mp4,
            ]
        )

        up = await asyncio.to_thread(uploader.upload_file, out_mp4, f"{run_id}/reel_montage.mp4", "video/mp4")
        url = getattr(up, "url", None) if up is not None else None
        if isinstance(up, dict) and not url:
            url = up.get("url")

        sas_hours = cfg_int("MARKETING_FUSION_ASSET_SAS_HOURS", 72)
        return _refresh_read_sas(str(url or ""), sas_hours)


# -------------------------
# Recipe
# -------------------------


class FaceAudioVideoRecipe:
    def __init__(self, *, face_client, audio_client, fusion_client, uploader: BlobUploader):
        self.face = face_client
        self.audio = audio_client
        self.fusion = fusion_client
        self.uploader = uploader

    def _story_script(self, use_case: UseCaseSpec) -> Dict[str, Any]:
        if hasattr(use_case, "story_script"):
            try:
                v = getattr(use_case, "story_script")
                if isinstance(v, dict):
                    return v
            except Exception:
                pass
        pj = as_dict(getattr(use_case, "payload_json", None))
        ss = pj.get("story_script")
        if isinstance(ss, dict):
            return ss
        ra = as_dict(getattr(use_case, "required_assets", None))
        ss2 = ra.get("story_script")
        return ss2 if isinstance(ss2, dict) else {}

    def _story_narration_text(self, use_case: UseCaseSpec) -> str:
        ss = self._story_script(use_case)
        beats = ss.get("beats")
        if isinstance(beats, list) and beats:
            lines = []
            for b in beats:
                if isinstance(b, dict):
                    t = str(b.get("narration") or "").strip()
                    if t:
                        lines.append(t)
            if lines:
                return " ".join(lines)
        return str(getattr(use_case, "voiceover_script", "") or "").strip()

    def _story_visual_prompt(self, use_case: UseCaseSpec) -> str:
        ss = self._story_script(use_case)
        beats = ss.get("beats")
        if isinstance(beats, list) and beats:
            b0 = beats[0] if isinstance(beats[0], dict) else {}
            vp = str((b0 or {}).get("visual_prompt") or "").strip()
            if vp:
                return vp
        return ""

    async def run(
        self,
        *,
        ctx: RunContext,
        use_case: UseCaseSpec,
        inp: MarketingRunIn,
        run_seed: int,
        request_nonce: str,
    ) -> Dict[str, Any]:
        dctx = ctx.to_downstream()
        fmt = str((inp.inputs or {}).get("format_hint") or "reel").strip().lower()
        aspect_ratio = _aspect_ratio(fmt)

        # planning-stage creative direction + festival
        cd = _creative_direction(use_case)
        festival = _festival_block(use_case)
        festival_name = str(festival.get("name") or "").strip()

        req_assets = as_dict(getattr(use_case, "required_assets", None))
        marketing_plan = as_dict(as_dict(req_assets.get("marketing_plan")))
        mp_tts = as_dict(marketing_plan.get("tts"))
        mp_visual = as_dict(marketing_plan.get("visual"))
        mp_demo = as_dict(marketing_plan.get("demographics"))
        mp_video = as_dict(marketing_plan.get("video"))

        # diversity override
        try:
            is_stage = (inp.mode == MarketingRunMode.stage)
        except Exception:
            is_stage = str(getattr(inp, "mode", "")).lower() == "stage"

        diversity_on = cfg_bool("MARKETING_ENABLE_DIVERSITY", True)
        diversity_force = cfg_bool("MARKETING_DIVERSITY_FORCE", is_stage)

        # Creative direction control knobs
        creative_on = cfg_bool("MARKETING_ENABLE_CREATIVE_DIRECTION", True)
        # If True, creative_direction wins over weak defaults (strongly recommended)
        creative_strict = cfg_bool("MARKETING_CREATIVE_DIRECTION_STRICT", True)

        diversity: Dict[str, Any] = {}
        if diversity_on:
            diversity = _pick_diversity_profile(int(run_seed))

            if diversity_force or not str(mp_demo.get("gender") or "").strip():
                mp_demo["gender"] = diversity["gender"]
            if diversity_force or not str(mp_demo.get("age_range") or "").strip():
                mp_demo["age_range"] = diversity["age_range"]
            if diversity_force or not str(mp_demo.get("region") or "").strip():
                mp_demo["region"] = f"from {diversity['region']}, India"
            if diversity_force or not str(mp_demo.get("attire") or "").strip():
                mp_demo["attire"] = diversity["attire"]

            if diversity_force or not str(mp_visual.get("background") or "").strip():
                mp_visual["background"] = diversity["background"]
            if diversity_force or not str(mp_visual.get("occasion") or "").strip():
                mp_visual["occasion"] = diversity["occasion"]

            act = diversity.get("activity") or {}
            if isinstance(act, dict):
                if diversity_force or not str(mp_visual.get("scene") or "").strip():
                    mp_visual["scene"] = str(act.get("scene") or "") or str(mp_visual.get("scene") or "")
                if diversity_force or not str(mp_visual.get("activity") or "").strip():
                    mp_visual["activity"] = str(act.get("key") or "")

            if diversity_force or not str(mp_tts.get("target_locale") or "").strip():
                mp_tts["target_locale"] = diversity["locale"]

        # Apply creative direction after diversity so it can win (when strict)
        if creative_on:
            _apply_creative_direction(
                mp_tts=mp_tts,
                mp_visual=mp_visual,
                mp_demo=mp_demo,
                mp_video=mp_video,
                cd=cd,
                festival=festival,
                prefer_creative=bool(creative_strict or (not diversity_force)),
            )

        logger.info(
            "run=%s creative_on=%s festival=%s diversity_on=%s force=%s region=%s locale=%s gender=%s age=%s attire=%s bg=%s occasion=%s activity=%s",
            str(ctx.run_id),
            str(bool(creative_on and bool(cd))),
            festival_name or "",
            str(bool(diversity_on)),
            str(bool(diversity_force)),
            str((diversity or {}).get("region") or mp_demo.get("region") or ""),
            str(mp_tts.get("target_locale") or ""),
            str(mp_demo.get("gender") or ""),
            str(mp_demo.get("age_range") or ""),
            str(mp_demo.get("attire") or ""),
            str(mp_visual.get("background") or ""),
            str(mp_visual.get("occasion") or ""),
            str(mp_visual.get("activity") or ""),
        )

        pick_key = "|".join(
            [
                str(ctx.run_id),
                str(mp_tts.get("target_locale") or ""),
                str(mp_demo.get("region") or ""),
                str(mp_demo.get("gender") or ""),
                str(mp_demo.get("age_range") or ""),
                str(mp_visual.get("activity") or ""),
                str(mp_visual.get("background") or ""),
                festival_name,
            ]
        )

        # ---------- Face payload ----------
        face_payload = as_dict((inp.inputs or {}).get("face_payload")) or {}
        story_visual = self._story_visual_prompt(use_case)

        act = diversity.get("activity") if isinstance(diversity, dict) else None
        requires_full_body = bool(isinstance(act, dict) and act.get("requires_full_body"))
        user_shot = str(mp_visual.get("shot") or "").strip().lower()
        if requires_full_body and not user_shot:
            mp_visual["shot"] = "full_body"

        mp_shot = str(mp_visual.get("shot") or ("full_body" if mp_visual.get("full_body") is True else "")).strip().lower()

        # Choose a strong base prompt source, with correct precedence:
        # 1) explicit face_payload user_prompt/prompt
        # 2) story_visual (LLM story beat)
        # 3) creative_direction.face_prompt
        # 4) shot-based defaults
        cd_face_prompt = str(cd.get("face_prompt") or "").strip() if isinstance(cd, dict) else ""

        if not str(face_payload.get("prompt") or "").strip() and not str(face_payload.get("user_prompt") or "").strip():
            if story_visual:
                face_payload["prompt"] = story_visual
            elif cd_face_prompt:
                face_payload["prompt"] = cd_face_prompt
            elif mp_shot in ("full_body", "full-body", "fullbody"):
                face_payload["prompt"] = (
                    f"{use_case.industry} promo host, full-body head-to-toe portrait, centered composition, "
                    f"keep entire head visible with extra headroom, dynamic lifestyle look, premium lighting"
                )
            elif mp_shot in ("half_body", "half-body", "waist_up", "waist-up"):
                face_payload["prompt"] = (
                    f"{use_case.industry} promo host, waist-up portrait, centered composition, extra headroom, "
                    f"include shoulders and upper torso, premium lighting"
                )
            else:
                face_payload["prompt"] = (
                    f"{use_case.industry} promo host, head-and-shoulders portrait, centered composition, extra headroom, "
                    f"include shoulders, premium look"
                )

        # Build dynamic bits (festival + scene + posture + camera + energy)
        bits: List[str] = []
        if festival_name:
            bits.append(f"festival vibe: {festival_name}")
        cd_scene_primary = _friendly_scene(str(cd.get("scene_primary") or "")) if isinstance(cd, dict) else ""
        cd_time = str(cd.get("time_of_day") or "").strip() if isinstance(cd, dict) else ""
        if cd_scene_primary:
            bits.append(f"scene: {cd_scene_primary}{(', ' + cd_time) if cd_time else ''}")

        cd_cams = _coerce_str_list(cd.get("camera")) if isinstance(cd, dict) else []
        if cd_cams:
            bits.append(f"camera: {', '.join(cd_cams[:2])}")

        cd_pose = str(cd.get("pose_action") or "").strip() if isinstance(cd, dict) else ""
        if cd_pose:
            bits.append(f"action: {cd_pose}")

        cd_energy = str(cd.get("energy") or "").strip() if isinstance(cd, dict) else ""
        if cd_energy:
            bits.append(f"energy: {cd_energy}")

        for k in ("gender", "age_range", "region", "ethnicity", "skin_tone", "attire", "hair", "accessories"):
            v = mp_demo.get(k)
            if isinstance(v, str) and v.strip():
                bits.append(v.strip())
        for k in ("occasion", "activity", "scene", "background", "lighting", "camera", "mood", "pose", "body_pose", "colors", "props"):
            v = mp_visual.get(k)
            if isinstance(v, str) and v.strip():
                bits.append(v.strip())

        bits.append("open confident posture, natural hand gestures, hands relaxed; do NOT fold arms; do NOT clasp hands")
        bits.append("keep full head visible with headroom; do not crop forehead")
        bits = _dedupe_bits(bits)

        base_prompt = str(face_payload.get("user_prompt") or face_payload.get("prompt") or "").strip()
        if bits and base_prompt:
            base_prompt = base_prompt.rstrip(" ,") + ", " + ", ".join(bits)
        elif bits and not base_prompt:
            base_prompt = ", ".join(bits)

        if base_prompt:
            # svc-face contract
            face_payload["user_prompt"] = base_prompt
            face_payload["prompt"] = base_prompt

        # num_variants
        try:
            user_n = int(float(face_payload.get("num_variants"))) if face_payload.get("num_variants") is not None else 0
        except Exception:
            user_n = 0
        default_n = 6 if is_stage else 3
        face_payload["num_variants"] = max(1, min(8, user_n if user_n > 0 else default_n))

        # preferred_variations pools:
        attire_pool: List[str] = []
        bg_pool: List[str] = []
        cam_pool: List[str] = []
        pose_pool: List[str] = []

        if isinstance(mp_demo.get("attire"), str) and mp_demo["attire"].strip():
            attire_pool.append(mp_demo["attire"].strip())
        if diversity and isinstance(diversity.get("attire"), str) and diversity["attire"].strip():
            attire_pool.append(diversity["attire"].strip())
        if isinstance(cd, dict):
            ca = str(cd.get("attire") or "").strip()
            if ca:
                attire_pool.append(ca)

        if isinstance(mp_visual.get("background"), str) and mp_visual["background"].strip():
            bg_pool.append(mp_visual["background"].strip())
        if diversity and isinstance(diversity.get("background"), str) and diversity["background"].strip():
            bg_pool.append(diversity["background"].strip())

        if isinstance(cd, dict):
            sp = _friendly_scene(str(cd.get("scene_primary") or ""))
            if sp:
                bg_pool.append(sp)
            for s in _coerce_str_list(cd.get("scene_secondary"))[:4]:
                fs = _friendly_scene(s)
                if fs:
                    bg_pool.append(fs)

            cam_pool = _coerce_str_list(cd.get("camera"))
            if cd_pose:
                pose_pool.append(cd_pose)

        pv = face_payload.get("preferred_variations")
        if not isinstance(pv, list) or not pv:
            face_payload["preferred_variations"] = _identity_variations_with_scene(
                str(face_payload.get("user_prompt") or face_payload.get("prompt") or ""),
                seed=int(run_seed),
                n=int(face_payload.get("num_variants") or 6),
                attire_pool=_dedupe_bits(attire_pool),
                bg_pool=_dedupe_bits(bg_pool),
                camera_pool=_dedupe_bits(cam_pool),
                pose_pool=_dedupe_bits(pose_pool),
            )

        face_payload.setdefault("resolution", "hd")

        g = str(mp_demo.get("gender") or "").strip().lower()
        if g in ("male", "female"):
            if diversity_force or not str(face_payload.get("gender") or "").strip():
                face_payload["gender"] = g

        if not str(face_payload.get("request_nonce") or "").strip():
            face_payload["request_nonce"] = str(request_nonce)

        if "seed" not in face_payload or face_payload.get("seed") in (None, "", 0):
            face_payload["seed_mode"] = str(face_payload.get("seed_mode") or "deterministic")
            face_payload["seed"] = int(run_seed)

        # ---------- Audio generation (parallel) ----------
        async def generate_voice_audio() -> Tuple[str, Dict[str, Any], Optional[str], str]:
            user_audio_payload = as_dict((inp.inputs or {}).get("audio_payload"))
            user_text_given = bool(str(user_audio_payload.get("text") or "").strip())

            audio_payload = dict(user_audio_payload)

            story_text = self._story_narration_text(use_case)
            audio_payload.setdefault("text", story_text)

            mp_locale = str(mp_tts.get("target_locale") or "").strip()
            if mp_locale and not str(audio_payload.get("target_locale") or "").strip():
                audio_payload["target_locale"] = mp_locale
            audio_payload.setdefault("target_locale", _default_target_locale(getattr(use_case, "language_hint", "en")))

            if "voice_id" in audio_payload and "voice" not in audio_payload:
                audio_payload["voice"] = audio_payload.pop("voice_id")

            if "output_format" not in audio_payload:
                if "format" in audio_payload and isinstance(audio_payload.get("format"), str):
                    audio_payload["output_format"] = audio_payload.pop("format")
                else:
                    audio_payload["output_format"] = cfg_str("AUDIO_OUTPUT_FORMAT", "mp3")

            desired_gender = norm_gender(mp_demo.get("gender") or "")
            target_locale = str(audio_payload.get("target_locale") or "").strip() or "en-US"

            if not str(audio_payload.get("voice") or "").strip() and desired_gender in ("male", "female"):
                v = default_voice_for_locale_gender(target_locale, desired_gender)
                if v:
                    audio_payload["voice"] = v

            use_canned = cfg_bool("MARKETING_DIVERSITY_USE_CANNED_VOICEOVER", True)
            if use_canned and (not user_text_given) and (not self._story_script(use_case)):
                scene_hint = _friendly_scene(str(cd.get("scene_primary") or "")) if isinstance(cd, dict) else ""
                audio_payload["text"] = _canned_voiceover(target_locale, getattr(use_case, "industry", ""), festival_name=festival_name, scene_hint=scene_hint)

            audio_resp = await self.audio.create(dctx, audio_payload)
            audio_url = deep_find_audio_url(audio_resp) or deep_find_url(audio_resp)
            if not audio_url:
                raise MarketingRunFailed("VOICE_AUDIO_MISSING", f"audio_resp={truncate_json(audio_resp, 1600)}", stage="generate")

            voice_gender = infer_voice_gender(target_locale, str(audio_payload.get("voice") or ""))
            return audio_url, audio_resp, voice_gender, target_locale

        face_resp = await self.face.create(dctx, face_payload)
        face_job_id = extract_media(face_resp).get("job_id")

        audio_task: asyncio.Task[Tuple[str, Dict[str, Any], Optional[str], str]] = asyncio.create_task(generate_voice_audio())

        async def ensure_face_image(resp: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
            urls = extract_image_urls(resp)
            if urls:
                idx = stable_pick_index(pick_key, len(urls))
                resp2 = dict(resp or {})
                resp2["_variant_urls"] = urls
                resp2["_selected_index"] = idx
                return urls[idx], resp2
            raise MarketingRunFailed("FACE_IMAGE_URL_MISSING", f"face_resp={truncate_json(resp, 1600)}", stage="generate")

        try:
            face_image_url, face_resp2 = await ensure_face_image(face_resp)
        except Exception:
            audio_task.cancel()
            with contextlib.suppress(Exception):
                await audio_task
            raise

        voice_audio_url, audio_resp2, voice_gender, target_locale = await audio_task

        # -------- SAS refresh (CRITICAL) --------
        sas_hours = cfg_int("MARKETING_FUSION_ASSET_SAS_HOURS", 72)
        face_image_url_base = _strip_query(face_image_url)
        voice_audio_url_base = _strip_query(voice_audio_url)

        face_image_url_signed = _refresh_read_sas(face_image_url, sas_hours)
        voice_audio_url_signed = _refresh_read_sas(voice_audio_url, sas_hours)

        # Reframe face for fusion (extra headroom)
        tmp_dir = os.path.join(settings.OUTPUT_DIR, str(ctx.run_id), "fusion_tmp")
        face_for_fusion = await _reframe_face_image_and_upload(
            uploader=self.uploader,
            run_id=ctx.run_id,
            face_image_url=face_image_url_signed,
            out_dir=tmp_dir,
            aspect_ratio=aspect_ratio,
        )

        # HARD QC: voice gender must match desired gender
        desired_gender = norm_gender(mp_demo.get("gender") or "")
        qc_voice_matches_gender_or_fail(desired_gender=desired_gender, voice_gender=voice_gender, stage="generate")

        # ---------- Fusion payload ----------
        def build_fusion_payload(face_url: str, audio_url: str) -> Dict[str, Any]:
            user = as_dict((inp.inputs or {}).get("fusion_payload"))
            p: Dict[str, Any] = {}

            # face
            if user.get("face_artifact_id"):
                p["face_artifact_id"] = user["face_artifact_id"]
            elif user.get("heygen_talking_photo_id"):
                p["heygen_talking_photo_id"] = user["heygen_talking_photo_id"]
            elif user.get("image_key"):
                p["image_key"] = user["image_key"]
            else:
                chosen = user.get("face_image_url") or face_url
                p["face_image_url"] = _refresh_read_sas(str(chosen), sas_hours)

            # voice
            p["voice_mode"] = "audio"
            va = user.get("voice_audio")
            if isinstance(va, dict) and (va.get("audio_url") or va.get("audio_asset_id") or va.get("audio_artifact_id")):
                if isinstance(va.get("audio_url"), str):
                    va2 = dict(va)
                    va2["audio_url"] = _refresh_read_sas(va2["audio_url"], sas_hours)
                    p["voice_audio"] = va2
                else:
                    p["voice_audio"] = va
            else:
                p["voice_audio"] = {"type": "audio", "audio_url": _refresh_read_sas(audio_url, sas_hours)}

            # video settings
            video_user = as_dict(user.get("video") or (inp.inputs or {}).get("fusion_video") or {})
            video: Dict[str, Any] = dict(video_user)
            video["aspect_ratio"] = (video.get("aspect_ratio") or aspect_ratio)

            dur_raw = video.get("duration_sec")
            if dur_raw is None:
                dur_raw = getattr(use_case, "target_seconds", None) or 15
            try:
                dur = int(round(float(dur_raw)))
            except Exception:
                dur = int(round(float(getattr(use_case, "target_seconds", 15) or 15)))
            dur = max(6, min(60, dur))
            video["duration_sec"] = dur

            # Use mp_video overrides (possibly set by creative_direction)
            video.setdefault("motion_style", str(mp_video.get("motion_style") or "dynamic, natural hand gestures, confident"))
            video.setdefault("emotion", str(mp_video.get("emotion") or "friendly, confident"))

            # include scene/activity if present
            if isinstance(mp_visual.get("activity"), str) and mp_visual["activity"].strip():
                video.setdefault("activity", mp_visual["activity"].strip())
            if isinstance(mp_visual.get("scene"), str) and mp_visual["scene"].strip():
                video.setdefault("scene", mp_visual["scene"].strip())
            if isinstance(mp_visual.get("background"), str) and mp_visual["background"].strip():
                video.setdefault("background", mp_visual["background"].strip())

            p["video"] = video

            # consent
            consent = as_dict(user.get("consent") or (inp.inputs or {}).get("fusion_consent") or (inp.inputs or {}).get("consent") or {})
            if "external_provider_ok" not in consent:
                v = (inp.inputs or {}).get("external_provider_ok")
                if isinstance(v, bool):
                    consent["external_provider_ok"] = v
            if "external_provider_ok" not in consent:
                try:
                    if inp.mode == MarketingRunMode.stage:
                        consent["external_provider_ok"] = True
                except Exception:
                    pass
            if "external_provider_ok" not in consent and cfg_bool("MARKETING_ASSUME_EXTERNAL_PROVIDER_OK", False):
                consent["external_provider_ok"] = True

            consent_obj = {"external_provider_ok": bool(consent.get("external_provider_ok", False))}

            try:
                is_publish = (inp.mode == MarketingRunMode.publish)
            except Exception:
                is_publish = False

            if is_publish and not consent_obj["external_provider_ok"]:
                raise MarketingRunFailed(
                    "CONSENT_REQUIRED",
                    "svc-fusion needs consent.external_provider_ok=true for HeyGen; set inputs.fusion_consent.external_provider_ok=true",
                    stage="generate",
                )

            p["consent"] = consent_obj

            tags = as_dict(user.get("tags"))
            tags.setdefault("df_run_id", str(ctx.run_id))
            tags.setdefault("activity", str(mp_visual.get("activity") or ""))
            tags.setdefault("occasion", str(mp_visual.get("occasion") or ""))
            tags.setdefault("region", str(mp_demo.get("region") or ""))
            if festival_name:
                tags.setdefault("festival", festival_name)
            if isinstance(cd, dict) and str(cd.get("energy") or "").strip():
                tags.setdefault("energy", str(cd.get("energy") or ""))
            p["tags"] = tags

            # optional override
            if isinstance(user.get("provider"), str) and user["provider"]:
                p["provider"] = user["provider"]

            return p

        fusion_payload = build_fusion_payload(face_for_fusion, voice_audio_url_signed)

        # Try Fusion; if fails, fallback to montage
        reel_url: Optional[str] = None
        fusion_raw: Any = None

        try:
            vid = await self.fusion.create(dctx, fusion_payload)
            fusion_raw = vid
            vid_m = extract_media(vid)
            reel_url = vid_m.get("video_url") or deep_find_url(vid)
        except Exception as e:
            # Retry once with even longer SAS
            try:
                sas_hours2 = max(sas_hours, 96)
                fusion_payload2 = build_fusion_payload(face_for_fusion, _refresh_read_sas(voice_audio_url_signed, sas_hours2))
                vid2 = await self.fusion.create(dctx, fusion_payload2)
                fusion_raw = vid2
                vid_m2 = extract_media(vid2)
                reel_url = vid_m2.get("video_url") or deep_find_url(vid2)
            except Exception as e2:
                if cfg_bool("MARKETING_FUSION_FALLBACK_MONTAGE", True):
                    if not _which("ffmpeg"):
                        raise MarketingRunFailed(
                            "FUSION_FAILED_NO_FFMPEG",
                            f"Fusion failed and montage fallback needs ffmpeg. fusion_err={e2}",
                            stage="generate",
                        )
                    urls = as_dict(face_resp2).get("_variant_urls") or []
                    montage = await _render_montage_fallback(
                        uploader=self.uploader,
                        run_id=ctx.run_id,
                        image_urls=[_refresh_read_sas(u, sas_hours) for u in urls if isinstance(u, str)],
                        audio_url=_refresh_read_sas(voice_audio_url_signed, sas_hours),
                        aspect_ratio=aspect_ratio,
                        duration_sec=int(as_dict(fusion_payload.get("video") or {}).get("duration_sec") or 15),
                    )
                    reel_url = montage
                    fusion_raw = {"_fallback": "montage", "fusion_error": str(e2)}
                else:
                    raise MarketingRunFailed(
                        "FUSION_CREATE_FAILED",
                        f"{e2}. face_for_fusion={face_for_fusion} voice_audio_url={voice_audio_url_signed}",
                        stage="generate",
                    ) from e2

        if fmt in ("reel", "yt_short", "yt_long") and not reel_url:
            raise MarketingRunFailed("MISSING_REEL_URL", f"fusion_resp={truncate_json(fusion_raw or {}, 1600)}", stage="generate")

        return {
            "marketing_plan": marketing_plan,
            "creative_direction": cd,
            "festival": festival,
            "run_seed": int(run_seed),
            "request_nonce": str(request_nonce),
            "diversity": diversity,
            "face_job_id": face_job_id,
            "face_image_url": face_image_url_signed,
            "face_image_url_base": face_image_url_base,
            "face_variant_urls": as_dict(face_resp2).get("_variant_urls"),
            "face_selected_index": as_dict(face_resp2).get("_selected_index"),
            "fusion_face_image_url": face_for_fusion,
            "voice_audio_url": voice_audio_url_signed,
            "voice_audio_url_base": voice_audio_url_base,
            "fusion_payload": fusion_payload,
            "reel_url": reel_url,
            "face_raw": face_resp2,
            "audio_raw": audio_resp2,
            "fusion_raw": fusion_raw,
        }