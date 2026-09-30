from __future__ import annotations

from typing import Optional


# desifaces product-level presentation modes are not automatically equivalent
# to provider-native style controls. Conversational and Narration describe the
# desired experience and can be fulfilled by a provider through its natural
# voice, speaker choice and pace even when it exposes no "style" API.
#
# Character remains a true expressive/native-style requirement until a provider
# has a separately certified mapping for that experience.
_PRODUCT_INTENT_WITHOUT_NATIVE_STYLE_REQUIREMENT = {
    "conversational",
    "narration",
}


def requires_provider_native_style(style: Optional[str]) -> bool:
    raw = str(style or "").strip().lower()

    if not raw:
        return False

    return raw not in _PRODUCT_INTENT_WITHOUT_NATIVE_STYLE_REQUIREMENT
