-- ============================================================
-- #eip7
-- Global quality-first routing policy.
--
-- IMPORTANT:
-- No provider order.
-- No country order.
-- No language order.
--
-- Eligibility comes from DB capability rows.
-- Ranking is quality/fit/health/latency/cost driven.
-- ============================================================

BEGIN;

INSERT INTO public.tts_routing_policies (
    policy_code,
    display_name,
    description,
    require_approved_capability,
    require_approved_quality,
    allow_provider_fallback,
    is_default,
    is_enabled,
    meta_json
)
VALUES (
    'global_quality_first',
    'Global Quality First',
    'Quality-first global TTS selection with capability, accent, health, latency and cost evaluation.',
    true,
    false,
    true,
    true,
    true,
    '{"scope":"svc-audio","provider_neutral":true}'::jsonb
)
ON CONFLICT (policy_code) DO UPDATE
SET
    display_name = EXCLUDED.display_name,
    description = EXCLUDED.description,
    require_approved_capability = EXCLUDED.require_approved_capability,
    allow_provider_fallback = EXCLUDED.allow_provider_fallback,
    is_default = EXCLUDED.is_default,
    is_enabled = EXCLUDED.is_enabled,
    meta_json = EXCLUDED.meta_json;


INSERT INTO public.tts_routing_policy_weights (
    policy_code,
    dimension_code,
    weight,
    sort_order,
    meta_json
)
VALUES
(
    'global_quality_first',
    'quality',
    0.6000,
    10,
    '{"hard_priority":true}'::jsonb
),
(
    'global_quality_first',
    'accent_fit',
    0.1500,
    20,
    '{}'::jsonb
),
(
    'global_quality_first',
    'style_fit',
    0.1000,
    30,
    '{}'::jsonb
),
(
    'global_quality_first',
    'provider_health',
    0.0750,
    40,
    '{}'::jsonb
),
(
    'global_quality_first',
    'latency',
    0.0500,
    50,
    '{}'::jsonb
),
(
    'global_quality_first',
    'cost',
    0.0250,
    60,
    '{}'::jsonb
)
ON CONFLICT (policy_code, dimension_code)
DO UPDATE SET
    weight = EXCLUDED.weight,
    sort_order = EXCLUDED.sort_order,
    is_enabled = true,
    meta_json = EXCLUDED.meta_json;


COMMIT;
