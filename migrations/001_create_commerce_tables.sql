-- services/svc-commerce/app/app/sql/001_create_commerce_tables.sql

BEGIN;

-- Needed for gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ----------------------------
-- Commerce products (SKU-level)
-- ----------------------------
CREATE TABLE IF NOT EXISTS public.commerce_products (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL,
  category       text NOT NULL,              -- apparel | fmcg | electronics | jewelry | other
  title          text NOT NULL,
  sku            text NULL,
  status         text NOT NULL DEFAULT 'active',   -- active | archived
  metadata_json  jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_commerce_products_user_id
  ON public.commerce_products(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_commerce_products_category
  ON public.commerce_products(category);

CREATE INDEX IF NOT EXISTS idx_commerce_products_sku
  ON public.commerce_products(sku);


-- ----------------------------------------
-- Product assets (photos, cutouts, masks…)
-- ----------------------------------------
CREATE TABLE IF NOT EXISTS public.commerce_product_assets (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id     uuid NOT NULL REFERENCES public.commerce_products(id) ON DELETE CASCADE,
  asset_type     text NOT NULL,       -- photo | cutout | mask | label_art | extra
  media_asset_id uuid NULL REFERENCES public.media_assets(id) ON DELETE SET NULL,
  artifact_id    uuid NULL REFERENCES public.artifacts(id) ON DELETE SET NULL,
  meta_json      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_commerce_product_assets_product_id
  ON public.commerce_product_assets(product_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_commerce_product_assets_type
  ON public.commerce_product_assets(asset_type);


-- --------------------------
-- Look-sets (Saree+Blouse…)
-- --------------------------
CREATE TABLE IF NOT EXISTS public.commerce_looksets (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL,
  title          text NOT NULL,
  items_json     jsonb NOT NULL DEFAULT '[]'::jsonb, -- [{"role":"saree","product_id":"..."}, ...]
  metadata_json  jsonb NOT NULL DEFAULT '{}'::jsonb,
  status         text NOT NULL DEFAULT 'active',
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_commerce_looksets_user_id
  ON public.commerce_looksets(user_id, created_at DESC);


-- -----------------------------
-- Quotes (Quote → Confirm gate)
-- -----------------------------
CREATE TABLE IF NOT EXISTS public.commerce_quotes (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          uuid NOT NULL,
  scope            text NOT NULL DEFAULT 'commerce',
  request_json     jsonb NOT NULL DEFAULT '{}'::jsonb,
  response_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
  total_credits    integer NOT NULL DEFAULT 0,
  total_usd        numeric(12,2) NOT NULL DEFAULT 0,
  total_inr        numeric(12,2) NOT NULL DEFAULT 0,
  status           text NOT NULL DEFAULT 'quoted',  -- quoted | confirmed | expired | canceled
  expires_at       timestamptz NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_commerce_quotes_user_id
  ON public.commerce_quotes(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_commerce_quotes_expires_at
  ON public.commerce_quotes(expires_at);

CREATE INDEX IF NOT EXISTS idx_commerce_quotes_status
  ON public.commerce_quotes(status);


-- --------------------------
-- Campaigns (single or bulk)
-- --------------------------
CREATE TABLE IF NOT EXISTS public.commerce_campaigns (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL,
  mode           text NOT NULL,  -- platform_models | customer_tryon
  product_type   text NOT NULL,  -- apparel | fmcg | electronics | mixed
  status         text NOT NULL DEFAULT 'draft', -- draft | queued | processing | succeeded | failed | canceled
  quote_id       uuid NULL REFERENCES public.commerce_quotes(id) ON DELETE SET NULL,
  input_json     jsonb NOT NULL DEFAULT '{}'::jsonb,
  meta_json      jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_message  text NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_commerce_campaigns_user_id
  ON public.commerce_campaigns(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_commerce_campaigns_status
  ON public.commerce_campaigns(status);


-- --------------------------
-- Variants (outputs)
-- --------------------------
CREATE TABLE IF NOT EXISTS public.commerce_variants (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id  uuid NOT NULL REFERENCES public.commerce_campaigns(id) ON DELETE CASCADE,
  kind         text NOT NULL, -- image | video
  artifact_id  uuid NULL REFERENCES public.artifacts(id) ON DELETE SET NULL,
  score_json   jsonb NOT NULL DEFAULT '{}'::jsonb,
  meta_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_commerce_variants_campaign_id
  ON public.commerce_variants(campaign_id, created_at DESC);


-- --------------------------
-- Exports (channel bundles)
-- --------------------------
CREATE TABLE IF NOT EXISTS public.commerce_exports (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id  uuid NOT NULL REFERENCES public.commerce_campaigns(id) ON DELETE CASCADE,
  channels     text[] NOT NULL DEFAULT ARRAY[]::text[],
  status       text NOT NULL DEFAULT 'queued', -- queued | processing | succeeded | failed
  artifact_id  uuid NULL REFERENCES public.artifacts(id) ON DELETE SET NULL,
  meta_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_message text NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_commerce_exports_campaign_id
  ON public.commerce_exports(campaign_id, created_at DESC);

COMMIT;