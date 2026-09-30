-- desifaces DEV commercial alignment v3 — 2026-09-29
-- Scope: pricing/SKU/COGS only. No generation workflow, prompt, provider routing,
-- HITL, scene orchestration, or entitlement behavior is changed by this migration.
--
-- Commercial contracts:
--   * Face T2I / I2I use current OpenAI runtime economics.
--   * Multi/group image customer rate is at least +20% over corresponding base.
--   * Multi/group video customer rate is at least +20% over base video.
--   * Participant count is metadata only; natural workload is not multiplied.
--   * Audio retains natural 1K-character economics.
--   * Internal child video renders remain customer-zero-charge; their retry/failover
--     risk is represented in the parent multi-person COGS allowance.
--   * All active launch video SKUs receive non-zero provider COGS.
--   * Usage SKU money overrides are cleared so money derives deterministically
--     from credits and the active currency credit-value contract.
--
-- This migration is idempotent at EFFECTIVE_AT and fails closed on catalog gaps.

BEGIN;

DO $$
BEGIN
  IF to_regclass('public.pricing_skus') IS NULL
     OR to_regclass('public.pricing_variants') IS NULL
     OR to_regclass('public.pricing_variant_lines') IS NULL
     OR to_regclass('public.pricing_pricebooks') IS NULL
     OR to_regclass('public.pricing_sku_prices') IS NULL
     OR to_regclass('public.pricing_sku_costs') IS NULL THEN
    RAISE EXCEPTION 'commercial alignment requires complete pricing catalog tables';
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.pricing_pricebooks
    WHERE is_active = true
      AND effective_from <= now()
      AND (effective_to IS NULL OR effective_to > now())
      AND COALESCE(multiplier,1) <= 0
  ) THEN
    RAISE EXCEPTION 'active pricing pricebook multiplier must be > 0';
  END IF;
END $$;

CREATE TEMP TABLE _commercial_prices (
  sku_code text PRIMARY KEY,
  target_credits bigint NOT NULL CHECK (target_credits > 0)
) ON COMMIT DROP;

INSERT INTO _commercial_prices(sku_code,target_credits) VALUES
  ('IMG_STD_RUN', 40),
  ('IMG_HD_RUN', 40),
  ('FACE_EDIT_PREMIUM_RUN', 50),
  ('FACE_MULTI_PERSON', 48),
  ('FACE_MULTI_PERSON_I2I', 60),
  ('AUDIO_MULTI_PERSON', 4),
  ('FUSION_TALK_MIN', 2500),
  ('FUSION_MULTI_PERSON', 3000),
  ('LONGFORM_CINEMATIC_MIN', 4000),
  ('LONGFORM_TALK_MIN', 2100),
  ('LONGFORM_TALK_ECONOMY_10S', 150),
  ('LONGFORM_TALK_ECONOMY_20S', 300),
  ('LONGFORM_TALK_ECONOMY_30S', 450),
  ('LONGFORM_TALK_PREMIUM_10S', 499),
  ('LONGFORM_TALK_PREMIUM_20S', 899),
  ('LONGFORM_TALK_PREMIUM_30S', 1299),
  ('LONGFORM_TALK_PREMIUM_SECOND', 25);

-- Baseline SKUs required before deriving premium SKUs.
DO $$
DECLARE missing text;
BEGIN
  SELECT string_agg(x.code, ', ' ORDER BY x.code)
  INTO missing
  FROM (VALUES
    ('IMG_STD_RUN'),
    ('IMG_HD_RUN'),
    ('FACE_EDIT_PREMIUM_RUN'),
    ('AUDIO_TTS_1K_CHARS'),
    ('AUDIO_MULTI_PERSON'),
    ('FUSION_TALK_MIN'),
    ('FUSION_MULTI_PERSON'),
    ('LONGFORM_CINEMATIC_MIN'),
    ('LONGFORM_TALK_MIN'),
    ('LONGFORM_TALK_ECONOMY_10S'),
    ('LONGFORM_TALK_ECONOMY_20S'),
    ('LONGFORM_TALK_ECONOMY_30S'),
    ('LONGFORM_TALK_PREMIUM_10S'),
    ('LONGFORM_TALK_PREMIUM_20S'),
    ('LONGFORM_TALK_PREMIUM_30S'),
    ('LONGFORM_TALK_PREMIUM_SECOND')
  ) AS x(code)
  LEFT JOIN public.pricing_skus s ON s.code=x.code
  WHERE s.code IS NULL;

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'commercial alignment missing baseline SKU(s): %', missing;
  END IF;
END $$;

-- Current Face runtime executes both T2I and I2I on OpenAI GPT Image 2.
UPDATE public.pricing_skus
SET provider_hint='openai',
    default_unit_credits=CASE code
      WHEN 'IMG_STD_RUN' THEN 40
      WHEN 'IMG_HD_RUN' THEN 40
      WHEN 'FACE_EDIT_PREMIUM_RUN' THEN 50
      ELSE default_unit_credits
    END,
    metadata_json=COALESCE(metadata_json,'{}'::jsonb) || jsonb_build_object(
      'provider','openai',
      'provider_model','gpt-image-2',
      'quality','high',
      'commercial_alignment','2026-09-29',
      'runtime_truth','svc-face forces provider=openai'
    )
WHERE code IN ('IMG_STD_RUN','IMG_HD_RUN','FACE_EDIT_PREMIUM_RUN');

-- FACE_MULTI_PERSON is now the T2I premium identity.
UPDATE public.pricing_skus
SET name='Face Studio - Multi-Person T2I Premium',
    unit='run',
    category='face',
    provider_hint='openai',
    default_unit_credits=48,
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
      'commercial_alignment','2026-09-29'
    )
WHERE code='FACE_MULTI_PERSON';

-- Separate I2I premium identity so T2I and I2I can each be +20% over the
-- correct baseline without changing generation behavior.
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
  60,
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
    'commercial_alignment','2026-09-29'
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
    'commercial_alignment','2026-09-29'
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
  'FACE_MULTI_PERSON_I2I',
  'FACE_MULTI_PERSON_I2I',
  'param',
  NULL,
  'num_edits',
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
      'commercial_alignment','2026-09-29'
    )
WHERE code='FACE_MULTI_PERSON';

-- Audio keeps natural character metering. Existing premium credits remain unchanged.
UPDATE public.pricing_skus
SET default_unit_credits=4,
    provider_hint='azure_tts',
    metadata_json=COALESCE(metadata_json,'{}'::jsonb) || jsonb_build_object(
      'pricing_policy','multi_person_workload_v2',
      'participant_scaling','aggregate_natural_usage',
      'commercial_alignment','2026-09-29'
    )
WHERE code='AUDIO_MULTI_PERSON';

-- Base video and dedicated multi/group video.
UPDATE public.pricing_skus
SET provider_hint='provider-neutral',
    default_unit_credits=2500,
    metadata_json=COALESCE(metadata_json,'{}'::jsonb) || jsonb_build_object(
      'provider_neutral',true,
      'pricing_owner','svc-pricing',
      'commercial_alignment','2026-09-29',
      'cost_basis','max current talking-provider one-attempt public rate'
    )
WHERE code='FUSION_TALK_MIN';

UPDATE public.pricing_skus
SET provider_hint='provider-neutral',
    default_unit_credits=3000,
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
      'commercial_alignment','2026-09-29'
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
      'commercial_alignment','2026-09-29'
    )
WHERE code='FUSION_MULTI_PERSON';

-- Long-form launch SKUs: update only price floors that were below current
-- conservative provider economics. Existing higher premium bucket prices stay higher.
UPDATE public.pricing_skus s
SET default_unit_credits=p.target_credits,
    metadata_json=COALESCE(s.metadata_json,'{}'::jsonb) || jsonb_build_object(
      'commercial_alignment','2026-09-29',
      'commercial_floor_credits',p.target_credits
    )
FROM _commercial_prices p
WHERE s.code=p.sku_code
  AND s.code IN (
    'LONGFORM_CINEMATIC_MIN','LONGFORM_TALK_MIN',
    'LONGFORM_TALK_ECONOMY_10S','LONGFORM_TALK_ECONOMY_20S','LONGFORM_TALK_ECONOMY_30S',
    'LONGFORM_TALK_PREMIUM_10S','LONGFORM_TALK_PREMIUM_20S','LONGFORM_TALK_PREMIUM_30S',
    'LONGFORM_TALK_PREMIUM_SECOND'
  )
  AND s.default_unit_credits IS DISTINCT FROM p.target_credits;

-- Ensure every active pricebook resolves each affected SKU to at least its
-- commercial floor after applying that pricebook's multiplier. Monetary
-- overrides are intentionally NULL so money derives from credits consistently.
INSERT INTO public.pricing_sku_prices(
  pricebook_id,sku_code,unit_credits_override,unit_money_override,min_qty,max_qty,metadata_json
)
SELECT
  pb.id,
  p.sku_code,
  CEIL(p.target_credits / COALESCE(pb.multiplier,1))::bigint,
  NULL,
  CASE WHEN p.sku_code IN ('FACE_MULTI_PERSON','FACE_MULTI_PERSON_I2I','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON') THEN 1 ELSE NULL END,
  NULL,
  jsonb_build_object(
    'commercial_alignment','2026-09-29',
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

-- Recompute multi/group pricebook overrides from the actual effective base
-- credit result so integer rounding can never make the premium < 20%.
WITH pairs(multi_sku,base_sku) AS (
  VALUES
    ('FACE_MULTI_PERSON','IMG_STD_RUN'),
    ('FACE_MULTI_PERSON_I2I','FACE_EDIT_PREMIUM_RUN'),
    ('FUSION_MULTI_PERSON','FUSION_TALK_MIN')
),
base_effective AS (
  SELECT
    pb.id pricebook_id,
    COALESCE(pb.multiplier,1) AS multiplier,
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
    CEIL(base_credits * 1.20)::bigint desired_effective_credits
  FROM base_effective
)
UPDATE public.pricing_sku_prices mp
SET unit_credits_override=CEIL(d.desired_effective_credits / d.multiplier)::bigint,
    unit_money_override=NULL,
    min_qty=1,
    max_qty=NULL,
    metadata_json=COALESCE(mp.metadata_json,'{}'::jsonb) || jsonb_build_object(
      'premium_rate_multiplier',1.20,
      'premium_floor_from_effective_base',true,
      'commercial_alignment','2026-09-29'
    )
FROM desired d
WHERE mp.pricebook_id=d.pricebook_id
  AND mp.sku_code=d.multi_sku;

-- Retire stale/placeholder economics for SKUs this migration owns.
UPDATE public.pricing_sku_costs
SET is_active=false,
    effective_to=COALESCE(effective_to,'2026-09-29 23:55:00+00'::timestamptz)
WHERE sku_code IN (
  'IMG_STD_RUN','IMG_HD_RUN','FACE_EDIT_PREMIUM_RUN',
  'FACE_MULTI_PERSON','FACE_MULTI_PERSON_I2I','AUDIO_MULTI_PERSON',
  'FUSION_TALK_MIN','FUSION_MULTI_PERSON',
  'LONGFORM_CINEMATIC_MIN','LONGFORM_TALK_MIN',
  'LONGFORM_TALK_ECONOMY_10S','LONGFORM_TALK_ECONOMY_20S','LONGFORM_TALK_ECONOMY_30S',
  'LONGFORM_TALK_PREMIUM_10S','LONGFORM_TALK_PREMIUM_20S','LONGFORM_TALK_PREMIUM_30S',
  'LONGFORM_TALK_PREMIUM_SECOND'
)
AND is_active=true
AND effective_from <> '2026-09-29 23:55:00+00'::timestamptz;

CREATE TEMP TABLE _commercial_costs(
  sku_code text PRIMARY KEY,
  component_code text NOT NULL,
  variable_cost numeric(18,8) NOT NULL CHECK(variable_cost>0),
  basis text NOT NULL
) ON COMMIT DROP;

INSERT INTO _commercial_costs VALUES
  ('IMG_STD_RUN','openai_gpt_image_2_high_conservative',0.22000000,
   'GPT Image 2 high common-size output upper bound plus prompt allowance'),
  ('IMG_HD_RUN','openai_gpt_image_2_high_conservative',0.22000000,
   'GPT Image 2 high common-size output upper bound plus prompt allowance'),
  ('FACE_EDIT_PREMIUM_RUN','openai_gpt_image_2_edit_conservative',0.30000000,
   'GPT Image 2 high output plus high-fidelity image-input and prompt allowance'),
  ('FACE_MULTI_PERSON','openai_gpt_image_2_high_conservative',0.22000000,
   'same provider generation cost as T2I; +20 percent is customer premium not provider COGS'),
  ('FACE_MULTI_PERSON_I2I','openai_gpt_image_2_edit_conservative',0.30000000,
   'same provider generation cost as I2I; +20 percent is customer premium not provider COGS'),
  ('AUDIO_MULTI_PERSON','azure_neural_tts_payg',0.01600000,
   'Azure standard neural TTS per 1K characters'),
  ('FUSION_TALK_MIN','talking_provider_public_upper_bound',9.60000000,
   'USD 0.16/sec x 60 sec conservative one-attempt talking-provider bound'),
  ('FUSION_MULTI_PERSON','one_full_retry_conservative_bound',19.20000000,
   'USD 0.16/sec x 60 sec x 2 attempts; conservative one-full-retry/fallback bound'),
  ('LONGFORM_CINEMATIC_MIN','cinematic_provider_bundle_conservative',21.60000000,
   'conservative presenter USD 0.16/sec plus 720p cinematic background USD 0.20/sec for 60 sec'),
  ('LONGFORM_TALK_MIN','talking_provider_public_upper_bound',9.60000000,
   'USD 0.16/sec x 60 sec conservative talking-provider bound'),
  ('LONGFORM_TALK_ECONOMY_10S','veed_fabric_480p_public_rate',0.80000000,
   'USD 0.08/sec x 10 sec'),
  ('LONGFORM_TALK_ECONOMY_20S','veed_fabric_480p_public_rate',1.60000000,
   'USD 0.08/sec x 20 sec'),
  ('LONGFORM_TALK_ECONOMY_30S','veed_fabric_480p_public_rate',2.40000000,
   'USD 0.08/sec x 30 sec'),
  ('LONGFORM_TALK_PREMIUM_10S','premium_presenter_public_upper_bound',1.50000000,
   'USD 0.15/sec x 10 sec; conservative max of VEED 720p and Kling Avatar Pro'),
  ('LONGFORM_TALK_PREMIUM_20S','premium_presenter_public_upper_bound',3.00000000,
   'USD 0.15/sec x 20 sec; conservative max of VEED 720p and Kling Avatar Pro'),
  ('LONGFORM_TALK_PREMIUM_30S','premium_presenter_public_upper_bound',4.50000000,
   'USD 0.15/sec x 30 sec; conservative max of VEED 720p and Kling Avatar Pro'),
  ('LONGFORM_TALK_PREMIUM_SECOND','premium_presenter_public_upper_bound',0.15000000,
   'USD 0.15 per output second conservative premium presenter rate');

INSERT INTO public.pricing_sku_costs(
  sku_code,component_code,cost_model,cost_currency,
  variable_cost_money,fixed_monthly_cost_money,assumed_monthly_units,
  is_active,effective_from,effective_to,metadata_json
)
SELECT
  c.sku_code,
  c.component_code,
  'variable',
  'USD',
  c.variable_cost,
  0,
  0,
  true,
  '2026-09-29 23:55:00+00'::timestamptz,
  NULL,
  jsonb_build_object(
    'source','commercial_alignment_2026_09_29',
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

-- Fail closed: every commercial target must exist and have exactly one or more
-- positive active costs, and each active pricebook must meet the credit floor.
DO $$
DECLARE
  missing_skus text;
  missing_costs text;
  floor_failures integer;
  premium_failures integer;
  stale_active_costs integer;
BEGIN
  SELECT string_agg(p.sku_code,', ' ORDER BY p.sku_code)
  INTO missing_skus
  FROM _commercial_prices p
  LEFT JOIN public.pricing_skus s ON s.code=p.sku_code AND s.status='active'
  WHERE s.code IS NULL;

  IF missing_skus IS NOT NULL THEN
    RAISE EXCEPTION 'commercial alignment missing active SKU(s): %',missing_skus;
  END IF;

  SELECT string_agg(p.sku_code,', ' ORDER BY p.sku_code)
  INTO missing_costs
  FROM _commercial_prices p
  WHERE NOT EXISTS (
    SELECT 1 FROM public.pricing_sku_costs c
    WHERE c.sku_code=p.sku_code
      AND c.is_active=true
      AND c.effective_from<=now()
      AND (c.effective_to IS NULL OR c.effective_to>now())
      AND (
        c.variable_cost_money
        + CASE WHEN c.assumed_monthly_units>0
               THEN c.fixed_monthly_cost_money/c.assumed_monthly_units
               ELSE 0 END
      ) > 0
  );

  IF missing_costs IS NOT NULL THEN
    RAISE EXCEPTION 'commercial alignment missing COGS for SKU(s): %',missing_costs;
  END IF;

  SELECT count(*) INTO floor_failures
  FROM public.pricing_pricebooks pb
  CROSS JOIN _commercial_prices p
  JOIN public.pricing_skus s ON s.code=p.sku_code
  LEFT JOIN public.pricing_sku_prices sp
    ON sp.pricebook_id=pb.id AND sp.sku_code=p.sku_code
  WHERE pb.is_active=true
    AND pb.effective_from<=now()
    AND (pb.effective_to IS NULL OR pb.effective_to>now())
    AND CEIL(COALESCE(sp.unit_credits_override,s.default_unit_credits)::numeric * pb.multiplier)
        < p.target_credits;

  IF floor_failures <> 0 THEN
    RAISE EXCEPTION 'commercial alignment active pricebook floor failures=%',floor_failures;
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
    LEFT JOIN public.pricing_sku_prices bsp ON bsp.pricebook_id=pb.id AND bsp.sku_code=p.base_sku
    LEFT JOIN public.pricing_sku_prices msp ON msp.pricebook_id=pb.id AND msp.sku_code=p.multi_sku
    WHERE pb.is_active=true
      AND pb.effective_from<=now()
      AND (pb.effective_to IS NULL OR pb.effective_to>now())
  )
  SELECT count(*) INTO premium_failures
  FROM effective
  WHERE multi_credits < CEIL(base_credits*1.20)::bigint;

  IF premium_failures <> 0 THEN
    RAISE EXCEPTION 'commercial alignment +20%% premium failures=%',premium_failures;
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

  IF stale_active_costs <> 0 THEN
    RAISE EXCEPTION 'commercial alignment stale/placeholder active cost rows=%',stale_active_costs;
  END IF;
END $$;

COMMIT;
