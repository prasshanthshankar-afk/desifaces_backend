begin;

-- =========================================================
-- TALKING VIDEO PREMIUM
-- Mirrors economy 10s / 20s / 30s buckets
-- Premium = 1.15 x Economy
-- Provider family = veed_fabric
-- Rerunnable: deletes premium variant-lines/prices first
-- =========================================================

-- ---------------------------------------------------------
-- 1) premium variants
-- ---------------------------------------------------------
insert into pricing_variants (code, name, category, is_active)
values
  ('TALKING_VIDEO_PREMIUM_10S', 'Talking Video Premium (<=10s)', 'fusion_extension', true),
  ('TALKING_VIDEO_PREMIUM_20S', 'Talking Video Premium (11-20s)', 'fusion_extension', true),
  ('TALKING_VIDEO_PREMIUM_30S', 'Talking Video Premium (21-30s)', 'fusion_extension', true)
on conflict (code) do update
set
  name = excluded.name,
  category = excluded.category,
  is_active = excluded.is_active;

-- ---------------------------------------------------------
-- 2) premium SKUs cloned from economy SKUs
-- preserves required fields like default_unit_credits
-- ---------------------------------------------------------
insert into pricing_skus (
  code,
  name,
  unit,
  category,
  default_unit_credits,
  status,
  metadata_json
)
select
  'LONGFORM_TALK_PREMIUM_10S' as code,
  'Talking Video Premium - up to 10 seconds' as name,
  unit,
  category,
  default_unit_credits,
  status,
  coalesce(metadata_json, '{}'::jsonb)
    || jsonb_build_object(
         'mode', 'talking_video',
         'quality_tier', 'premium',
         'bucket_kind', 'duration_band',
         'bucket_max_sec', 10,
         'product_family', 'fusion_extension',
         'provider_family', 'veed_fabric',
         'billing_entity', 'parent_longform_job',
         'resolution', 'premium',
         'seed', 'veed_premium_10_20_30_v1'
       )
from pricing_skus
where code = 'LONGFORM_TALK_ECONOMY_10S'
on conflict (code) do update
set
  name = excluded.name,
  unit = excluded.unit,
  category = excluded.category,
  default_unit_credits = excluded.default_unit_credits,
  status = excluded.status,
  metadata_json = excluded.metadata_json;

insert into pricing_skus (
  code,
  name,
  unit,
  category,
  default_unit_credits,
  status,
  metadata_json
)
select
  'LONGFORM_TALK_PREMIUM_20S' as code,
  'Talking Video Premium - 11 to 20 seconds' as name,
  unit,
  category,
  default_unit_credits,
  status,
  coalesce(metadata_json, '{}'::jsonb)
    || jsonb_build_object(
         'mode', 'talking_video',
         'quality_tier', 'premium',
         'bucket_kind', 'duration_band',
         'bucket_max_sec', 20,
         'product_family', 'fusion_extension',
         'provider_family', 'veed_fabric',
         'billing_entity', 'parent_longform_job',
         'resolution', 'premium',
         'seed', 'veed_premium_10_20_30_v1'
       )
from pricing_skus
where code = 'LONGFORM_TALK_ECONOMY_20S'
on conflict (code) do update
set
  name = excluded.name,
  unit = excluded.unit,
  category = excluded.category,
  default_unit_credits = excluded.default_unit_credits,
  status = excluded.status,
  metadata_json = excluded.metadata_json;

insert into pricing_skus (
  code,
  name,
  unit,
  category,
  default_unit_credits,
  status,
  metadata_json
)
select
  'LONGFORM_TALK_PREMIUM_30S' as code,
  'Talking Video Premium - 21 to 30 seconds' as name,
  unit,
  category,
  default_unit_credits,
  status,
  coalesce(metadata_json, '{}'::jsonb)
    || jsonb_build_object(
         'mode', 'talking_video',
         'quality_tier', 'premium',
         'bucket_kind', 'duration_band',
         'bucket_max_sec', 30,
         'product_family', 'fusion_extension',
         'provider_family', 'veed_fabric',
         'billing_entity', 'parent_longform_job',
         'resolution', 'premium',
         'seed', 'veed_premium_10_20_30_v1'
       )
from pricing_skus
where code = 'LONGFORM_TALK_ECONOMY_30S'
on conflict (code) do update
set
  name = excluded.name,
  unit = excluded.unit,
  category = excluded.category,
  default_unit_credits = excluded.default_unit_credits,
  status = excluded.status,
  metadata_json = excluded.metadata_json;

-- ---------------------------------------------------------
-- 3) reset premium variant-line mappings
-- ---------------------------------------------------------
delete from pricing_variant_lines
where variant_code in (
  'TALKING_VIDEO_PREMIUM_10S',
  'TALKING_VIDEO_PREMIUM_20S',
  'TALKING_VIDEO_PREMIUM_30S'
);

-- ---------------------------------------------------------
-- 4) clone variant-line rows from economy
-- preserves qty_mode / qty_param
-- ---------------------------------------------------------
insert into pricing_variant_lines (
  variant_code,
  sku_code,
  qty_mode,
  qty_param,
  metadata_json
)
select
  'TALKING_VIDEO_PREMIUM_10S' as variant_code,
  'LONGFORM_TALK_PREMIUM_10S' as sku_code,
  qty_mode,
  qty_param,
  coalesce(metadata_json, '{}'::jsonb)
    || jsonb_build_object(
         'bucket_max_sec', 10,
         'quality_tier', 'premium',
         'provider_family', 'veed_fabric',
         'seed', 'veed_premium_10_20_30_v1'
       )
from pricing_variant_lines
where variant_code = 'TALKING_VIDEO_ECONOMY_10S';

insert into pricing_variant_lines (
  variant_code,
  sku_code,
  qty_mode,
  qty_param,
  metadata_json
)
select
  'TALKING_VIDEO_PREMIUM_20S' as variant_code,
  'LONGFORM_TALK_PREMIUM_20S' as sku_code,
  qty_mode,
  qty_param,
  coalesce(metadata_json, '{}'::jsonb)
    || jsonb_build_object(
         'bucket_max_sec', 20,
         'quality_tier', 'premium',
         'provider_family', 'veed_fabric',
         'seed', 'veed_premium_10_20_30_v1'
       )
from pricing_variant_lines
where variant_code = 'TALKING_VIDEO_ECONOMY_20S';

insert into pricing_variant_lines (
  variant_code,
  sku_code,
  qty_mode,
  qty_param,
  metadata_json
)
select
  'TALKING_VIDEO_PREMIUM_30S' as variant_code,
  'LONGFORM_TALK_PREMIUM_30S' as sku_code,
  qty_mode,
  qty_param,
  coalesce(metadata_json, '{}'::jsonb)
    || jsonb_build_object(
         'bucket_max_sec', 30,
         'quality_tier', 'premium',
         'provider_family', 'veed_fabric',
         'seed', 'veed_premium_10_20_30_v1'
       )
from pricing_variant_lines
where variant_code = 'TALKING_VIDEO_ECONOMY_30S';

-- ---------------------------------------------------------
-- 5) reset premium price rows
-- Premium price = Economy price * 1.15
-- money rounded to 2 decimals
-- credits rounded up
-- ---------------------------------------------------------
delete from pricing_sku_prices
where sku_code in (
  'LONGFORM_TALK_PREMIUM_10S',
  'LONGFORM_TALK_PREMIUM_20S',
  'LONGFORM_TALK_PREMIUM_30S'
);

with sku_map as (
  select *
  from (
    values
      ('LONGFORM_TALK_ECONOMY_10S', 'LONGFORM_TALK_PREMIUM_10S'),
      ('LONGFORM_TALK_ECONOMY_20S', 'LONGFORM_TALK_PREMIUM_20S'),
      ('LONGFORM_TALK_ECONOMY_30S', 'LONGFORM_TALK_PREMIUM_30S')
  ) as t(economy_sku_code, premium_sku_code)
)
insert into pricing_sku_prices (
  pricebook_id,
  sku_code,
  unit_credits_override,
  unit_money_override,
  min_qty,
  max_qty,
  metadata_json
)
select
  p.pricebook_id,
  m.premium_sku_code as sku_code,
  case
    when p.unit_credits_override is null then null
    else greatest(1, ceil(p.unit_credits_override::numeric * 1.15))::bigint
  end as unit_credits_override,
  case
    when p.unit_money_override is null then null
    else round(p.unit_money_override * 1.15, 2)
  end as unit_money_override,
  p.min_qty,
  p.max_qty,
  coalesce(p.metadata_json, '{}'::jsonb)
    || jsonb_build_object(
         'copied_from_sku', p.sku_code,
         'pricing_multiplier', 1.15,
         'quality_tier', 'premium',
         'provider_family', 'veed_fabric',
         'seed', 'veed_premium_10_20_30_v1'
       )
from pricing_sku_prices p
join sku_map m
  on m.economy_sku_code = p.sku_code;

commit;