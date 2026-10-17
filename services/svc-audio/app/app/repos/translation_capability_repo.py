"""Read-only translation route discovery from existing locale metadata."""
from __future__ import annotations
import json
from typing import Any

QUERY = """SELECT locale, language_code, meta_json FROM public.tts_locales
WHERE is_enabled AND (locale=$2 OR locale=$1 OR
(strpos($1,'-')=0 AND lower(language_code)=lower($1)))"""


async def list_approved_translation_routes(pool: Any, *, source_locale: str, target_locale: str) -> list[dict]:
    if not source_locale or not target_locale or source_locale == target_locale:
        return []
    rows = await pool.fetch(QUERY, source_locale, target_locale)
    source_locales = {r["locale"] for r in rows if r["locale"] == source_locale or (
        "-" not in source_locale and str(r["language_code"] or "").lower() == source_locale.lower()
    )}
    targets = [r for r in rows if r["locale"] == target_locale]
    if len(targets) != 1 or not source_locales:
        return []
    metadata = targets[0]["meta_json"] or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return []
    routing = metadata.get("translation_routing_v1") if isinstance(metadata, dict) else None
    if not isinstance(routing, dict) or routing.get("enabled") is not True:
        return []
    capabilities = routing.get("capabilities")
    if not isinstance(capabilities, list):
        return []
    eligible = []
    for c in capabilities:
        if not isinstance(c, dict) or c.get("enabled") is not True or c.get("approved") is not True:
            continue
        if c.get("source_locale") not in source_locales or c.get("target_locale") != target_locale:
            continue
        if not all(isinstance(c.get(k), str) and c[k].strip() for k in (
            "provider", "model", "provider_source_code", "provider_target_code"
        )):
            continue
        eligible.append({
            "provider_code": c["provider"], "model_code": c["model"],
            "source_locale": c["source_locale"], "target_locale": c["target_locale"],
            "provider_source_code": c["provider_source_code"],
            "provider_target_code": c["provider_target_code"],
            "priority": int(c.get("priority",100)),
        })
    return sorted(eligible,key=lambda r:(r["priority"],r["provider_code"],r["model_code"]))
