BEGIN;

-- =========================================================
-- 0) Keep historical SKUs. Do NOT delete them.
--    Just mark old economy SKUs inactive/superseded.
-- =========================================================
UPDATE pricing_skus
SET
    status = 'inactive',
    metadata_json = COALESCE(metadata_json, '{}'::jsonb) || jsonb_build_object(
        'superseded_by_seed', 'veed_economy_10_20_30_v1',
        'superseded_at', now()::text
    )
WHERE code IN (
    'LONGFORM_TALK_ECONOMY_MIN',
    'LONGFORM_TALK_ECONOMY_15S',
    'LONGFORM_TALK_ECONOMY_30S',
    'LONGFORM_TALK_ECONOMY_60S'
);

-- Optional: keep old variants but deactivate them
UPDATE pricing_variants
SET
    is_active = false,
    metadata_json = COALESCE(metadata_json, '{}'::jsonb) || jsonb_build_object(
        'superseded_by_seed', 'veed_economy_10_20_30_v1',
        'superseded_at', now()::text
    )
WHERE code IN (
    'TALKING_VIDEO_ECONOMY',
    'TALKING_VIDEO_ECONOMY_15S',
    'TALKING_VIDEO_ECONOMY_30S',
    'TALKING_VIDEO_ECONOMY_60S'
);

-- Remove old active mappings so new pricing cannot resolve through them
DELETE FROM pricing_variant_lines
WHERE variant_code IN (
    'TALKING_VIDEO_ECONOMY',
    'TALKING_VIDEO_ECONOMY_15S',
    'TALKING_VIDEO_ECONOMY_30S',
    'TALKING_VIDEO_ECONOMY_60S'
);

-- Remove old price rows from the active global USD web pricebook
DELETE FROM pricing_sku_prices
WHERE pricebook_id = '11111111-1111-1111-1111-111111111111'::uuid
  AND sku_code IN (
      'LONGFORM_TALK_ECONOMY_MIN',
      'LONGFORM_TALK_ECONOMY_15S',
      'LONGFORM_TALK_ECONOMY_30S',
      'LONGFORM_TALK_ECONOMY_60S'
  );

-- =========================================================
-- 1) New active SKUs: 10s / 20s / 30s
-- =========================================================
INSERT INTO pricing_skus
    (code, name, unit, category, default_unit_credits, status, metadata_json)
VALUES
    (
        'LONGFORM_TALK_ECONOMY_10S',
        'Talking Video Economy - up to 10 seconds',
        'job',
        'fusion_extension',
        0,
        'active',
        jsonb_build_object(
            'product_family', 'fusion_extension',
            'mode', 'talking_video',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'resolution', '480p',
            'bucket_kind', 'duration_band',
            'bucket_max_sec', 10,
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_10_20_30_v1'
        )
    ),
    (
        'LONGFORM_TALK_ECONOMY_20S',
        'Talking Video Economy - 11 to 20 seconds',
        'job',
        'fusion_extension',
        0,
        'active',
        jsonb_build_object(
            'product_family', 'fusion_extension',
            'mode', 'talking_video',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'resolution', '480p',
            'bucket_kind', 'duration_band',
            'bucket_max_sec', 20,
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_10_20_30_v1'
        )
    ),
    (
        'LONGFORM_TALK_ECONOMY_30S',
        'Talking Video Economy - 21 to 30 seconds',
        'job',
        'fusion_extension',
        0,
        'active',
        jsonb_build_object(
            'product_family', 'fusion_extension',
            'mode', 'talking_video',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'resolution', '480p',
            'bucket_kind', 'duration_band',
            'bucket_max_sec', 30,
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_10_20_30_v1'
        )
    )
ON CONFLICT (code) DO UPDATE
SET
    name = EXCLUDED.name,
    unit = EXCLUDED.unit,
    category = EXCLUDED.category,
    default_unit_credits = EXCLUDED.default_unit_credits,
    status = EXCLUDED.status,
    metadata_json = EXCLUDED.metadata_json;

-- =========================================================
-- 2) New active variants
-- =========================================================
INSERT INTO pricing_variants
    (code, name, category, is_active, metadata_json)
VALUES
    (
        'TALKING_VIDEO_ECONOMY_10S',
        'Talking Video Economy (<=10s)',
        'fusion_extension',
        true,
        jsonb_build_object(
            'product_family', 'fusion_extension',
            'mode', 'talking_video',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'resolution', '480p',
            'bucket_max_sec', 10,
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_10_20_30_v1'
        )
    ),
    (
        'TALKING_VIDEO_ECONOMY_20S',
        'Talking Video Economy (11-20s)',
        'fusion_extension',
        true,
        jsonb_build_object(
            'product_family', 'fusion_extension',
            'mode', 'talking_video',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'resolution', '480p',
            'bucket_max_sec', 20,
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_10_20_30_v1'
        )
    ),
    (
        'TALKING_VIDEO_ECONOMY_30S',
        'Talking Video Economy (21-30s)',
        'fusion_extension',
        true,
        jsonb_build_object(
            'product_family', 'fusion_extension',
            'mode', 'talking_video',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'resolution', '480p',
            'bucket_max_sec', 30,
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_10_20_30_v1'
        )
    )
ON CONFLICT (code) DO UPDATE
SET
    name = EXCLUDED.name,
    category = EXCLUDED.category,
    is_active = EXCLUDED.is_active,
    metadata_json = EXCLUDED.metadata_json;

-- =========================================================
-- 3) New active variant mappings
-- =========================================================
DELETE FROM pricing_variant_lines
WHERE variant_code IN (
    'TALKING_VIDEO_ECONOMY_10S',
    'TALKING_VIDEO_ECONOMY_20S',
    'TALKING_VIDEO_ECONOMY_30S'
);

INSERT INTO pricing_variant_lines
    (variant_code, sku_code, qty_mode, qty_param, metadata_json)
VALUES
    (
        'TALKING_VIDEO_ECONOMY_10S',
        'LONGFORM_TALK_ECONOMY_10S',
        'param',
        'requested_units',
        jsonb_build_object(
            'bucket_max_sec', 10,
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_10_20_30_v1'
        )
    ),
    (
        'TALKING_VIDEO_ECONOMY_20S',
        'LONGFORM_TALK_ECONOMY_20S',
        'param',
        'requested_units',
        jsonb_build_object(
            'bucket_max_sec', 20,
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_10_20_30_v1'
        )
    ),
    (
        'TALKING_VIDEO_ECONOMY_30S',
        'LONGFORM_TALK_ECONOMY_30S',
        'param',
        'requested_units',
        jsonb_build_object(
            'bucket_max_sec', 30,
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_10_20_30_v1'
        )
    );

-- =========================================================
-- 4) New active price rows
-- =========================================================
DELETE FROM pricing_sku_prices
WHERE pricebook_id = '11111111-1111-1111-1111-111111111111'::uuid
  AND sku_code IN (
      'LONGFORM_TALK_ECONOMY_10S',
      'LONGFORM_TALK_ECONOMY_20S',
      'LONGFORM_TALK_ECONOMY_30S'
  );

INSERT INTO pricing_sku_prices
    (
        pricebook_id,
        sku_code,
        unit_credits_override,
        unit_money_override,
        min_qty,
        max_qty,
        metadata_json
    )
VALUES
    (
        '11111111-1111-1111-1111-111111111111'::uuid,
        'LONGFORM_TALK_ECONOMY_10S',
        NULL,
        1.99,
        NULL,
        NULL,
        jsonb_build_object(
            'seed', 'veed_economy_10_20_30_v1',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'resolution', '480p',
            'bucket_max_sec', 10,
            'billing_entity', 'parent_longform_job'
        )
    ),
    (
        '11111111-1111-1111-1111-111111111111'::uuid,
        'LONGFORM_TALK_ECONOMY_20S',
        NULL,
        2.99,
        NULL,
        NULL,
        jsonb_build_object(
            'seed', 'veed_economy_10_20_30_v1',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'resolution', '480p',
            'bucket_max_sec', 20,
            'billing_entity', 'parent_longform_job'
        )
    ),
    (
        '11111111-1111-1111-1111-111111111111'::uuid,
        'LONGFORM_TALK_ECONOMY_30S',
        NULL,
        3.99,
        NULL,
        NULL,
        jsonb_build_object(
            'seed', 'veed_economy_10_20_30_v1',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'resolution', '480p',
            'bucket_max_sec', 30,
            'billing_entity', 'parent_longform_job'
        )
    )
ON CONFLICT (pricebook_id, sku_code) DO UPDATE
SET
    unit_money_override = EXCLUDED.unit_money_override,
    metadata_json = EXCLUDED.metadata_json;

COMMIT;