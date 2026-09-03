from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

FACE_MULTI_PERSON = "FACE_MULTI_PERSON"
AUDIO_MULTI_PERSON = "AUDIO_MULTI_PERSON"
FUSION_MULTI_PERSON = "FUSION_MULTI_PERSON"

MULTI_PERSON_MIN_PARTICIPANTS = 2
PRICING_POLICY = "multi_person_workload_v1"

_STUDIO_SKUS = {
    "face": FACE_MULTI_PERSON,
    "audio": AUDIO_MULTI_PERSON,
    "fusion": FUSION_MULTI_PERSON,
}


@dataclass(frozen=True)
class MultiPersonPricingSelection:
    studio: str
    participant_count: int
    sku_code: str
    variant_code: str
    natural_units: int
    quantity_param: str

    @property
    def billable_units(self) -> int:
        # Face identity stages already execute independently per participant, so
        # their natural units already represent the actual face workload. Audio
        # likewise captures workload through aggregate generated characters.
        # Fusion is the coordinated multi-person operation and therefore scales
        # duration by participant count (participant-minutes).
        if self.studio == "fusion":
            return max(1, self.natural_units * self.participant_count)
        return max(1, self.natural_units)

    @property
    def variant_params(self) -> dict[str, str]:
        return {self.quantity_param: str(self.billable_units)}

    @property
    def metadata(self) -> dict[str, Any]:
        participant_scaling = {
            "face": "per_character_natural_usage",
            "audio": "aggregate_natural_usage",
            "fusion": "natural_units_x_participants",
        }[self.studio]
        return {
            "multi_person": True,
            "premium": True,
            "participant_count": self.participant_count,
            "participant_count_in_sku": False,
            "natural_units": self.natural_units,
            "billable_units": self.billable_units,
            "participant_scaling": participant_scaling,
            "pricing_policy": PRICING_POLICY,
        }


def _positive_int(value: Any) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        return 0
    return parsed if parsed > 0 else 0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _count_from_string(value: str) -> int:
    """Return an explicitly encoded count from a string, or 0 when absent."""
    text = str(value or "").strip()
    if not text:
        return 0

    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, Mapping):
        return _count_from_mapping(parsed)

    for pattern in (
        r"(?:participant_count|participants_count|speaker_count|subject_count|people_count)\s*[:=]\s*(\d+)",
        r"(?:participants|speakers|subjects|people)\s*[:=]\s*(\d+)",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            count = _positive_int(match.group(1))
            if count:
                return count

    if re.search(
        r"(?:multi_person|multi_speaker)\s*[:=]\s*(?:true|1|yes|on)",
        text,
        flags=re.IGNORECASE,
    ):
        return MULTI_PERSON_MIN_PARTICIPANTS

    return 0


def _count_from_mapping(value: Mapping[str, Any]) -> int:
    # Explicit numeric counts are authoritative when supplied by the caller.
    for key in ("participant_count", "participants_count", "speaker_count", "subject_count", "people_count"):
        count = _positive_int(value.get(key))
        if count:
            return count

    # Explicit multi-person policy markers must take precedence over structural
    # defaults. Face request normalization materializes a one-item `subjects`
    # list for every single-person identity request, including identities that
    # Director creates inside a multi-person story. If we inspect that derived
    # list first, the premium orchestration context is incorrectly downgraded to
    # participant_count=1.
    if _truthy(value.get("multi_person")) or _truthy(value.get("multi_speaker")):
        return MULTI_PERSON_MIN_PARTICIPANTS

    pricing_context = value.get("pricing_context")
    if isinstance(pricing_context, Mapping):
        count = _count_from_mapping(pricing_context)
        if count:
            return count
    elif isinstance(pricing_context, str):
        count = _count_from_string(pricing_context)
        if count:
            return count

    for key in ("participants", "speakers", "subjects", "people"):
        items = value.get(key)
        if isinstance(items, (list, tuple, set)) and items:
            return len(items)

    composition = str(value.get("subject_composition") or value.get("composition") or "").strip().lower()
    if composition in {"two_people", "couple", "pair", "duo"}:
        return 2
    if composition in {"single", "single_person", "one_person"}:
        return 1

    for nested_key in ("context", "tags", "metadata", "generation_metadata", "preview_metadata"):
        nested = value.get(nested_key)
        if isinstance(nested, Mapping):
            count = _count_from_mapping(nested)
            if count:
                return count
        elif isinstance(nested, str):
            count = _count_from_string(nested)
            if count:
                return count

    return 0


def participant_count(value: Any, *, default: int = 1) -> int:
    """Resolve an explicitly supplied participant count without guessing from prose."""
    fallback = max(1, int(default))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _positive_int(value) or fallback

    if isinstance(value, Mapping):
        return _count_from_mapping(value) or fallback

    if isinstance(value, str):
        return _count_from_string(value) or fallback

    return fallback


def is_multi_person(value: Any) -> bool:
    return participant_count(value) >= MULTI_PERSON_MIN_PARTICIPANTS


def face_units(requested_variants: Any) -> int:
    return max(1, _positive_int(requested_variants) or 1)


def audio_units_from_chars(chars: Any) -> int:
    count = max(1, _positive_int(chars) or 1)
    return max(1, math.ceil(count / 1000))


def fusion_units_from_seconds(duration_sec: Any) -> int:
    seconds = max(1, _positive_int(duration_sec) or 1)
    return max(1, math.ceil(seconds / 60))


def select_multi_person_pricing(
    *,
    studio: str,
    participant_count_value: Any,
    natural_units: Any,
) -> Optional[MultiPersonPricingSelection]:
    studio_key = str(studio or "").strip().lower()
    sku = _STUDIO_SKUS.get(studio_key)
    if not sku:
        raise ValueError(f"unsupported studio for multi-person pricing: {studio}")

    count = participant_count(participant_count_value)
    if count < MULTI_PERSON_MIN_PARTICIPANTS:
        return None

    units = max(1, _positive_int(natural_units) or 1)
    quantity_param = {
        "face": "num_edits",
        "audio": "chars_1k",
        "fusion": "minutes",
    }[studio_key]

    return MultiPersonPricingSelection(
        studio=studio_key,
        participant_count=count,
        sku_code=sku,
        variant_code=sku,
        natural_units=units,
        quantity_param=quantity_param,
    )
