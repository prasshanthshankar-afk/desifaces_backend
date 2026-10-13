"""SQL policy preference for automatic TTS choices; eligibility remains unchanged."""

def provider_preference_rank_sql(*, provider_expr: str, language_expr: str) -> str:
    # Expressions come only from application-owned aliases, never request input.
    return f"""CASE WHEN {provider_expr} = (
        SELECT rp.meta_json->'preferred_provider_by_language'->>{language_expr}
        FROM public.tts_routing_policies rp
        WHERE rp.is_default = TRUE AND rp.is_enabled = TRUE
        ORDER BY rp.policy_code LIMIT 1
    ) THEN 0 ELSE 1 END"""

