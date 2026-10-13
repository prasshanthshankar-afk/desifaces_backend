from __future__ import annotations

from typing import Optional


_SARVAM_RELAXED_PRODUCT_INTENTS = {
    "conversational",
    "narration",
}


def relax_native_style_only_for_eligible_sarvam_voice(
    style: Optional[str],
    *,
    sarvam_voice_eligible: bool,
) -> bool:
    """
    Narrow Sarvam-only compatibility rule.

    Existing Audio style semantics remain unchanged for every non-Sarvam
    request. Conversational/Narration may bypass provider-native style
    capability only when the explicitly selected voice has already been
    verified by DB masterdata as an enabled/routable Sarvam voice for the
    requested locale.
    """
    if not sarvam_voice_eligible:
        return False

    raw = str(style or "").strip().lower()
    return raw in _SARVAM_RELAXED_PRODUCT_INTENTS
