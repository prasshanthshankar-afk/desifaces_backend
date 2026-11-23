from __future__ import annotations
import json

class TranslationRouteRejected(RuntimeError):
    pass


async def resolve_approved_translation_route(
    pool, *, source_locale: str, target_locale: str
) -> dict[str, str]:
    """Resolve one explicitly approved DB route without executing it.

    This is route selection, not DEV cohort authorization, credential
    verification, model/voice resolution, or quality certification.
    """
    source_locale = str(source_locale or "").strip()
    target_locale = str(target_locale or "").strip()

    if (
        not source_locale
        or not target_locale
        or source_locale.lower() == "auto"
    ):
        raise TranslationRouteRejected(
            "explicit_source_and_target_locale_required"
        )

    # Existing-schema translation contract: stored only in target locale
    # meta_json.translation_routing_v1. No new table and no implicit fallback.
    records = await pool.fetch(
        """SELECT locale, language_code, meta_json
           FROM public.tts_locales
           WHERE is_enabled AND (
             locale=$2 OR locale=$1 OR
             (strpos($1,'-')=0 AND lower(language_code)=lower($1))
           )""",
        source_locale, target_locale,
    )
    source_locales = {
        r["locale"] for r in records
        if r["locale"] == source_locale or (
            "-" not in source_locale and
            str(r["language_code"] or "").lower() == source_locale.lower()
        )
    }
    targets = [r for r in records if r["locale"] == target_locale]
    if len(targets) != 1 or not source_locales:
        raise TranslationRouteRejected("no_approved_translation_direction")
    metadata = targets[0]["meta_json"] or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError as exc:
            raise TranslationRouteRejected("invalid_translation_metadata") from exc
    routing = metadata.get("translation_routing_v1") if isinstance(metadata, dict) else None
    if not isinstance(routing, dict) or routing.get("enabled") is not True:
        raise TranslationRouteRejected("no_approved_translation_direction")
    entries = routing.get("capabilities")
    if not isinstance(entries, list):
        raise TranslationRouteRejected("invalid_translation_metadata")
    candidates = []
    for c in entries:
        if not isinstance(c, dict) or c.get("approved") is not True or c.get("enabled") is not True:
            continue
        if c.get("source_locale") not in source_locales or c.get("target_locale") != target_locale:
            continue
        if not all(isinstance(c.get(k), str) and c[k].strip() for k in (
            "provider", "model", "provider_source_code", "provider_target_code"
        )):
            continue
        candidates.append(c)
    if not candidates:
        raise TranslationRouteRejected("no_approved_translation_direction")
    exact = [c for c in candidates if c["source_locale"] == source_locale]
    pool_candidates = exact or candidates
    if len({c["source_locale"] for c in pool_candidates}) != 1:
        raise TranslationRouteRejected("ambiguous_source_locale")
    if len(pool_candidates) != 1:
        raise TranslationRouteRejected("ambiguous_translation_route")
    c = pool_candidates[0]
    row = dict(provider_code=c["provider"], model_code=c["model"],
               provider_source_code=c["provider_source_code"],
               provider_target_code=c["provider_target_code"])
    return row
