BEGIN;

-- 1) SKU
INSERT INTO pricing_skus
    (code, name, unit, category, default_unit_credits, status, metadata_json)
VALUES
    (
        'LONGFORM_TALK_ECONOMY_MIN',
        'Talking Video Economy - per minute',
        'minute',
        'fusion_extension',
        0,
        'active',
        jsonb_build_object(
            'product_family', 'fusion_extension',
            'mode', 'talking_video',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'meter', 'final_output_minutes',
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_launch'
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

-- 2) Variant
INSERT INTO pricing_variants
    (code, name, category, is_active, metadata_json)
VALUES
    (
        'TALKING_VIDEO_ECONOMY',
        'Talking Video Economy',
        'fusion_extension',
        true,
        jsonb_build_object(
            'product_family', 'fusion_extension',
            'mode', 'talking_video',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'meter', 'final_output_minutes',
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_launch'
        )
    )
ON CONFLICT (code) DO UPDATE
SET
    name = EXCLUDED.name,
    category = EXCLUDED.category,
    is_active = EXCLUDED.is_active,
    metadata_json = EXCLUDED.metadata_json;

-- 3) Variant -> SKU mapping
DELETE FROM pricing_variant_lines
WHERE variant_code = 'TALKING_VIDEO_ECONOMY'
  AND sku_code = 'LONGFORM_TALK_ECONOMY_MIN';

INSERT INTO pricing_variant_lines
    (variant_code, sku_code, qty_mode, qty_param, metadata_json)
VALUES
    (
        'TALKING_VIDEO_ECONOMY',
        'LONGFORM_TALK_ECONOMY_MIN',
        'param',
        'minutes',
        jsonb_build_object(
            'meter', 'final_output_minutes',
            'quality_tier', 'economy',
            'provider_family', 'veed_fabric',
            'billing_entity', 'parent_longform_job',
            'seed', 'veed_economy_launch'
        )
    );

-- 4) Prices
DELETE FROM pricing_sku_prices sp
USING pricing_pricebooks pb
WHERE sp.pricebook_id = pb.id
  AND sp.sku_code = 'LONGFORM_TALK_ECONOMY_MIN'
  AND pb.is_active = true
  AND (
        (pb.tier_code = 'pro'      AND pb.currency = 'USD')
     OR (pb.tier_code = 'business' AND pb.currency = 'USD')
     OR (pb.tier_code = 'pro'      AND pb.currency = 'INR')
     OR (pb.tier_code = 'business' AND pb.currency = 'INR')
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
SELECT
    pb.id,
    'LONGFORM_TALK_ECONOMY_MIN',
    NULL,
    CASE
        WHEN pb.tier_code = 'pro'      AND pb.currency = 'USD' THEN 6.99
        WHEN pb.tier_code = 'business' AND pb.currency = 'USD' THEN 4.99
        WHEN pb.tier_code = 'pro'      AND pb.currency = 'INR' THEN 549
        WHEN pb.tier_code = 'business' AND pb.currency = 'INR' THEN 399
    END,
    NULL,
    NULL,
    jsonb_build_object(
        'seed', 'veed_economy_launch',
        'quality_tier', 'economy',
        'provider_family', 'veed_fabric',
        'meter', 'final_output_minutes',
        'billing_entity', 'parent_longform_job'
    )
FROM pricing_pricebooks pb
WHERE pb.is_active = true
  AND (
        (pb.tier_code = 'pro'      AND pb.currency = 'USD')
     OR (pb.tier_code = 'business' AND pb.currency = 'USD')
     OR (pb.tier_code = 'pro'      AND pb.currency = 'INR')
     OR (pb.tier_code = 'business' AND pb.currency = 'INR')
  );

COMMIT;

insert into pricing_sku_prices
(
    pricebook_id,
    sku_code,
    unit_credits_override,
    unit_money_override,
    min_qty,
    max_qty,
    metadata_json
)
values
(
    '11111111-1111-1111-1111-111111111111'::uuid,
    'LONGFORM_TALK_ECONOMY_MIN',
    null,
    4.99,
    null,
    null,
    jsonb_build_object(
        'seed', 'veed_economy_launch_global_usd_web_v1',
        'quality_tier', 'economy',
        'provider_family', 'veed_fabric',
        'meter', 'final_output_minutes',
        'billing_entity', 'parent_longform_job'
    )
)
on conflict (pricebook_id, sku_code) do update
set
    unit_money_override = excluded.unit_money_override,
    metadata_json = excluded.metadata_json;