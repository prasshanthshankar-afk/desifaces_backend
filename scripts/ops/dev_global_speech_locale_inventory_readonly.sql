-- DEV READ ONLY — inventory every enabled desifaces locale and its eligible TTS provider.
-- Execute with a READ ONLY DB role on desifaces-dev. Never run on production.
-- This inventories live DB configuration; it does not prove API success or
-- independent translation support. No prices, payments, credits or writes.
BEGIN READ ONLY;
SELECT 'enabled_locale_count' AS metric, count(*) AS value FROM public.tts_locales WHERE is_enabled;
SELECT 'enabled_language_count' AS metric, count(DISTINCT COALESCE(NULLIF(language_code,''),translator_lang)) AS value
FROM public.tts_locales WHERE is_enabled;
SELECT 'enabled_user_selectable_locale_count' AS metric, count(*) AS value
FROM public.tts_locales WHERE is_enabled AND is_user_selectable;

WITH active_models AS (
 SELECT m.provider_code, m.model_code, p.routing_enabled AS provider_routing,
        m.routing_enabled AS model_routing
 FROM public.tts_provider_models m JOIN public.tts_providers p USING (provider_code)
 WHERE p.is_enabled AND m.is_enabled
), mapped AS (
 SELECT l.locale, l.language_code, l.translator_lang, l.translate_supported,
        l.tts_supported, l.is_user_selectable, m.provider_code, m.model_code,
        c.provider_locale_code, m.provider_routing, m.model_routing
 FROM public.tts_locales l
 LEFT JOIN public.tts_model_locale_capabilities c ON c.locale=l.locale
   AND c.is_enabled AND c.is_approved
 LEFT JOIN active_models m ON m.provider_code=c.provider_code AND m.model_code=c.model_code
 WHERE l.is_enabled
)
SELECT locale, language_code, translator_lang, translate_supported, tts_supported,
       is_user_selectable,
       COALESCE(string_agg(DISTINCT provider_code||':'||model_code||
            ' ['||CASE WHEN provider_routing AND model_routing THEN 'ROUTABLE' ELSE 'CONFIGURED' END||']',
            '; ' ORDER BY provider_code||':'||model_code||
            ' ['||CASE WHEN provider_routing AND model_routing THEN 'ROUTABLE' ELSE 'CONFIGURED' END||']')
             FILTER (WHERE provider_code IS NOT NULL), 'NONE') AS eligible_locale_models,
       COALESCE(bool_or(provider_routing AND model_routing),false) AS any_locale_model_routable
FROM mapped
GROUP BY locale, language_code, translator_lang, translate_supported, tts_supported, is_user_selectable
ORDER BY locale;
-- Independently audit actual configured voices, model-language eligibility,
-- translation routing, credentials, and live provider call results. Locale
-- model inventory alone is not proof of a fully eligible voice.
ROLLBACK;
