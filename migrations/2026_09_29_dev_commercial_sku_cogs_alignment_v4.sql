-- desifaces DEV commercial alignment v4 — 2026-09-29
-- Narrow launch scope only: normal Face/Audio/Video plus multi-person/group equivalents.
-- No generation workflow, prompt, provider routing, HITL, scene orchestration,
-- subscription plan, or entitlement behavior is changed.
--
-- Contracts:
--   * Face runtime is OpenAI GPT Image 2 high.
--   * Multi/group image and video rates are >= +20% over the corresponding base.
--   * Participant count is metadata only; natural workload is never multiplied.
--   * Audio remains natural 1K-character metering.
--   * V3 group/multi video charges one parent; child renders remain customer-zero-charge.
--   * Active top-up packs cannot undercut the cheapest active public subscription
--     realized credit rate in the same currency.
--   * COGS are explicit, positive, provider-aligned, and stale Fal/HeyGen placeholders
--     are retired only for the SKUs owned by this migration.

BEGIN;

DO $$
BEGIN
  IF to_regclass('public.pricing_skus') IS NULL
     OR to_regclass('public.pricing_variants') IS NULL
     OR to_regclass('public.pricing_variant_lines') IS NULL
     OR to_regclass('public.pricing_pricebooks') IS NULL
     OR to_regclass('public.pricing_sku_prices') IS NULL
     OR to_regclass('public.pricing_sku_costs') IS NULL
     OR to_regclass('public.pricing_credit_packs') IS NULL
     OR to_regclass('public.pricing_plan_prices') IS NULL
     OR to_regclass('public.pricing_tiers') IS NULL THEN
    RAISE EXCEPTION 'commercial alignment v4 requires complete pricing catalog tables';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.pricing_pricebooks
    WHERE is_active=true
      AND effective_from<=now()
      AND (effective_to IS NULL OR effective_to>now())
      AND COALESCE(multiplier,1)<=0
  ) THEN
    RAISE EXCEPTION 'active pricebook multiplier must be > 0';
  END IF;
END $$;

CREATE TEMP TABLE _plan_credit_floor (
  currency text PRIMARY KEY,
  money_per_credit numeric(18,8) NOT NULL CHECK (money_per_credit > 0)
) ON COMMIT DROP;

INSERT INTO _plan_credit_floor(currency,money_per_credit)
SELECT currency,min(value)
FROM (
  SELECT
    upper(p.currency) currency,
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
      ) value
  FROM public.pricing_plan_prices p
  JOIN public.pricing_tiers t ON t.code=p.tier_code
  WHERE p.is_active=true
    AND p.is_public=true
    AND p.price_money>0
    AND upper(p.currency) IN ('USD','INR')
) x
WHERE value>0
GROUP BY currency;

DO $$
DECLARE missing text;
BEGIN
  SELECT string_agg(x.currency,', ' ORDER BY x.currency)
  INTO missing
  FROM (VALUES ('USD'),('INR')) x(currency)
  LEFT JOIN _plan_credit_floor f ON f.currency=x.currency
  WHERE f.currency IS NULL;

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'missing active public subscription credit floor for: %',missing;
  END IF;
END $$;

-- Top-ups must never make a credit cheaper than the cheapest active public plan.
UPDATE public.pricing_credit_packs p
SET price_money=GREATEST(
      p.price_money,
      CEIL((p.credits::numeric*f.money_per_credit)*100)/100
    ),
    metadata_json=COALESCE(p.metadata_json,'{}'::jsonb) || jsonb_build_object(
      'commercial_alignment','2026-09-29-v4',
      'minimum_plan_money_per_credit',f.money_per_credit,
      'topup_may_not_undercut_active_public_plan',true,
      'updated_at',now()::text
    )
FROM _plan_credit_floor f
WHERE p.is_active=true
  AND upper(p.currency)=f.currency
  AND p.credits>0
  AND p.price_money/p.credits::numeric < f.money_per_credit;

CREATE TEMP TABLE _commercial_prices (
  sku_code text PRIMARY KEY,
  target_credits bigint NOT NULL CHECK (target_credits > 0)
) ON COMMIT DROP;

INSERT INTO _commercial_prices(sku_code,target_credits) VALUES
  ('IMG_STD_RUN',6),
  ('IMG_HD_RUN',6),
  ('FACE_EDIT_PREMIUM_RUN',12),
  ('FACE_MULTI_PERSON',8),
  ('FACE_MULTI_PERSON_I2I',15),
  ('AUDIO_MULTI_PERSON',4),
  ('FUSION_TALK_MIN',275),
  ('FUSION_MULTI_PERSON',330);

DO $$
DECLARE missing text;
BEGIN
  SELECT string_agg(x.code,', ' ORDER BY x.code)
  INTO missing
  FROM (VALUES
    ('IMG_STD_RUN'),
    ('IMG_HD_RUN'),
    ('FACE_EDIT_PREMIUM_RUN'),
    ('AUDIO_TTS_1K_CHARS'),
    ('AUDIO_MULTI_PERSON'),
    ('FUSION_TALK_MIN'),
    ('FUSION_MULTI_PERSON')
  ) x(code)
  LEFT JOIN public.pricing_skus s ON s.code=x.code
  WHERE s.code IS NULL;

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'commercial alignment v4 missing baseline SKU(s): %',missing;
  END IF;
END $$;

-- Face runtime truth: both T2I and I2I execute on OpenAI.
UPDATE public.pricing_skus
SET provider_hint='openai',
    default_unit_credits=CASE code
      WHEN 'IMG_STD_RUN' THEN 6
      WHEN 'IMG_HD_RUN' THEN 6
      WHEN 'FACE_EDIT_PREMIUM_RUN' THEN 12
      ELSE default_unit_credits
    END,
    metadata_json=COALESCE(metadata_json,'{}'::jsonb) || jsonb_build_object(
      'provider','openai',
      'provider_model','gpt-image-2',
      'quality','high',
      'commercial_alignment','2026-09-29-v4',
      'runtime_truth','svc-face forces provider=openai'
    )
WHERE code IN ('IMG_STD_RUN','IMG_HD_RUN','FACE_EDIT_PREMIUM_RUN');

-- Existing FACE_MULTI_PERSON becomes the T2I group/multi premium identity.
UPDATE public.pricing_skus
SET name='Face Studio - Multi-Person T2I Premium',
    unit='run',
    category='face',
    provider_hint='openai',
    default_unit_credits=8,
    status='active',
    effective_to=NULL,
    metadata_json=COALESCE(metadata_json,'{}'::jsonb) || jsonb_build_object(
      'mode','t2i',
      'multi_person',true,
      'premium',true,
      'minimum_participants',2,
      'participant_count_in_sku',false,
      'participant_scaling','natural_usage_premium_rate_only',
      'pricing_policy','multi_person_workload_v2',
      'quantity_param','num_edits',
      'premium_rate_multiplier',1.20,
      'source_sku','IMG_STD_RUN',
      'provider','openai',
      'provider_model','gpt-image-2',
      'commercial_alignment','2026-09-29-v4'
    )
WHERE code='FACE_MULTI_PERSON';

-- Separate I2I premium pricing identity. Generation implementation is unchanged.
INSERT INTO public.pricing_skus(
  code,name,unit,category,provider_hint,default_unit_credits,status,
  effective_from,effective_to,metadata_json
)
SELECT
  'FACE_MULTI_PERSON_I2I',
  'Face Studio - Multi-Person I2I Premium',
  src.unit,
  'face',
  'openai',
  15,
  'active',
  now(),
  NULL,
  COALESCE(src.metadata_json,'{}'::jsonb) || jsonb_build_object(
    'mode','i2i',
    'multi_person',true,
    'premium',true,
    'minimum_participants',2,
    'participant_count_in_sku',false,
    'participant_scaling','natural_usage_premium_rate_only',
    'pricing_policy','multi_person_workload_v2',
    'quantity_param','num_edits',
    'premium_rate_multiplier',1.20,
    'source_sku','FACE_EDIT_PREMIUM_RUN',
    'provider','openai',
    'provider_model','gpt-image-2',
    'commercial_alignment','2026-09-29-v4'
  )
FROM public.pricing_skus src
WHERE src.code='FACE_EDIT_PREMIUM_RUN'
ON CONFLICT(code) DO UPDATE SET
  name=EXCLUDED.name,
  unit=EXCLUDED.unit,
  category=EXCLUDED.category,
  provider_hint=EXCLUDED.provider_hint,
  default_unit_credits=EXCLUDED.default_unit_credits,
  status='active',
  effective_to=NULL,
  metadata_json=EXCLUDED.metadata_json;

INSERT INTO public.pricing_variants(code,name,category,is_active,metadata_json)
VALUES(
  'FACE_MULTI_PERSON_I2I',
  'Face Studio - Multi-Person I2I Premium',
  'face',
  true,
  jsonb_build_object(
    'mode','i2i',
    'multi_person',true,
    'premium',true,
    'minimum_participants',2,
    'participant_count_in_sku',false,
    'participant_scaling','natural_usage_premium_rate_only',
    'pricing_policy','multi_person_workload_v2',
    'qty_param','num_edits',
    'premium_rate_multiplier',1.20,
    'source_variant','FACE_I2I',
    'commercial_alignment','2026-09-29-v4'
  )
)
ON CONFLICT(code) DO UPDATE SET
  name=EXCLUDED.name,
  category=EXCLUDED.category,
  is_active=true,
  metadata_json=EXCLUDED.metadata_json;

DELETE FROM public.pricing_variant_lines WHERE variant_code='FACE_MULTI_PERSON_I2I';
INSERT INTO public.pricing_variant_lines(
  variant_code,sku_code,qty_mode,qty_value,qty_param,metadata_json
) VALUES(
  'FACE_MULTI_PERSON_I2I','FACE_MULTI_PERSON_I2I','param',NULL,'num_edits',
  jsonb_build_object(
    'mode','i2i',
    'multi_person',true,
    'pricing_policy','multi_person_workload_v2',
    'source_sku','FACE_EDIT_PREMIUM_RUN'
  )
);

UPDATE public.pricing_variants
SET metadata_json=COALESCE(metadata_json,'{}'::jsonb) || jsonb_build_object(
      'mode','t2i',
      'multi_person',true,
      'premium',true,
      'participant_count_in_sku',false,
      'participant_scaling','natural_usage_premium_rate_only',
      'pricing_policy','multi_person_workload_v2',
      'premium_rate_multiplier',1.20,
      'source_variant','FACE_T2I',
      'commercial_alignment','2026-09-29-v4'
    )
WHERE code='FACE_MULTI_PERSON';

-- Audio: natural aggregate characters. No forced 20% image/video policy.
UPDATE public.pricing_skus
SET default_unit_credits=4,
    provider_hint='azure_tts',
    metadata_json=COALESCE(metadata_json,'{}'::jsonb) || jsonb_build_object(
      'pricing_policy','multi_person_workload_v2',
      'participant_scaling','aggregate_natural_usage',
      'commercial_alignment','2026-09-29-v4'
    )
WHERE code='AUDIO_MULTI_PERSON';

-- Normal direct video and group/multi parent video.
UPDATE public.pricing_skus
SET provider_hint='provider-neutral',
    default_unit_credits=275,
    metadata_json=COALESCE(metadata_json,'{}'::jsonb) || jsonb_build_object(
      'provider_neutral',true,
      'pricing_owner','svc-pricing',
      'commercial_alignment','2026-09-29-v4',
      'cost_basis','conservative one-attempt talking-provider minute'
    )
WHERE code='FUSION_TALK_MIN';

UPDATE public.pricing_skus
SET provider_hint='provider-neutral',
    default_unit_credits=330,
    metadata_json=COALESCE(metadata_json,'{}'::jsonb) || jsonb_build_object(
      'multi_person',true,
      'premium',true,
      'minimum_participants',2,
      'participant_count_in_sku',false,
      'participant_scaling','natural_usage_premium_rate_only',
      'pricing_policy','multi_person_workload_v2',
      'premium_rate_multiplier',1.20,
      'source_sku','FUSION_TALK_MIN',
      'provider_neutral',true,
      'v3_scene_parent_billing',true,
      'commercial_alignment','2026-09-29-v4'
    )
WHERE code='FUSION_MULTI_PERSON';

UPDATE public.pricing_variants
SET metadata_json=COALESCE(metadata_json,'{}'::jsonb) || jsonb_build_object(
      'multi_person',true,
      'premium',true,
      'participant_count_in_sku',false,
      'participant_scaling','natural_usage_premium_rate_only',
      'pricing_policy','multi_person_workload_v2',
      'premium_rate_multiplier',1.20,
      'source_variant','FUSION_TALKING_VIDEO',
      'commercial_alignment','2026-09-29-v4'
    )
WHERE code='FUSION_MULTI_PERSON';

-- Apply deterministic credit overrides to active pricebooks for only this scope.
INSERT INTO public.pricing_sku_prices(
  pricebook_id,sku_code,unit_credits_override,unit_money_override,min_qty,max_qty,metadata_json
)
SELECT
  pb.id,
  p.sku_code,
  CEIL(p.target_credits/COALESCE(pb.multiplier,1))::bigint,
  NULL,
  CASE
    WHEN p.sku_code IN ('FACE_MULTI_PERSON','FACE_MULTI_PERSON_I2I','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON')
      THEN 1
    ELSE NULL
  END,
  NULL,
  jsonb_build_object(
    'commercial_alignment','2026-09-29-v4',
    'target_effective_credit_floor',p.target_credits,
    'money_derived_from_credit_value',true
  )
FROM public.pricing_pricebooks pb
CROSS JOIN _commercial_prices p
WHERE pb.is_active=true
  AND pb.effective_from<=now()
  AND (pb.effective_to IS NULL OR pb.effective_to>now())
ON CONFLICT(pricebook_id,sku_code) DO UPDATE SET
  unit_credits_override=EXCLUDED.unit_credits_override,
  unit_money_override=NULL,
  min_qty=COALESCE(EXCLUDED.min_qty,public.pricing_sku_prices.min_qty),
  max_qty=CASE
    WHEN EXCLUDED.sku_code IN ('FACE_MULTI_PERSON','FACE_MULTI_PERSON_I2I','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON')
      THEN NULL
    ELSE public.pricing_sku_prices.max_qty
  END,
  metadata_json=COALESCE(public.pricing_sku_prices.metadata_json,'{}'::jsonb) || EXCLUDED.metadata_json;

-- Derive premium effective credits from base so pricebook rounding can never
-- make the premium less than +20%.
WITH pairs(multi_sku,base_sku) AS (
  VALUES
    ('FACE_MULTI_PERSON','IMG_STD_RUN'),
    ('FACE_MULTI_PERSON_I2I','FACE_EDIT_PREMIUM_RUN'),
    ('FUSION_MULTI_PERSON','FUSION_TALK_MIN')
),
base_effective AS (
  SELECT
    pb.id pricebook_id,
    COALESCE(pb.multiplier,1) multiplier,
    p.multi_sku,
    CEIL(
      COALESCE(bp.unit_credits_override,b.default_unit_credits)::numeric
      * COALESCE(pb.multiplier,1)
    )::bigint base_credits
  FROM public.pricing_pricebooks pb
  CROSS JOIN pairs p
  JOIN public.pricing_skus b ON b.code=p.base_sku
  LEFT JOIN public.pricing_sku_prices bp
    ON bp.pricebook_id=pb.id AND bp.sku_code=p.base_sku
  WHERE pb.is_active=true
    AND pb.effective_from<=now()
    AND (pb.effective_to IS NULL OR pb.effective_to>now())
),
desired AS (
  SELECT
    pricebook_id,
    multiplier,
    multi_sku,
    CEIL(base_credits*1.20)::bigint desired_effective_credits
  FROM base_effective
)
UPDATE public.pricing_sku_prices mp
SET unit_credits_override=CEIL(d.desired_effective_credits/d.multiplier)::bigint,
    unit_money_override=NULL,
    min_qty=1,
    max_qty=NULL,
    metadata_json=COALESCE(mp.metadata_json,'{}'::jsonb) || jsonb_build_object(
      'premium_rate_multiplier',1.20,
      'premium_floor_from_effective_base',true,
      'commercial_alignment','2026-09-29-v4'
    )
FROM desired d
WHERE mp.pricebook_id=d.pricebook_id
  AND mp.sku_code=d.multi_sku;

-- Retire stale/placeholder economics only for the SKUs owned by this migration.
UPDATE public.pricing_sku_costs
SET is_active=false,
    effective_to=COALESCE(effective_to,'2026-09-30 00:15:00+00'::timestamptz)
WHERE sku_code IN (
  'IMG_STD_RUN','IMG_HD_RUN','FACE_EDIT_PREMIUM_RUN',
  'FACE_MULTI_PERSON','FACE_MULTI_PERSON_I2I','AUDIO_MULTI_PERSON',
  'FUSION_TALK_MIN','FUSION_MULTI_PERSON'
)
AND is_active=true
AND effective_from <> '2026-09-30 00:15:00+00'::timestamptz;

CREATE TEMP TABLE _commercial_costs(
  sku_code text PRIMARY KEY,
  component_code text NOT NULL,
  variable_cost numeric(18,8) NOT NULL CHECK(variable_cost>0),
  basis text NOT NULL
) ON COMMIT DROP;

INSERT INTO _commercial_costs VALUES
  ('IMG_STD_RUN','openai_gpt_image_2_high_conservative',0.22000000,
   'GPT Image 2 high output upper bound for common Face sizes plus small prompt allowance'),
  ('IMG_HD_RUN','openai_gpt_image_2_high_conservative',0.22000000,
   'GPT Image 2 high output upper bound for supported Face sizes plus small prompt allowance'),
  ('FACE_EDIT_PREMIUM_RUN','openai_gpt_image_2_edit_conservative',0.30000000,
   'GPT Image 2 high output plus high-fidelity image-input and prompt allowance'),
  ('FACE_MULTI_PERSON','openai_gpt_image_2_high_conservative',0.22000000,
   'same provider generation cost as T2I; premium is customer-facing engineering value'),
  ('FACE_MULTI_PERSON_I2I','openai_gpt_image_2_edit_conservative',0.30000000,
   'same provider generation cost as I2I; premium is customer-facing engineering value'),
  ('AUDIO_MULTI_PERSON','azure_neural_tts_payg',0.01600000,
   'Azure standard neural TTS per 1K characters'),
  ('FUSION_TALK_MIN','talking_provider_public_upper_bound',9.60000000,
   'USD 0.16/sec x 60 sec conservative one-attempt talking-provider bound'),
  ('FUSION_MULTI_PERSON','parent_retry_failover_reserve',13.50000000,
   'provider-neutral parent reserve above current Sync3 normal minute cost with retry/failover allowance');

INSERT INTO public.pricing_sku_costs(
  sku_code,component_code,cost_model,cost_currency,
  variable_cost_money,fixed_monthly_cost_money,assumed_monthly_units,
  is_active,effective_from,effective_to,metadata_json
)
SELECT
  c.sku_code,c.component_code,'variable','USD',c.variable_cost,0,0,true,
  '2026-09-30 00:15:00+00'::timestamptz,NULL,
  jsonb_build_object(
    'source','commercial_alignment_2026_09_29_v4',
    'basis',c.basis,
    'verified_date','2026-09-29',
    'customer_pricing_impact',false,
    'replace_with_contract_or_metered_rate_when_available',true
  )
FROM _commercial_costs c
ON CONFLICT(sku_code,component_code,effective_from) DO UPDATE SET
  cost_model=EXCLUDED.cost_model,
  cost_currency=EXCLUDED.cost_currency,
  variable_cost_money=EXCLUDED.variable_cost_money,
  fixed_monthly_cost_money=EXCLUDED.fixed_monthly_cost_money,
  assumed_monthly_units=EXCLUDED.assumed_monthly_units,
  is_active=true,
  effective_to=NULL,
  metadata_json=EXCLUDED.metadata_json;

-- Fail closed on top-up floor, premium relation, COGS, and stale provider cost rows.
DO $$
DECLARE
  bad_packs integer;
  missing_costs text;
  premium_failures integer;
  stale_active_costs integer;
BEGIN
  SELECT count(*) INTO bad_packs
  FROM public.pricing_credit_packs p
  JOIN _plan_credit_floor f ON f.currency=upper(p.currency)
  WHERE p.is_active=true
    AND p.credits>0
    AND p.price_money/p.credits::numeric < f.money_per_credit;

  IF bad_packs<>0 THEN
    RAISE EXCEPTION 'top-up credit floor failures=%',bad_packs;
  END IF;

  SELECT string_agg(p.sku_code,', ' ORDER BY p.sku_code)
  INTO missing_costs
  FROM _commercial_prices p
  WHERE NOT EXISTS (
    SELECT 1
    FROM public.pricing_sku_costs c
    WHERE c.sku_code=p.sku_code
      AND c.is_active=true
      AND c.effective_from<=now()
      AND (c.effective_to IS NULL OR c.effective_to>now())
      AND (
        c.variable_cost_money
        + CASE
            WHEN c.assumed_monthly_units>0
              THEN c.fixed_monthly_cost_money/c.assumed_monthly_units
            ELSE 0
          END
      )>0
  );

  IF missing_costs IS NOT NULL THEN
    RAISE EXCEPTION 'commercial alignment v4 missing COGS for SKU(s): %',missing_costs;
  END IF;

  WITH pairs(multi_sku,base_sku) AS (
    VALUES
      ('FACE_MULTI_PERSON','IMG_STD_RUN'),
      ('FACE_MULTI_PERSON_I2I','FACE_EDIT_PREMIUM_RUN'),
      ('FUSION_MULTI_PERSON','FUSION_TALK_MIN')
  ),
  effective AS (
    SELECT
      pb.id,
      p.multi_sku,
      CEIL(COALESCE(bsp.unit_credits_override,b.default_unit_credits)::numeric*COALESCE(pb.multiplier,1))::bigint base_credits,
      CEIL(COALESCE(msp.unit_credits_override,m.default_unit_credits)::numeric*COALESCE(pb.multiplier,1))::bigint multi_credits
    FROM public.pricing_pricebooks pb
    CROSS JOIN pairs p
    JOIN public.pricing_skus b ON b.code=p.base_sku
    JOIN public.pricing_skus m ON m.code=p.multi_sku
    LEFT JOIN public.pricing_sku_prices bsp
      ON bsp.pricebook_id=pb.id AND bsp.sku_code=p.base_sku
    LEFT JOIN public.pricing_sku_prices msp
      ON msp.pricebook_id=pb.id AND msp.sku_code=p.multi_sku
    WHERE pb.is_active=true
      AND pb.effective_from<=now()
      AND (pb.effective_to IS NULL OR pb.effective_to>now())
  )
  SELECT count(*) INTO premium_failures
  FROM effective
  WHERE multi_credits < CEIL(base_credits*1.20)::bigint;

  IF premium_failures<>0 THEN
    RAISE EXCEPTION '+20%% premium failures=%',premium_failures;
  END IF;

  SELECT count(*) INTO stale_active_costs
  FROM public.pricing_sku_costs c
  WHERE c.sku_code IN ('IMG_STD_RUN','IMG_HD_RUN','FUSION_TALK_MIN')
    AND c.is_active=true
    AND c.effective_from<=now()
    AND (c.effective_to IS NULL OR c.effective_to>now())
    AND (
      c.component_code ~* 'fal|heygen|placeholder'
      OR COALESCE(c.metadata_json::text,'') ~* 'fal_subscription|heygen_subscription|placeholder|update_required'
    );

  IF stale_active_costs<>0 THEN
    RAISE EXCEPTION 'stale/placeholder active cost rows=%',stale_active_costs;
  END IF;
END $$;

COMMIT;
