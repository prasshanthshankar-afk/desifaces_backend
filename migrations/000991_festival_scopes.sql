-- services/svc-marketing/app/app/migrations/20260305_02_festival_scopes_prod.sql
-- Postgres production-safe + idempotent.
-- Fix: drop old UNIQUE(festival_id, festival_date) using conkey (smallint[]) instead of name[] comparisons.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ------------------------------------------------------------
-- 1) Base tables
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS marketing_festival_definitions (
  festival_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text UNIQUE NOT NULL,
  name text NOT NULL,

  -- backward-compatible legacy fields (optional now)
  country_code text NULL,
  region_code text NULL,
  religion text NULL,
  timezone text NULL,

  category text NOT NULL DEFAULT 'festival',   -- festival | holiday | observance
  lead_days int NOT NULL DEFAULT 2,
  lag_days int NOT NULL DEFAULT 0,
  priority int NOT NULL DEFAULT 50,
  enabled boolean NOT NULL DEFAULT true,

  motifs_json jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE marketing_festival_definitions
  ADD COLUMN IF NOT EXISTS motifs_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS category text NOT NULL DEFAULT 'festival',
  ADD COLUMN IF NOT EXISTS lead_days int NOT NULL DEFAULT 2,
  ADD COLUMN IF NOT EXISTS lag_days int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS priority int NOT NULL DEFAULT 50,
  ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS country_code text NULL,
  ADD COLUMN IF NOT EXISTS region_code text NULL,
  ADD COLUMN IF NOT EXISTS religion text NULL,
  ADD COLUMN IF NOT EXISTS timezone text NULL,
  ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS marketing_festival_occurrences (
  occurrence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  festival_id uuid NOT NULL REFERENCES marketing_festival_definitions(festival_id) ON DELETE CASCADE,

  festival_date date NOT NULL,
  year int NOT NULL,

  start_ts timestamptz NULL,
  end_ts timestamptz NULL,

  notes text NULL,
  sources_json jsonb NOT NULL DEFAULT '[]'::jsonb,

  -- scope_id added later (after scopes table exists)

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE marketing_festival_occurrences
  ADD COLUMN IF NOT EXISTS sources_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS notes text NULL,
  ADD COLUMN IF NOT EXISTS start_ts timestamptz NULL,
  ADD COLUMN IF NOT EXISTS end_ts timestamptz NULL,
  ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

-- ------------------------------------------------------------
-- 2) Scopes table (extensibility layer)
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS marketing_festival_scopes (
  scope_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  festival_id uuid NOT NULL REFERENCES marketing_festival_definitions(festival_id) ON DELETE CASCADE,

  -- ISO3166-1 alpha2: IN, US, GB, AE ...
  country_code text NOT NULL,

  -- ISO3166-2 subdivision: IN-TN, IN-OD, US-CA ...
  region_code text NULL,

  -- hindu, islam, christian, sikh, jewish, buddhist, secular, etc.
  religion text NULL,

  -- hi-IN, ta-IN, en-US, etc.
  locale text NULL,

  -- moon-sighting variants etc: local_sighting / saudi / umm_al_qura / calculation
  observance_variant text NULL,

  timezone text NOT NULL DEFAULT 'Asia/Kolkata',

  enabled boolean NOT NULL DEFAULT true,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Correct uniqueness: UNIQUE INDEX on expressions (valid in Postgres)
CREATE UNIQUE INDEX IF NOT EXISTS uq_mkt_festival_scopes_norm
  ON marketing_festival_scopes (
    festival_id,
    country_code,
    COALESCE(region_code, ''),
    COALESCE(religion, ''),
    COALESCE(locale, ''),
    COALESCE(observance_variant, '')
  );

CREATE INDEX IF NOT EXISTS idx_mkt_fscope_country  ON marketing_festival_scopes(country_code);
CREATE INDEX IF NOT EXISTS idx_mkt_fscope_region   ON marketing_festival_scopes(region_code);
CREATE INDEX IF NOT EXISTS idx_mkt_fscope_religion ON marketing_festival_scopes(religion);

-- ------------------------------------------------------------
-- 3) Add scope_id to occurrences (after scopes exists)
-- ------------------------------------------------------------

ALTER TABLE marketing_festival_occurrences
  ADD COLUMN IF NOT EXISTS scope_id uuid NULL REFERENCES marketing_festival_scopes(scope_id) ON DELETE CASCADE;

-- ------------------------------------------------------------
-- 4) Backfill: default scope per festival definition
-- ------------------------------------------------------------

INSERT INTO marketing_festival_scopes (
  festival_id, country_code, region_code, religion, locale, observance_variant, timezone, enabled
)
SELECT
  d.festival_id,
  COALESCE(NULLIF(UPPER(d.country_code), ''), 'IN') AS country_code,
  NULLIF(d.region_code, '') AS region_code,
  NULLIF(d.religion, '') AS religion,
  NULL AS locale,
  NULL AS observance_variant,
  COALESCE(NULLIF(d.timezone, ''), 'Asia/Kolkata') AS timezone,
  d.enabled
FROM marketing_festival_definitions d
ON CONFLICT DO NOTHING;

-- Backfill occurrences.scope_id where missing
UPDATE marketing_festival_occurrences o
SET scope_id = s.scope_id
FROM marketing_festival_definitions d
JOIN marketing_festival_scopes s
  ON s.festival_id = d.festival_id
 AND s.country_code = COALESCE(NULLIF(UPPER(d.country_code), ''), 'IN')
 AND COALESCE(s.region_code, '') = COALESCE(NULLIF(d.region_code, ''), '')
 AND COALESCE(s.religion, '') = COALESCE(NULLIF(d.religion, ''), '')
 AND COALESCE(s.locale, '') = ''
 AND COALESCE(s.observance_variant, '') = ''
WHERE o.festival_id = d.festival_id
  AND o.scope_id IS NULL;

-- ------------------------------------------------------------
-- 5) Drop old UNIQUE(festival_id, festival_date) constraint if present
--    Robust: uses pg_constraint.conkey (smallint[]) not name[] comparisons
-- ------------------------------------------------------------

DO $$
DECLARE
  rel oid := 'marketing_festival_occurrences'::regclass;
  att_festival_id smallint;
  att_festival_date smallint;
  c record;
BEGIN
  SELECT attnum INTO att_festival_id
  FROM pg_attribute
  WHERE attrelid = rel AND attname = 'festival_id' AND NOT attisdropped;

  SELECT attnum INTO att_festival_date
  FROM pg_attribute
  WHERE attrelid = rel AND attname = 'festival_date' AND NOT attisdropped;

  IF att_festival_id IS NULL OR att_festival_date IS NULL THEN
    RETURN;
  END IF;

  FOR c IN
    SELECT conname
    FROM pg_constraint
    WHERE conrelid = rel
      AND contype = 'u'
      AND array_length(conkey, 1) = 2
      AND conkey @> ARRAY[att_festival_id, att_festival_date]::smallint[]
  LOOP
    EXECUTE format('ALTER TABLE marketing_festival_occurrences DROP CONSTRAINT IF EXISTS %I', c.conname);
  END LOOP;
END $$;

-- ------------------------------------------------------------
-- 6) Indexes + correct uniqueness for occurrences
-- ------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_mkt_focc_date     ON marketing_festival_occurrences(festival_date);
CREATE INDEX IF NOT EXISTS idx_mkt_focc_festival ON marketing_festival_occurrences(festival_id);

-- Correct uniqueness: one occurrence per scope_id per date
CREATE UNIQUE INDEX IF NOT EXISTS uq_mkt_focc_scope_date
  ON marketing_festival_occurrences(scope_id, festival_date);

CREATE INDEX IF NOT EXISTS idx_mkt_focc_scope_date
  ON marketing_festival_occurrences(scope_id, festival_date);

COMMIT;