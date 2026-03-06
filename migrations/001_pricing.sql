-- services/svc-pricing/app/app/migrations/0001_pricing_core.sql
-- DesiFaces: svc-pricing schema (idempotent, Postgres-safe)
--
-- Key guarantees:
-- 1) No "PRIMARY KEY(... coalesce(...))" (Postgres disallows expressions in PK)
-- 2) No "ALTER TABLE ... ADD CONSTRAINT IF NOT EXISTS" (Postgres disallows)
-- 3) If pricing_variant_lines / pricing_tier_prices exist with a wrong PK, we fix them.
--
-- Conventions used:
-- - qty_param and country_code are stored as NOT NULL text with default '' where we need PK stability.
--   This avoids expression PKs and keeps lookups simple.

BEGIN;

-- ------------------------------------------------------------
-- Extensions
-- ------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ------------------------------------------------------------
-- Core tables (from your original SQL; kept compatible)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing_tiers (
  code text PRIMARY KEY,                         -- free, pro, enterprise, developer
  name text NOT NULL,
  monthly_grant_credits bigint NOT NULL DEFAULT 0,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pricing_user_entitlements (
  user_id uuid PRIMARY KEY,
  tier_code text NOT NULL REFERENCES pricing_tiers(code),
  effective_from timestamptz NOT NULL DEFAULT now(),
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS pricing_credit_accounts (
  user_id uuid PRIMARY KEY,
  balance_credits bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Add reserved_credits for holds (idempotent)
ALTER TABLE pricing_credit_accounts
  ADD COLUMN IF NOT EXISTS reserved_credits bigint NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_pricing_accounts_updated
  ON pricing_credit_accounts(updated_at DESC);

-- SKU catalog
CREATE TABLE IF NOT EXISTS pricing_skus (
  code text PRIMARY KEY,                         -- IMG_STD_GEN, VIDEO_SEC_STANDARD, ...
  name text NOT NULL,
  unit text NOT NULL,                            -- run, second, 1k_chars, minute
  category text NOT NULL,                        -- face, audio, fusion, music, api, commerce, infra
  provider_hint text NULL,                       -- fal, openai, azure_tts, heygen, native
  default_unit_credits bigint NOT NULL,          -- default credits per unit
  status text NOT NULL DEFAULT 'active',         -- active, inactive
  effective_from timestamptz NOT NULL DEFAULT now(),
  effective_to timestamptz NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Credit value per currency (money-per-credit)
CREATE TABLE IF NOT EXISTS pricing_credit_value (
  currency text NOT NULL,                        -- USD, INR
  money_per_credit numeric(18,8) NOT NULL,       -- e.g. 0.01000000 for USD
  rounding_mode text NOT NULL DEFAULT 'ceil',    -- ceil, round, floor
  effective_from timestamptz NOT NULL DEFAULT now(),
  effective_to timestamptz NULL,
  PRIMARY KEY(currency, effective_from)
);

-- FX rates (optional)
CREATE TABLE IF NOT EXISTS pricing_fx_rates (
  base_currency text NOT NULL,                   -- USD
  quote_currency text NOT NULL,                  -- INR
  rate numeric(18,8) NOT NULL,                   -- 1 USD = rate INR
  as_of timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(base_currency, quote_currency, as_of)
);

-- Pricebooks (kept close to your design; nullable country/tier allowed)
CREATE TABLE IF NOT EXISTS pricing_pricebooks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  country_code text NULL,                        -- 'IN', 'US', null means global
  currency text NOT NULL,                        -- USD, INR
  channel text NOT NULL,                         -- web, mobile, api
  tier_code text NULL REFERENCES pricing_tiers(code), -- optional
  multiplier numeric(10,6) NOT NULL DEFAULT 1.0,  -- regional/tier multiplier on credits
  is_active boolean NOT NULL DEFAULT true,
  effective_from timestamptz NOT NULL DEFAULT now(),
  effective_to timestamptz NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Per-SKU overrides inside a pricebook
CREATE TABLE IF NOT EXISTS pricing_sku_prices (
  pricebook_id uuid NOT NULL REFERENCES pricing_pricebooks(id) ON DELETE CASCADE,
  sku_code text NOT NULL REFERENCES pricing_skus(code),
  unit_credits_override bigint NULL,
  unit_money_override numeric(18,8) NULL,
  min_qty bigint NULL,
  max_qty bigint NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY(pricebook_id, sku_code)
);

-- Ledger events (idempotent)
CREATE TABLE IF NOT EXISTS pricing_credit_ledger_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  event_type text NOT NULL,                      -- grant, topup, consume, refund, adjust, reserve_hold, reserve_release
  credits_delta bigint NOT NULL,                 -- + or -
  sku_code text NULL REFERENCES pricing_skus(code),
  quantity numeric(18,6) NULL,
  unit_credits bigint NULL,
  idempotency_key text NOT NULL,
  country_code text NULL,
  currency text NULL,
  money_amount numeric(18,8) NULL,               -- money charged (if applicable)
  channel text NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_pricing_ledger_user_created
  ON pricing_credit_ledger_events(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_pricing_pricebooks_lookup
  ON pricing_pricebooks(is_active, currency, channel, country_code, tier_code, effective_from DESC);

-- ------------------------------------------------------------
-- Reservations: quote -> reserve -> finalize/release
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing_credit_reservations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  status text NOT NULL DEFAULT 'reserved',       -- reserved|finalized|released|expired
  pricebook_id uuid NULL REFERENCES pricing_pricebooks(id),
  country_code text NULL,
  currency text NULL,
  channel text NULL,                             -- web|mobile|api
  tier_code text NULL,
  quote_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  reserved_credits bigint NOT NULL,
  estimated_money numeric(18,8) NULL,
  idempotency_key text NOT NULL,
  job_ref text NULL,                             -- e.g. "svc-face:JOB_UUID"
  expires_at timestamptz NOT NULL,
  finalized_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_pricing_reservations_user_status
  ON pricing_credit_reservations(user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_pricing_reservations_expires
  ON pricing_credit_reservations(status, expires_at);

-- ------------------------------------------------------------
-- Variants (BOM templates)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing_variants (
  code text PRIMARY KEY,                         -- MUSIC_VIDEO_STANDARD, COMMERCE_PRODUCT_PACK
  name text NOT NULL,
  category text NOT NULL,                        -- face|audio|fusion|music|commerce|api
  is_active boolean NOT NULL DEFAULT true,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- pricing_variant_lines:
-- IMPORTANT: qty_param is NOT NULL DEFAULT '' to allow a PK without expressions.
CREATE TABLE IF NOT EXISTS pricing_variant_lines (
  variant_code text NOT NULL REFERENCES pricing_variants(code) ON DELETE CASCADE,
  sku_code text NOT NULL REFERENCES pricing_skus(code),
  qty_mode text NOT NULL DEFAULT 'fixed',        -- fixed|param|metered
  qty_value numeric(18,6) NULL,                  -- for fixed qty
  qty_param text NOT NULL DEFAULT '',            -- '' means "no param"
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (variant_code, sku_code, qty_mode, qty_param)
);

-- If table existed earlier with qty_param nullable, normalize it safely:
UPDATE pricing_variant_lines
  SET qty_param = ''
  WHERE qty_param IS NULL;

ALTER TABLE pricing_variant_lines
  ALTER COLUMN qty_param SET DEFAULT '';

ALTER TABLE pricing_variant_lines
  ALTER COLUMN qty_param SET NOT NULL;

-- Ensure qty_mode check constraint exists (idempotent)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_pricing_variant_lines_qty_mode'
  ) THEN
    ALTER TABLE pricing_variant_lines
      ADD CONSTRAINT ck_pricing_variant_lines_qty_mode
      CHECK (qty_mode IN ('fixed','param','metered'));
  END IF;
END $$;

-- Ensure PK columns are correct (drop/recreate PK if wrong)
DO $$
DECLARE
  pk_name text;
  pk_cols text[];
BEGIN
  SELECT c.conname INTO pk_name
  FROM pg_constraint c
  WHERE c.conrelid = 'pricing_variant_lines'::regclass
    AND c.contype = 'p'
  LIMIT 1;

  IF pk_name IS NOT NULL THEN
    SELECT array_agg(a.attname ORDER BY k.ord) INTO pk_cols
    FROM pg_constraint c
    JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
      ON true
    JOIN pg_attribute a
      ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    WHERE c.conrelid = 'pricing_variant_lines'::regclass
      AND c.contype = 'p';

    IF pk_cols IS DISTINCT FROM ARRAY['variant_code','sku_code','qty_mode','qty_param'] THEN
      EXECUTE format('ALTER TABLE pricing_variant_lines DROP CONSTRAINT %I', pk_name);
      EXECUTE 'ALTER TABLE pricing_variant_lines ADD CONSTRAINT pricing_variant_lines_pkey PRIMARY KEY (variant_code, sku_code, qty_mode, qty_param)';
    END IF;
  ELSE
    EXECUTE 'ALTER TABLE pricing_variant_lines ADD CONSTRAINT pricing_variant_lines_pkey PRIMARY KEY (variant_code, sku_code, qty_mode, qty_param)';
  END IF;
END $$;

-- ------------------------------------------------------------
-- Credit packs (PAYG)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pricing_credit_packs (
  code text PRIMARY KEY,                         -- PACK_1000, PACK_5000
  name text NOT NULL,
  credits bigint NOT NULL,
  currency text NOT NULL,                        -- USD, INR
  price_money numeric(18,8) NOT NULL,
  country_code text NULL,                        -- 'IN', 'US', null means global
  is_active boolean NOT NULL DEFAULT true,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pricing_credit_packs_lookup
  ON pricing_credit_packs(is_active, currency, country_code, credits);

-- ------------------------------------------------------------
-- Tier prices (subscriptions/upgrades)
-- ------------------------------------------------------------
-- IMPORTANT: country_code is NOT NULL DEFAULT '' to allow PK without expressions.
CREATE TABLE IF NOT EXISTS pricing_tier_prices (
  tier_code text NOT NULL REFERENCES pricing_tiers(code),
  currency text NOT NULL,                        -- USD, INR
  country_code text NOT NULL DEFAULT '',         -- '' means global
  monthly_price numeric(18,8) NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tier_code, currency, country_code)
);

-- If an older version existed with nullable country_code, normalize it:
UPDATE pricing_tier_prices
  SET country_code = ''
  WHERE country_code IS NULL;

ALTER TABLE pricing_tier_prices
  ALTER COLUMN country_code SET DEFAULT '';

ALTER TABLE pricing_tier_prices
  ALTER COLUMN country_code SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_pricing_tier_prices_lookup
  ON pricing_tier_prices(is_active, currency, country_code, tier_code);

-- Ensure PK columns are correct (drop/recreate PK if wrong)
DO $$
DECLARE
  pk_name text;
  pk_cols text[];
BEGIN
  SELECT c.conname INTO pk_name
  FROM pg_constraint c
  WHERE c.conrelid = 'pricing_tier_prices'::regclass
    AND c.contype = 'p'
  LIMIT 1;

  IF pk_name IS NOT NULL THEN
    SELECT array_agg(a.attname ORDER BY k.ord) INTO pk_cols
    FROM pg_constraint c
    JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
      ON true
    JOIN pg_attribute a
      ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    WHERE c.conrelid = 'pricing_tier_prices'::regclass
      AND c.contype = 'p';

    IF pk_cols IS DISTINCT FROM ARRAY['tier_code','currency','country_code'] THEN
      EXECUTE format('ALTER TABLE pricing_tier_prices DROP CONSTRAINT %I', pk_name);
      EXECUTE 'ALTER TABLE pricing_tier_prices ADD CONSTRAINT pricing_tier_prices_pkey PRIMARY KEY (tier_code, currency, country_code)';
    END IF;
  ELSE
    EXECUTE 'ALTER TABLE pricing_tier_prices ADD CONSTRAINT pricing_tier_prices_pkey PRIMARY KEY (tier_code, currency, country_code)';
  END IF;
END $$;

-- ------------------------------------------------------------
-- Check constraints (idempotent DO blocks)
-- ------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_pricing_res_status') THEN
    ALTER TABLE pricing_credit_reservations
      ADD CONSTRAINT ck_pricing_res_status
      CHECK (status IN ('reserved','finalized','released','expired'));
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_pricing_skus_status') THEN
    ALTER TABLE pricing_skus
      ADD CONSTRAINT ck_pricing_skus_status
      CHECK (status IN ('active','inactive'));
  END IF;
END $$;

COMMIT;