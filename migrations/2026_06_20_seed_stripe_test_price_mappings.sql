-- DesiFaces Stripe TEST price mapping seed
-- Generated at 2026-06-20T16:18:57.267506+00:00
-- This file contains TEST MODE Stripe price IDs only.
begin;

-- pro_monthly_v1 => price_1TkRbe2eFT0FzYomY4KOdpD5 product=prod_UjvSNvt2rZunH8 reused=False
update public.pricing_plan_prices
set stripe_price_id = 'price_1TkRbe2eFT0FzYomY4KOdpD5',
    metadata_json = jsonb_set(
      jsonb_set(
        coalesce(metadata_json, '{}'::jsonb),
        '{stripe_price_id}',
        to_jsonb('price_1TkRbe2eFT0FzYomY4KOdpD5'::text),
        true
      ),
      '{stripe_product_id}',
      to_jsonb('prod_UjvSNvt2rZunH8'::text),
      true
    ),
    updated_at = now()
where plan_code = 'pro_monthly_v1'
  and interval_code = 'monthly'
  and upper(currency) = 'USD';

-- pro_yearly_v1 => price_1TkRbf2eFT0FzYomjNvaJ1EN product=prod_UjvSZkaglUCc51 reused=False
update public.pricing_plan_prices
set stripe_price_id = 'price_1TkRbf2eFT0FzYomjNvaJ1EN',
    metadata_json = jsonb_set(
      jsonb_set(
        coalesce(metadata_json, '{}'::jsonb),
        '{stripe_price_id}',
        to_jsonb('price_1TkRbf2eFT0FzYomjNvaJ1EN'::text),
        true
      ),
      '{stripe_product_id}',
      to_jsonb('prod_UjvSZkaglUCc51'::text),
      true
    ),
    updated_at = now()
where plan_code = 'pro_yearly_v1'
  and interval_code = 'yearly'
  and upper(currency) = 'USD';

-- business_monthly_v1 => price_1TkRbf2eFT0FzYomAwQiIHsw product=prod_UjvS9ZNGTfoL40 reused=False
update public.pricing_plan_prices
set stripe_price_id = 'price_1TkRbf2eFT0FzYomAwQiIHsw',
    metadata_json = jsonb_set(
      jsonb_set(
        coalesce(metadata_json, '{}'::jsonb),
        '{stripe_price_id}',
        to_jsonb('price_1TkRbf2eFT0FzYomAwQiIHsw'::text),
        true
      ),
      '{stripe_product_id}',
      to_jsonb('prod_UjvS9ZNGTfoL40'::text),
      true
    ),
    updated_at = now()
where plan_code = 'business_monthly_v1'
  and interval_code = 'monthly'
  and upper(currency) = 'USD';

-- business_yearly_v1 => price_1TkRbg2eFT0FzYomCTsQc7h6 product=prod_UjvSspPfVg0g6D reused=False
update public.pricing_plan_prices
set stripe_price_id = 'price_1TkRbg2eFT0FzYomCTsQc7h6',
    metadata_json = jsonb_set(
      jsonb_set(
        coalesce(metadata_json, '{}'::jsonb),
        '{stripe_price_id}',
        to_jsonb('price_1TkRbg2eFT0FzYomCTsQc7h6'::text),
        true
      ),
      '{stripe_product_id}',
      to_jsonb('prod_UjvSspPfVg0g6D'::text),
      true
    ),
    updated_at = now()
where plan_code = 'business_yearly_v1'
  and interval_code = 'yearly'
  and upper(currency) = 'USD';

-- PACK_USD_1000 => price_1TdwQk2eFT0FzYomldUauNXe product=prod_UdCqRxlTBPiPHa reused=True
update public.pricing_credit_packs
set metadata_json = jsonb_set(
      jsonb_set(
        coalesce(metadata_json, '{}'::jsonb),
        '{stripe_price_id}',
        to_jsonb('price_1TdwQk2eFT0FzYomldUauNXe'::text),
        true
      ),
      '{stripe_product_id}',
      to_jsonb('prod_UdCqRxlTBPiPHa'::text),
      true
    )
where code = 'PACK_USD_1000'
  and upper(currency) = 'USD';

-- PACK_USD_5000 => price_1TdwQk2eFT0FzYomerXDZZ8X product=prod_UdCqz9xy7uv6YX reused=True
update public.pricing_credit_packs
set metadata_json = jsonb_set(
      jsonb_set(
        coalesce(metadata_json, '{}'::jsonb),
        '{stripe_price_id}',
        to_jsonb('price_1TdwQk2eFT0FzYomerXDZZ8X'::text),
        true
      ),
      '{stripe_product_id}',
      to_jsonb('prod_UdCqz9xy7uv6YX'::text),
      true
    )
where code = 'PACK_USD_5000'
  and upper(currency) = 'USD';

-- PACK_USD_15000 => price_1TdwQk2eFT0FzYomVSzcDNP2 product=prod_UdCqCv92OOaBDo reused=True
update public.pricing_credit_packs
set metadata_json = jsonb_set(
      jsonb_set(
        coalesce(metadata_json, '{}'::jsonb),
        '{stripe_price_id}',
        to_jsonb('price_1TdwQk2eFT0FzYomVSzcDNP2'::text),
        true
      ),
      '{stripe_product_id}',
      to_jsonb('prod_UdCqCv92OOaBDo'::text),
      true
    )
where code = 'PACK_USD_15000'
  and upper(currency) = 'USD';

do $$
declare
  missing_count integer;
begin
  select count(*) into missing_count
  from public.pricing_plan_prices
  where plan_code in ('pro_monthly_v1','pro_yearly_v1','business_monthly_v1','business_yearly_v1')
    and upper(currency) = 'USD'
    and coalesce(stripe_price_id, '') = '';

  if missing_count > 0 then
    raise exception 'Missing Stripe plan price mappings after seed: %', missing_count;
  end if;

  select count(*) into missing_count
  from public.pricing_credit_packs
  where code in ('PACK_USD_1000','PACK_USD_5000','PACK_USD_15000')
    and upper(currency) = 'USD'
    and coalesce(metadata_json->>'stripe_price_id', '') = '';

  if missing_count > 0 then
    raise exception 'Missing Stripe top-up price mappings after seed: %', missing_count;
  end if;
end $$;

commit;
