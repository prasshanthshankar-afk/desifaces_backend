-- Validate DesiFaces Stripe provider configuration.
-- Expected result before launch/regression:
--   missing_count = 0 for both checks.
--
-- Note:
--   This validation checks USD Stripe mappings only.
--   INR Stripe mappings are intentionally not required unless INR Stripe checkout is enabled.

select
  'pricing_plan_prices_missing_stripe_price_id' as check_name,
  count(*) as missing_count
from public.pricing_plan_prices
where plan_code in (
  'pro_monthly_v1',
  'pro_yearly_v1',
  'business_monthly_v1',
  'business_yearly_v1'
)
  and upper(currency) = 'USD'
  and coalesce(stripe_price_id, '') = '';

select
  'pricing_credit_packs_missing_stripe_price_id' as check_name,
  count(*) as missing_count
from public.pricing_credit_packs
where code in (
  'PACK_USD_1000',
  'PACK_USD_5000',
  'PACK_USD_15000'
)
  and upper(currency) = 'USD'
  and coalesce(metadata_json->>'stripe_price_id', '') = '';
