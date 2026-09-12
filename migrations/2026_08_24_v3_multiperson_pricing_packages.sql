BEGIN;

-- V3 multi-person pricing package configuration.
--
-- Current behavior is intentionally unchanged:
--   * Face, Audio and Fusion continue to use their existing svc-pricing variants.
--   * MULTIPERSON_STANDARD is a metadata/configuration envelope only.
--   * MULTIPERSON_PREMIUM is dormant and cannot charge until explicitly activated.
--
-- The purpose of this migration is to provide a durable DB-owned control point for
-- future premium Story/Multi-Person pricing without hard-coding package selection in
-- Director, mobile or individual Studio services.

CREATE TABLE IF NOT EXISTS pricing_experience_packages (
  package_code text PRIMARY KEY,
  experience_code text NOT NULL,
  display_name text NOT NULL,
  pricing_strategy text NOT NULL,
  package_variant_code text NULL REFERENCES pricing_variants(code),
  is_default boolean NOT NULL DEFAULT false,
  is_active boolean NOT NULL DEFAULT false,
  effective_from timestamptz NOT NULL DEFAULT now(),
  effective_to timestamptz NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_pricing_experience_packages_strategy
    CHECK (pricing_strategy IN ('component_passthrough','component_plus_package_variant'))
);

CREATE INDEX IF NOT EXISTS ix_pricing_experience_packages_lookup
  ON pricing_experience_packages(experience_code,is_active,is_default,effective_from DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ux_pricing_experience_packages_active_default
  ON pricing_experience_packages(experience_code)
  WHERE is_active = true AND is_default = true;

-- Dormant surcharge SKU. It carries zero default credits and is inactive so it can
-- never accidentally participate in today's pricing. Pricing Operations can later
-- activate it and use the existing pricebook/sku override model for country/channel/
-- tier-specific values. ON CONFLICT deliberately preserves status/default credits so
-- a future Pricing Operations activation is never silently undone by a replay.
INSERT INTO pricing_skus(
  code,name,unit,category,provider_hint,default_unit_credits,status,metadata_json
)
VALUES (
  'V3_MULTIPERSON_PREMIUM_SURCHARGE',
  'V3 Multi-Person Premium Experience Surcharge',
  'run',
  'fusion',
  NULL,
  0,
  'inactive',
  jsonb_build_object(
    'experience_code','face_audio_fusion_story',
    'package_code','V3_MULTIPERSON_PREMIUM',
    'activation_policy','explicit_pricing_ops_activation_only',
    'pricing_owner','svc-pricing',
    'note','Dormant future premium package surcharge; existing Face/Audio/Fusion component pricing remains authoritative.'
  )
)
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  unit = EXCLUDED.unit,
  category = EXCLUDED.category,
  provider_hint = EXCLUDED.provider_hint,
  metadata_json = pricing_skus.metadata_json || EXCLUDED.metadata_json;

INSERT INTO pricing_variants(code,name,category,is_active,metadata_json)
VALUES (
  'V3_MULTIPERSON_STORY_PREMIUM',
  'V3 Multi-Person Story Premium Package',
  'fusion',
  false,
  jsonb_build_object(
    'experience_code','face_audio_fusion_story',
    'package_code','V3_MULTIPERSON_PREMIUM',
    'composition','existing_component_quotes_plus_optional_package_surcharge',
    'activation_policy','explicit_pricing_ops_activation_only'
  )
)
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  category = EXCLUDED.category,
  metadata_json = pricing_variants.metadata_json || EXCLUDED.metadata_json;

INSERT INTO pricing_variant_lines(
  variant_code,sku_code,qty_mode,qty_value,qty_param,metadata_json
)
VALUES (
  'V3_MULTIPERSON_STORY_PREMIUM',
  'V3_MULTIPERSON_PREMIUM_SURCHARGE',
  'fixed',
  1,
  '',
  jsonb_build_object(
    'purpose','future_multi_person_premium_surcharge',
    'current_charge_behavior','disabled'
  )
)
ON CONFLICT (variant_code,sku_code,qty_mode,qty_param) DO UPDATE SET
  metadata_json = pricing_variant_lines.metadata_json || EXCLUDED.metadata_json;

INSERT INTO pricing_experience_packages(
  package_code,experience_code,display_name,pricing_strategy,package_variant_code,
  is_default,is_active,metadata_json
)
VALUES
(
  'V3_MULTIPERSON_STANDARD',
  'face_audio_fusion_story',
  'Multi-Person Story',
  'component_passthrough',
  NULL,
  true,
  true,
  jsonb_build_object(
    'face_pricing','existing_svc_face_preview_reserve_commit',
    'audio_pricing','existing_svc_audio_preview_reserve_commit',
    'fusion_pricing','existing_svc_fusion_preview_reserve_commit',
    'frontend_rule','display_backend_quotes_only',
    'premium_enabled',false
  )
),
(
  'V3_MULTIPERSON_PREMIUM',
  'face_audio_fusion_story',
  'Multi-Person Story Premium',
  'component_plus_package_variant',
  'V3_MULTIPERSON_STORY_PREMIUM',
  false,
  false,
  jsonb_build_object(
    'activation_policy','future_explicit_launch',
    'requires_variant_active',true,
    'requires_sku_active',true,
    'requires_pricebook_configuration',true,
    'component_pricing_remains_authoritative',true,
    'frontend_rule','never_compute_premium_price_locally'
  )
)
ON CONFLICT (package_code) DO UPDATE SET
  experience_code = EXCLUDED.experience_code,
  display_name = EXCLUDED.display_name,
  pricing_strategy = EXCLUDED.pricing_strategy,
  package_variant_code = EXCLUDED.package_variant_code,
  -- is_default/is_active are operational state. Preserve them on replay so future
  -- package activation/deactivation remains an explicit pricing decision.
  metadata_json = pricing_experience_packages.metadata_json || EXCLUDED.metadata_json,
  updated_at = now();

COMMIT;
