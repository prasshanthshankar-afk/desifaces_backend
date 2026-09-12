from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


_PREFIX_RE = re.compile(r"^participant(?:[\s:/#_-]+)", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^\w]+", re.UNICODE)


def _reference_key(value: str) -> str:
    """Return a conservative comparison key for LLM participant references.

    This intentionally normalizes only presentation-level variation (case,
    Unicode compatibility forms, punctuation/spacing, and an optional
    ``participant:``-style prefix). It never invents a participant or performs
    fuzzy matching.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = _PREFIX_RE.sub("", text)
    text = _NON_ALNUM_RE.sub("-", text).strip("-")
    return text


def normalize_participant_reference(
    reference: str | None,
    participant_names: Iterable[str],
) -> str | None:
    """Resolve a wire-level participant reference to one canonical display name.

    Exact display-name matches win. Otherwise harmless aliases such as
    ``participant:ananya``, ``Participant / Ananya`` or case/slug variants are
    accepted only when they resolve uniquely. Unknown or ambiguous references
    are returned unchanged so canonical Story validation still rejects them.
    """

    if reference is None:
        return None
    raw = str(reference).strip()
    if not raw:
        return raw

    names = tuple(str(name).strip() for name in participant_names if str(name).strip())
    for name in names:
        if raw == name:
            return name

    key = _reference_key(raw)
    if not key:
        return raw

    matches = {name for name in names if _reference_key(name) == key}
    if len(matches) == 1:
        return next(iter(matches))
    return raw
