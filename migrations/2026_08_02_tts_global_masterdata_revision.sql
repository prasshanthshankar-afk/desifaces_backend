-- ============================================================
-- #eip7
-- Global TTS masterdata revision control.
--
-- Every committed mutation to TTS catalog/configuration data
-- increments masterdata_revision(domain='tts').
--
-- This supports safe cache invalidation and catalog refresh without
-- embedding masterdata knowledge in application source.
-- ============================================================

BEGIN;

INSERT INTO public.masterdata_revision (
    domain,
    revision,
    updated_at
)
VALUES (
    'tts',
    0,
    now()
)
ON CONFLICT (domain) DO NOTHING;


CREATE OR REPLACE FUNCTION public.fn_touch_tts_masterdata_revision()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE public.masterdata_revision
       SET revision = revision + 1,
           updated_at = now()
     WHERE domain = 'tts';

    RETURN NULL;
END;
$$;


DO $$
DECLARE
    table_name text;
    trigger_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'tts_languages',
        'tts_locales',
        'tts_locale_aliases',
        'tts_locale_context_rules',
        'tts_providers',
        'tts_provider_models',
        'tts_model_language_capabilities',
        'tts_model_locale_capabilities',
        'tts_voices',
        'tts_voice_locale_capabilities',
        'tts_voice_model_capabilities',
        'tts_quality_profiles',
        'tts_provider_cost_profiles',
        'tts_routing_policies',
        'tts_routing_policy_weights'
    ]
    LOOP
        IF to_regclass('public.' || table_name) IS NULL THEN
            RAISE EXCEPTION
                'required TTS masterdata table missing: %',
                table_name;
        END IF;

        trigger_name := 'trg_tts_mdrev_' || table_name;

        EXECUTE format(
            'DROP TRIGGER IF EXISTS %I ON public.%I',
            trigger_name,
            table_name
        );

        EXECUTE format(
            'CREATE TRIGGER %I
             AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE
             ON public.%I
             FOR EACH STATEMENT
             EXECUTE FUNCTION public.fn_touch_tts_masterdata_revision()',
            trigger_name,
            table_name
        );
    END LOOP;
END;
$$;


-- Mark the already-applied #eip7 global foundation as a new
-- masterdata revision. Trigger-controlled increments take over
-- after this migration.
UPDATE public.masterdata_revision
SET
    revision = revision + 1,
    updated_at = now()
WHERE domain = 'tts';


COMMIT;
