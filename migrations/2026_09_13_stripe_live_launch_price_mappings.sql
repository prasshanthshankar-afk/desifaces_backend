-- desifaces Stripe LIVE commercial-launch mapping.
-- Stripe account: acct_1TIIxvPA22bn06oY (US)
-- Catalog provisioned and validated in LIVE mode on 2026-09-13.
--
-- Scope is intentionally narrow:
--   * 8 self-serve recurring plan Stripe price IDs
--   * 6 credit-pack Stripe price IDs
--
-- This migration MUST NOT change customer prices, credits, plan visibility,
-- entitlements, or any non-Stripe billing value.

BEGIN;

DO $$
DECLARE
  v_count integer;
BEGIN
  IF to_regclass('public.pricing_plan_prices') IS NULL THEN
    RAISE EXCEPTION 'pricing_plan_prices table is required';
  END IF;
  IF to_regclass('public.pricing_credit_packs') IS NULL THEN
    RAISE EXCEPTION 'pricing_credit_packs table is required';
  END IF;

  -- Exact launch recurring-plan rows and customer amounts.
  SELECT count(*) INTO v_count
  FROM public.pricing_plan_prices
  WHERE (plan_code, interval_code, upper(currency), coalesce(country_code,''), price_money) IN (
    ('pro_monthly_v1',      'monthly', 'USD', '',   28.99::numeric),
    ('pro_monthly_v1',      'monthly', 'INR', 'IN', 2999.00::numeric),
    ('pro_yearly_v1',       'yearly',  'USD', '',   289.99::numeric),
    ('pro_yearly_v1',       'yearly',  'INR', 'IN', 29900.00::numeric),
    ('business_monthly_v1', 'monthly', 'USD', '',   99.99::numeric),
    ('business_monthly_v1', 'monthly', 'INR', 'IN', 9900.00::numeric),
    ('business_yearly_v1',  'yearly',  'USD', '',   989.99::numeric),
    ('business_yearly_v1',  'yearly',  'INR', 'IN', 83900.00::numeric)
  )
    AND is_active = true
    AND is_public = true
    AND self_serve = true
    AND contact_sales = false;
  IF v_count <> 8 THEN
    RAISE EXCEPTION 'Expected 8 exact self-serve launch plan rows; found %', v_count;
  END IF;

  -- Exact launch credit-pack rows and customer amounts/credits.
  SELECT count(*) INTO v_count
  FROM public.pricing_credit_packs
  WHERE (code, upper(currency), coalesce(country_code,''), credits, price_money) IN (
    ('PACK_USD_1000',  'USD', '',   1000,  9.99::numeric),
    ('PACK_USD_5000',  'USD', '',   5000,  39.99::numeric),
    ('PACK_USD_15000', 'USD', '',   15000, 99.99::numeric),
    ('PACK_INR_1000',  'INR', 'IN', 1000,  999.00::numeric),
    ('PACK_INR_5000',  'INR', 'IN', 5000,  3999.00::numeric),
    ('PACK_INR_15000', 'INR', 'IN', 15000, 9999.00::numeric)
  )
    AND is_active = true;
  IF v_count <> 6 THEN
    RAISE EXCEPTION 'Expected 6 exact launch credit-pack rows; found %', v_count;
  END IF;
END $$;

-- Preserve a transaction-local fingerprint of all customer-facing values that
-- this migration is forbidden to change.
CREATE TEMP TABLE _df_stripe_live_value_before AS
SELECT
  (
    SELECT md5(string_agg(
      concat_ws('|', plan_code, interval_code, upper(currency), coalesce(country_code,''),
                price_money::text, is_active::text, is_public::text,
                self_serve::text, contact_sales::text),
      '||' ORDER BY plan_code, interval_code, upper(currency), coalesce(country_code,'')))
    FROM public.pricing_plan_prices
  ) AS plan_hash,
  (
    SELECT md5(string_agg(
      concat_ws('|', code, credits::text, upper(currency), coalesce(country_code,''),
                price_money::text, is_active::text),
      '||' ORDER BY code))
    FROM public.pricing_credit_packs
  ) AS pack_hash;

-- ---------------------------------------------------------------------------
-- Recurring self-serve plans: Stripe LIVE price IDs
-- ---------------------------------------------------------------------------
UPDATE public.pricing_plan_prices
SET stripe_price_id = CASE
      WHEN plan_code='pro_monthly_v1'      AND interval_code='monthly' AND upper(currency)='USD' AND coalesce(country_code,'')=''   THEN 'price_1UFIZSPA22bn06oYoBHGn0N5'
      WHEN plan_code='pro_monthly_v1'      AND interval_code='monthly' AND upper(currency)='INR' AND coalesce(country_code,'')='IN' THEN 'price_1UFIZTPA22bn06oYHbJwX9wS'
      WHEN plan_code='pro_yearly_v1'       AND interval_code='yearly'  AND upper(currency)='USD' AND coalesce(country_code,'')=''   THEN 'price_1UFIZUPA22bn06oYkTlNWpBz'
      WHEN plan_code='pro_yearly_v1'       AND interval_code='yearly'  AND upper(currency)='INR' AND coalesce(country_code,'')='IN' THEN 'price_1UFIZUPA22bn06oYe0qd8Iw4'
      WHEN plan_code='business_monthly_v1' AND interval_code='monthly' AND upper(currency)='USD' AND coalesce(country_code,'')=''   THEN 'price_1UFIZVPA22bn06oYD4huKUam'
      WHEN plan_code='business_monthly_v1' AND interval_code='monthly' AND upper(currency)='INR' AND coalesce(country_code,'')='IN' THEN 'price_1UFIZVPA22bn06oYgpgLBzj7'
      WHEN plan_code='business_yearly_v1'  AND interval_code='yearly'  AND upper(currency)='USD' AND coalesce(country_code,'')=''   THEN 'price_1UFIZWPA22bn06oYkIF8XHnf'
      WHEN plan_code='business_yearly_v1'  AND interval_code='yearly'  AND upper(currency)='INR' AND coalesce(country_code,'')='IN' THEN 'price_1UFIZWPA22bn06oYUqdp0UQ8'
      ELSE stripe_price_id
    END,
    metadata_json = coalesce(metadata_json, '{}'::jsonb) || jsonb_build_object(
      'stripe_env', 'live',
      'stripe_account_id', 'acct_1TIIxvPA22bn06oY',
      'stripe_price_id', CASE
        WHEN plan_code='pro_monthly_v1'      AND interval_code='monthly' AND upper(currency)='USD' AND coalesce(country_code,'')=''   THEN 'price_1UFIZSPA22bn06oYoBHGn0N5'
        WHEN plan_code='pro_monthly_v1'      AND interval_code='monthly' AND upper(currency)='INR' AND coalesce(country_code,'')='IN' THEN 'price_1UFIZTPA22bn06oYHbJwX9wS'
        WHEN plan_code='pro_yearly_v1'       AND interval_code='yearly'  AND upper(currency)='USD' AND coalesce(country_code,'')=''   THEN 'price_1UFIZUPA22bn06oYkTlNWpBz'
        WHEN plan_code='pro_yearly_v1'       AND interval_code='yearly'  AND upper(currency)='INR' AND coalesce(country_code,'')='IN' THEN 'price_1UFIZUPA22bn06oYe0qd8Iw4'
        WHEN plan_code='business_monthly_v1' AND interval_code='monthly' AND upper(currency)='USD' AND coalesce(country_code,'')=''   THEN 'price_1UFIZVPA22bn06oYD4huKUam'
        WHEN plan_code='business_monthly_v1' AND interval_code='monthly' AND upper(currency)='INR' AND coalesce(country_code,'')='IN' THEN 'price_1UFIZVPA22bn06oYgpgLBzj7'
        WHEN plan_code='business_yearly_v1'  AND interval_code='yearly'  AND upper(currency)='USD' AND coalesce(country_code,'')=''   THEN 'price_1UFIZWPA22bn06oYkIF8XHnf'
        WHEN plan_code='business_yearly_v1'  AND interval_code='yearly'  AND upper(currency)='INR' AND coalesce(country_code,'')='IN' THEN 'price_1UFIZWPA22bn06oYUqdp0UQ8'
      END,
      'stripe_live_mapped_at', '2026-09-13'
    ),
    updated_at = now()
WHERE tier_code IN ('pro','business')
  AND is_active=true
  AND is_public=true
  AND self_serve=true
  AND upper(currency) IN ('USD','INR');

-- ---------------------------------------------------------------------------
-- Credit packs: Stripe LIVE price IDs in canonical metadata
-- ---------------------------------------------------------------------------
UPDATE public.pricing_credit_packs
SET metadata_json = coalesce(metadata_json, '{}'::jsonb) || jsonb_build_object(
      'stripe_env', 'live',
      'stripe_account_id', 'acct_1TIIxvPA22bn06oY',
      'stripe_price_id', CASE code
        WHEN 'PACK_USD_1000'  THEN 'price_1UFIZXPA22bn06oYKrt5xey1'
        WHEN 'PACK_USD_5000'  THEN 'price_1UFIZXPA22bn06oYoubhQiFt'
        WHEN 'PACK_USD_15000' THEN 'price_1UFIZYPA22bn06oYPfKPtxkw'
        WHEN 'PACK_INR_1000'  THEN 'price_1UFIZZPA22bn06oYdhPjac64'
        WHEN 'PACK_INR_5000'  THEN 'price_1UFIZZPA22bn06oYMabHilgS'
        WHEN 'PACK_INR_15000' THEN 'price_1UFIZaPA22bn06oYOHOiv1fW'
      END,
      'stripe_live_mapped_at', '2026-09-13'
    )
WHERE code IN (
  'PACK_USD_1000','PACK_USD_5000','PACK_USD_15000',
  'PACK_INR_1000','PACK_INR_5000','PACK_INR_15000'
)
  AND is_active=true;

DO $$
DECLARE
  v_count integer;
  v_plan_hash text;
  v_pack_hash text;
  v_before_plan_hash text;
  v_before_pack_hash text;
BEGIN
  -- Exact live plan mappings.
  SELECT count(*) INTO v_count
  FROM public.pricing_plan_prices
  WHERE (plan_code, interval_code, upper(currency), coalesce(country_code,''), stripe_price_id) IN (
    ('pro_monthly_v1',      'monthly', 'USD', '',   'price_1UFIZSPA22bn06oYoBHGn0N5'),
    ('pro_monthly_v1',      'monthly', 'INR', 'IN', 'price_1UFIZTPA22bn06oYHbJwX9wS'),
    ('pro_yearly_v1',       'yearly',  'USD', '',   'price_1UFIZUPA22bn06oYkTlNWpBz'),
    ('pro_yearly_v1',       'yearly',  'INR', 'IN', 'price_1UFIZUPA22bn06oYe0qd8Iw4'),
    ('business_monthly_v1', 'monthly', 'USD', '',   'price_1UFIZVPA22bn06oYD4huKUam'),
    ('business_monthly_v1', 'monthly', 'INR', 'IN', 'price_1UFIZVPA22bn06oYgpgLBzj7'),
    ('business_yearly_v1',  'yearly',  'USD', '',   'price_1UFIZWPA22bn06oYkIF8XHnf'),
    ('business_yearly_v1',  'yearly',  'INR', 'IN', 'price_1UFIZWPA22bn06oYUqdp0UQ8')
  )
    AND metadata_json->>'stripe_env'='live'
    AND metadata_json->>'stripe_account_id'='acct_1TIIxvPA22bn06oY';
  IF v_count <> 8 THEN
    RAISE EXCEPTION 'Stripe LIVE plan mapping verification failed; found %/8', v_count;
  END IF;

  -- Exact live pack mappings.
  SELECT count(*) INTO v_count
  FROM public.pricing_credit_packs
  WHERE (code, metadata_json->>'stripe_price_id') IN (
    ('PACK_USD_1000',  'price_1UFIZXPA22bn06oYKrt5xey1'),
    ('PACK_USD_5000',  'price_1UFIZXPA22bn06oYoubhQiFt'),
    ('PACK_USD_15000', 'price_1UFIZYPA22bn06oYPfKPtxkw'),
    ('PACK_INR_1000',  'price_1UFIZZPA22bn06oYdhPjac64'),
    ('PACK_INR_5000',  'price_1UFIZZPA22bn06oYMabHilgS'),
    ('PACK_INR_15000', 'price_1UFIZaPA22bn06oYOHOiv1fW')
  )
    AND metadata_json->>'stripe_env'='live'
    AND metadata_json->>'stripe_account_id'='acct_1TIIxvPA22bn06oY';
  IF v_count <> 6 THEN
    RAISE EXCEPTION 'Stripe LIVE pack mapping verification failed; found %/6', v_count;
  END IF;

  -- Prove this migration changed no customer-facing economic values.
  SELECT plan_hash, pack_hash INTO v_before_plan_hash, v_before_pack_hash
  FROM _df_stripe_live_value_before;

  SELECT md5(string_agg(
      concat_ws('|', plan_code, interval_code, upper(currency), coalesce(country_code,''),
                price_money::text, is_active::text, is_public::text,
                self_serve::text, contact_sales::text),
      '||' ORDER BY plan_code, interval_code, upper(currency), coalesce(country_code,'')))
    INTO v_plan_hash
  FROM public.pricing_plan_prices;

  SELECT md5(string_agg(
      concat_ws('|', code, credits::text, upper(currency), coalesce(country_code,''),
                price_money::text, is_active::text),
      '||' ORDER BY code))
    INTO v_pack_hash
  FROM public.pricing_credit_packs;

  IF v_plan_hash IS DISTINCT FROM v_before_plan_hash THEN
    RAISE EXCEPTION 'Customer plan pricing/visibility changed unexpectedly';
  END IF;
  IF v_pack_hash IS DISTINCT FROM v_before_pack_hash THEN
    RAISE EXCEPTION 'Customer credit-pack values changed unexpectedly';
  END IF;
END $$;

COMMIT;
