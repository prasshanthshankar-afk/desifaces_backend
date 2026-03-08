-- services/svc-pricing/app/app/migrations/0007_pricing_sku_costs.sql
-- Per-SKU COGS components (currency-agnostic in the sense that we store base costs in USD).
-- Used for internal economics: cogs_money_est/final and gross margin snapshots.
-- Does NOT affect customer billing amounts.

BEGIN;

CREATE TABLE IF NOT EXISTS pricing_sku_costs (
  sku_code text NOT NULL REFERENCES pricing_skus(code) ON DELETE CASCADE,
  component_code text NOT NULL,                              -- e.g. heygen_subscription, fal_subscription_alloc, azure_infra_alloc
  cost_model text NOT NULL DEFAULT 'blended',                -- variable|amortized|blended
  cost_currency text NOT NULL DEFAULT 'USD',                 -- currently USD only; you can extend later
  variable_cost_money numeric(18,8) NOT NULL DEFAULT 0,      -- variable $ per unit
  fixed_monthly_cost_money numeric(18,8) NOT NULL DEFAULT 0, -- fixed $/month to amortize
  assumed_monthly_units numeric(18,6) NOT NULL DEFAULT 0,    -- units/month for amortization (if 0 => amort part ignored)
  is_active boolean NOT NULL DEFAULT true,
  effective_from timestamptz NOT NULL DEFAULT now(),
  effective_to timestamptz NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sku_code, component_code, effective_from)
);

-- Checks (idempotent)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_pricing_sku_costs_model') THEN
    ALTER TABLE pricing_sku_costs
      ADD CONSTRAINT ck_pricing_sku_costs_model
      CHECK (cost_model IN ('variable','amortized','blended'));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_pricing_sku_costs_nonneg') THEN
    ALTER TABLE pricing_sku_costs
      ADD CONSTRAINT ck_pricing_sku_costs_nonneg
      CHECK (
        variable_cost_money >= 0
        AND fixed_monthly_cost_money >= 0
        AND assumed_monthly_units >= 0
      );
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_pricing_sku_costs_lookup
  ON pricing_sku_costs(sku_code, is_active, effective_from DESC);

CREATE INDEX IF NOT EXISTS ix_pricing_sku_costs_component
  ON pricing_sku_costs(component_code, is_active, effective_from DESC);

COMMIT;