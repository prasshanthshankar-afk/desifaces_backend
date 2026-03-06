# services/svc-marketing/app/app/services/orchestration/qc/gender_qc.py
from __future__ import annotations

from typing import Dict, Optional

from app.services.orchestration.errors import MarketingRunFailed


def norm_gender(g: str | None) -> Optional[str]:
    if not g:
        return None
    s = str(g).strip().lower()
    if s in ("m", "male", "man", "boy", "masculine"):
        return "male"
    if s in ("f", "female", "woman", "girl", "feminine"):
        return "female"
    if s in ("unknown", "uncertain", "na", "n/a"):
        return None
    if s in ("nb", "nonbinary", "non-binary", "other"):
        return "other"
    return s


DEFAULT_TTS_VOICE_BY_LOCALE_GENDER: Dict[str, Dict[str, str]] = {
    "en-US": {"female": "en-US-JennyNeural", "male": "en-US-GuyNeural"},
    "en-IN": {"female": "en-IN-NeerjaNeural", "male": "en-IN-PrabhatNeural"},
    "hi-IN": {"female": "hi-IN-SwaraNeural", "male": "hi-IN-MadhurNeural"},
    "ta-IN": {"female": "ta-IN-PallaviNeural", "male": "ta-IN-ValluvarNeural"},
    "te-IN": {"female": "te-IN-ShrutiNeural", "male": "te-IN-MohanNeural"},
    "kn-IN": {"female": "kn-IN-SapnaNeural", "male": "kn-IN-GaganNeural"},
    "ml-IN": {"female": "ml-IN-SobhanaNeural", "male": "ml-IN-MidhunNeural"},
    "mr-IN": {"female": "mr-IN-AarohiNeural", "male": "mr-IN-ManoharNeural"},
    "bn-IN": {"female": "bn-IN-TanishaaNeural", "male": "bn-IN-BashkarNeural"},
    "gu-IN": {"female": "gu-IN-DhwaniNeural", "male": "gu-IN-NiranjanNeural"},
    "pa-IN": {"female": "pa-IN-GurleenNeural", "male": "pa-IN-AjitNeural"},
}


def default_voice_for_locale_gender(target_locale: str, gender: str) -> Optional[str]:
    loc = (target_locale or "en-US").strip()
    g = norm_gender(gender)
    if g not in ("male", "female"):
        return None
    m = DEFAULT_TTS_VOICE_BY_LOCALE_GENDER.get(loc) or DEFAULT_TTS_VOICE_BY_LOCALE_GENDER.get("en-US") or {}
    return m.get(g)


def infer_voice_gender(target_locale: str, voice: str | None) -> Optional[str]:
    v = (voice or "").strip()
    if not v:
        return None
    loc = (target_locale or "en-US").strip()
    m = DEFAULT_TTS_VOICE_BY_LOCALE_GENDER.get(loc) or {}
    for g, vv in m.items():
        if vv == v:
            return g
    low = v.lower()
    if "female" in low:
        return "female"
    if "male" in low:
        return "male"
    return None


def qc_voice_matches_gender_or_fail(*, desired_gender: str | None, voice_gender: str | None, stage: str = "generate") -> None:
    dg = norm_gender(desired_gender)
    vg = norm_gender(voice_gender)
    if dg in ("male", "female") and not vg:
        raise MarketingRunFailed("QC_GENDER_VOICE_UNKNOWN", f"Could not infer voice gender for desired_gender={dg}", stage=stage)
    if dg in ("male", "female") and vg != dg:
        raise MarketingRunFailed("QC_GENDER_VOICE_MISMATCH", f"Voice gender={vg} mismatches desired_gender={dg}", stage=stage)