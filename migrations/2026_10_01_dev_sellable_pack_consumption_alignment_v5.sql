-- desifaces DEV sellable-pack / consumption economics alignment v5 — 2026-10-01
--
-- Business contract explicitly confirmed by product owner:
--   * Customer-facing credit packs remain sellable commercial packages.
--   * USD packs: 1000=$9.99, 5000=$39.99, 15000=$99.99.
--   * INR packs retain the prior launch package values: 1000=₹999,
--     5000=₹3999, 15000=₹9999.
--   * Pack prices MUST NOT be derived from subscription money-per-credit.
--   * Sustainable economics are enforced through credit CONSUMPTION rates.
--   * Multi-person/group image and video remain at least +20% over base.
--   * Provider COGS remain explicit and authoritative.
--
-- DEV only. No generation workflow, prompt, provider routing, HITL, scene
-- orchestration, subscription pricing, entitlement behavior, PROD, or Stripe
-- live objects are changed by this migration.

BEGIN;

DO $$
BEGIN
  IF to_regclass('public.pricing_credit_packs') IS NULL
     OR to_regclass('public.pricing_skus') IS NULL
     OR to_regclass('public.pricing_sku_costs') IS NULL
     OR to_regclass('public.pricing_pricebooks') IS NULL
     OR to_regclass('public.pricing_sku_prices') IS NULL
     OR to_regclass('public.pricing_plan_prices') IS NULL
     OR to_regclass('public.pricing_tiers') IS NULL THEN
    RAISE EXCEPTION 'sellable-pack v5 requires complete pricing catalog tables';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1. Restore the approved customer-facing package contract.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _approved_packs(
  code text PRIMARY KEY,
  currency text NOT NULL,
  country_code text NOT NULL,
  credits bigint NOT NULL,
  price_money numeric(18,8) NOT NULL
) ON COMMIT DROP;

INSERT INTO _approved_packs VALUES
  ('PACK_USD_1000',  'USD','',   1000,    9.99),
  ('PACK_USD_5000',  'USD','',   5000,   39.99),
  ('PACK_USD_15000', 'USD','',  15000,   99.99),
  ('PACK_INR_1000',  'INR','IN', 1000,  999.00),
  ('PACK_INR_5000',  'INR','IN', 5000, 3999.00),
  ('PACK_INR_15000', 'INR','IN',15000, 9999.00);

DO $$
DECLARE missing text;
BEGIN
  SELECT string_agg(a.code,', ' ORDER BY a.code)
  INTO missing
  FROM _approved_packs a
  LEFT JOIN public.pricing_credit_packs p
    ON p.code=a.code
  WHERE p.code IS NULL;

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'missing approved credit pack(s): %',missing;
  END IF;
END $$;

UPDATE public.pricing_credit_packs p
SET price_money=a.price_money,
    metadata_json=
      (
        COALESCE(p.metadata_json,'{}'::jsonb)
        - 'minimum_plan_money_per_credit'
        - 'topup_may_not_undercut_active_public_plan'
        - 'stripe_catalog_version'
        - 'stripe_v4_mapped_at'
      )
      || jsonb_build_object(
        'commercial_pack_contract','sellable-pack-v1',
        'pack_price_change_requires_explicit_approval',true,
        'consumption_economics_owner','pricing_skus',
        'corrective_alignment','2026-10-01-v5'
      )
FROM _approved_packs a
WHERE p.code=a.code
  AND upper(p.currency)=a.currency
  AND coalesce(p.country_code,'')=a.country_code
  AND p.credits=a.credits;

-- ---------------------------------------------------------------------------
-- 2. Determine worst-case PAID USD realization.
--    Unlike v4, this value can NEVER mutate a pack price. It only sets the
--    minimum sustainable credit consumption for generation.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _paid_usd_credit_value(
  source text NOT NULL,
  code text NOT NULL,
  money_per_credit numeric(18,10) NOT NULL CHECK(money_per_credit>0)
) ON COMMIT DROP;

INSERT INTO _paid_usd_credit_value(source,code,money_per_credit)
SELECT
  'pack',
  p.code,
  p.price_money/p.credits::numeric
FROM public.pricing_credit_packs p
WHERE p.is_active=true
  AND upper(p.currency)='USD'
  AND p.price_money>0
  AND p.credits>0

UNION ALL

SELECT
  'subscription',
  p.plan_code || ':' || p.interval_code,
  p.price_money /
    nullif(
      coalesce(
        nullif(p.metadata_json->>'included_credits_total','')::numeric,
        nullif(p.metadata_json->>'grant_credits','')::numeric,
        case
          when p.interval_code='yearly' then t.monthly_grant_credits::numeric*12
          else t.monthly_grant_credits::numeric
        end
      ),0
    )
FROM public.pricing_plan_prices p
JOIN public.pricing_tiers t ON t.code=p.tier_code
WHERE p.is_active=true
  AND p.is_public=true
  AND upper(p.currency)='USD'
  AND p.price_money>0;

DELETE FROM _paid_usd_credit_value WHERE money_per_credit IS NULL OR money_per_credit<=0;

DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM _paid_usd_credit_value;
  IF n=0 THEN
    RAISE EXCEPTION 'no paid USD credit realization source available';
  END IF;
END $$;

CREATE TEMP TABLE _credit_floor(value numeric(18,10) PRIMARY KEY) ON COMMIT DROP;
INSERT INTO _credit_floor(value)
SELECT min(money_per_credit) FROM _paid_usd_credit_value;

-- ---------------------------------------------------------------------------
-- 3. Compute provider-COGS break-even consumption by natural unit.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE _target_skus(code text PRIMARY KEY) ON COMMIT DROP;
INSERT INTO _target_skus VALUES
  ('IMG_STD_RUN'),
  ('IMG_HD_RUN'),
  ('FACE_EDIT_PREMIUM_RUN'),
  ('FACE_MULTI_PERSON'),
  ('FACE_MULTI_PERSON_I2I'),
  ('AUDIO_TTS_1K_CHARS'),
  ('AUDIO_MULTI_PERSON'),
  ('FUSION_TALK_MIN'),
  ('FUSION_MULTI_PERSON');

CREATE TEMP TABLE _active_costs(
  sku_code text PRIMARY KEY,
  unit_cogs_usd numeric(18,8) NOT NULL CHECK(unit_cogs_usd>0)
) ON COMMIT DROP;

INSERT INTO _active_costs(sku_code,unit_cogs_usd)
SELECT
  c.sku_code,
  sum(
    case lower(c.cost_model)
      when 'variable' then c.variable_cost_money
      when 'amortized' then
        case when c.assumed_monthly_units>0
             then c.fixed_monthly_cost_money/c.assumed_monthly_units
             else 0 end
      when 'blended' then
        c.variable_cost_money
        + case when c.assumed_monthly_units>0
               then c.fixed_monthly_cost_money/c.assumed_monthly_units
               else 0 end
      else 0
    end
  )::numeric(18,8)
FROM public.pricing_sku_costs c
JOIN _target_skus t ON t.code=c.sku_code
WHERE c.is_active=true
  AND c.effective_from<=now()
  AND (c.effective_to IS NULL OR c.effective_to>now())
GROUP BY c.sku_code
HAVING sum(
    case lower(c.cost_model)
      when 'variable' then c.variable_cost_money
      when 'amortized' then
        case when c.assumed_monthly_units>0
             then c.fixed_monthly_cost_money/c.assumed_monthly_units
             else 0 end
      when 'blended' then
        c.variable_cost_money
        + case when c.assumed_monthly_units>0
               then c.fixed_monthly_cost_money/c.assumed_monthly_units
               else 0 end
      else 0
    end
  )>0;

DO $$
DECLARE missing text;
BEGIN
  SELECT string_agg(t.code,', ' ORDER BY t.code)
  INTO missing
  FROM _target_skus t
  LEFT JOIN _active_costs c ON c.sku_code=t.code
  WHERE c.sku_code IS NULL;

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'missing positive active COGS for consumption SKU(s): %',missing;
  END IF;
END $$;

CREATE TEMP TABLE _consumption_targets(
  sku_code text PRIMARY KEY,
  cogs_floor_credits bigint NOT NULL CHECK(cogs_floor_credits>0),
  target_credits bigint NOT NULL CHECK(target_credits>0),
  unit_cogs_usd numeric(18,8) NOT NULL,
  min_paid_usd_per_credit numeric(18,10) NOT NULL
) ON COMMIT DROP;

INSERT INTO _consumption_targets(
  sku_code,cogs_floor_credits,target_credits,unit_cogs_usd,min_paid_usd_per_credit
)
SELECT
  c.sku_code,
  ceil(c.unit_cogs_usd/f.value)::bigint,
  ceil(c.unit_cogs_usd/f.value)::bigint,
  c.unit_cogs_usd,
  f.value
FROM _active_costs c
CROSS JOIN _credit_floor f;

-- Preserve the explicitly approved minimum +20% engineering/customer premium
-- for multi-person image/video while never pricing below provider COGS.
UPDATE _consumption_targets m
SET target_credits=greatest(
      m.cogs_floor_credits,
      ceil(b.target_credits*1.20)::bigint
    )
FROM _consumption_targets b
WHERE (m.sku_code,b.sku_code) IN (
  ('FACE_MULTI_PERSON','IMG_STD_RUN'),
  ('FACE_MULTI_PERSON_I2I','FACE_EDIT_PREMIUM_RUN'),
  ('FUSION_MULTI_PERSON','FUSION_TALK_MIN')
);

-- ---------------------------------------------------------------------------
-- 4. Apply consumption targets. Pack prices remain untouched from here onward.
-- ---------------------------------------------------------------------------
UPDATE public.pricing_skus s
SET default_unit_credits=t.target_credits,
    metadata_json=COALESCE(s.metadata_json,'{}'::jsonb) || jsonb_build_object(
      'consumption_economics','sellable-pack-v1',
      'consumption_floor_basis','active_provider_cogs_vs_min_paid_usd_per_credit',
      'min_paid_usd_per_credit',t.min_paid_usd_per_credit,
      'unit_cogs_usd',t.unit_cogs_usd,
      'cogs_floor_credits',t.cogs_floor_credits,
      'target_credits',t.target_credits,
      'pack_prices_mutated',false,
      'commercial_alignment','2026-10-01-v5'
    )
FROM _consumption_targets t
WHERE s.code=t.sku_code;

-- Every active pricebook resolves to at least the target effective credits.
INSERT INTO public.pricing_sku_prices(
  pricebook_id,sku_code,unit_credits_override,unit_money_override,
  min_qty,max_qty,metadata_json
)
SELECT
  pb.id,
  t.sku_code,
  ceil(t.target_credits/coalesce(pb.multiplier,1))::bigint,
  NULL,
  CASE
    WHEN t.sku_code IN (
      'FACE_MULTI_PERSON','FACE_MULTI_PERSON_I2I',
      'AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON'
    ) THEN 1
    ELSE NULL
  END,
  NULL,
  jsonb_build_object(
    'consumption_economics','sellable-pack-v1',
    'target_effective_credits',t.target_credits,
    'money_derived_from_paid_credit_realization',true,
    'pack_prices_mutated',false,
    'commercial_alignment','2026-10-01-v5'
  )
FROM public.pricing_pricebooks pb
CROSS JOIN _consumption_targets t
WHERE pb.is_active=true
  AND pb.effective_from<=now()
  AND (pb.effective_to IS NULL OR pb.effective_to>now())
  AND coalesce(pb.multiplier,1)>0
ON CONFLICT(pricebook_id,sku_code) DO UPDATE SET
  unit_credits_override=EXCLUDED.unit_credits_override,
  unit_money_override=NULL,
  min_qty=CASE
    WHEN EXCLUDED.sku_code IN (
      'FACE_MULTI_PERSON','FACE_MULTI_PERSON_I2I',
      'AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON'
    ) THEN 1
    ELSE public.pricing_sku_prices.min_qty
  END,
  max_qty=CASE
    WHEN EXCLUDED.sku_code IN (
      'FACE_MULTI_PERSON','FACE_MULTI_PERSON_I2I',
      'AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON'
    ) THEN NULL
    ELSE public.pricing_sku_prices.max_qty
  END,
  metadata_json=COALESCE(public.pricing_sku_prices.metadata_json,'{}'::jsonb)
                || EXCLUDED.metadata_json;

-- ---------------------------------------------------------------------------
-- 5. Fail-closed certification.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  bad_packs integer;
  negative_margin integer;
  premium_failures integer;
  pricebook_failures integer;
BEGIN
  SELECT count(*) INTO bad_packs
  FROM _approved_packs a
  LEFT JOIN public.pricing_credit_packs p
    ON p.code=a.code
   AND upper(p.currency)=a.currency
   AND coalesce(p.country_code,'')=a.country_code
  WHERE p.code IS NULL
     OR p.credits<>a.credits
     OR p.price_money<>a.price_money;

  IF bad_packs<>0 THEN
    RAISE EXCEPTION 'approved sellable pack contract failures=%',bad_packs;
  END IF;

  SELECT count(*) INTO negative_margin
  FROM _consumption_targets t
  WHERE t.target_credits*t.min_paid_usd_per_credit < t.unit_cogs_usd;

  IF negative_margin<>0 THEN
    RAISE EXCEPTION 'provider-COGS consumption floor failures=%',negative_margin;
  END IF;

  WITH pairs(multi_sku,base_sku) AS (
    VALUES
      ('FACE_MULTI_PERSON','IMG_STD_RUN'),
      ('FACE_MULTI_PERSON_I2I','FACE_EDIT_PREMIUM_RUN'),
      ('FUSION_MULTI_PERSON','FUSION_TALK_MIN')
  )
  SELECT count(*) INTO premium_failures
  FROM pairs p
  JOIN _consumption_targets m ON m.sku_code=p.multi_sku
  JOIN _consumption_targets b ON b.sku_code=p.base_sku
  WHERE m.target_credits<ceil(b.target_credits*1.20)::bigint;

  IF premium_failures<>0 THEN
    RAISE EXCEPTION '+20%% multi-person image/video consumption failures=%',premium_failures;
  END IF;

  SELECT count(*) INTO pricebook_failures
  FROM public.pricing_pricebooks pb
  CROSS JOIN _consumption_targets t
  LEFT JOIN public.pricing_sku_prices sp
    ON sp.pricebook_id=pb.id AND sp.sku_code=t.sku_code
  WHERE pb.is_active=true
    AND pb.effective_from<=now()
    AND (pb.effective_to IS NULL OR pb.effective_to>now())
    AND (
      sp.sku_code IS NULL
      OR ceil(
          coalesce(sp.unit_credits_override,0)::numeric
          * coalesce(pb.multiplier,1)
        )::bigint < t.target_credits
    );

  IF pricebook_failures<>0 THEN
    RAISE EXCEPTION 'active pricebook consumption target failures=%',pricebook_failures;
  END IF;
END $$;

COMMIT;
