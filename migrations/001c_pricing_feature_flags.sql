-- services/svc-pricing/app/app/migrations/0004_pricing_feature_flags.sql
-- Adds production-grade feature/policy switches for modules and channels.

BEGIN;

CREATE TABLE IF NOT EXISTS pricing_feature_flags (
  code text PRIMARY KEY,                         -- e.g. module.music, module.api, channel.api
  scope text NOT NULL DEFAULT 'global',          -- global|country|tier|channel
  country_code text NOT NULL DEFAULT '',         -- '' = any/global
  tier_code text NOT NULL DEFAULT '',            -- '' = any/global
  channel text NOT NULL DEFAULT '',              -- '' = any/global (web|mobile|api)
  enabled boolean NOT NULL DEFAULT true,

  -- billing_mode governs behavior even when enabled=true
  -- disabled|shadow|free|bill
  billing_mode text NOT NULL DEFAULT 'bill',

  effective_from timestamptz NOT NULL DEFAULT now(),
  effective_to timestamptz NULL,
  priority int NOT NULL DEFAULT 100,             -- higher wins when multiple matches
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pricing_feature_flags_lookup
  ON pricing_feature_flags(code, enabled, billing_mode, scope, country_code, tier_code, channel, priority DESC, effective_from DESC);

-- Validate billing_mode values (idempotent)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_pricing_feature_flags_mode') THEN
    ALTER TABLE pricing_feature_flags
      ADD CONSTRAINT ck_pricing_feature_flags_mode
      CHECK (billing_mode IN ('disabled','shadow','free','bill'));
  END IF;
END $$;

COMMIT;