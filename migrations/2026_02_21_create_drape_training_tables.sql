CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS drape_templates (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  garment_type         TEXT NOT NULL,                  -- 'saree'
  drape_style          TEXT NOT NULL,                  -- 'nivi'
  version              INT  NOT NULL,                  -- 1,2,3...
  status               TEXT NOT NULL DEFAULT 'active',  -- active|deprecated|disabled
  is_active            BOOLEAN NOT NULL DEFAULT FALSE,  -- only one active per garment/style

  storage_container    TEXT NOT NULL,                  -- 'commerce-assets'
  storage_prefix       TEXT NOT NULL,                  -- 'drape_templates/saree/nivi/v1'

  manifest_json        JSONB NOT NULL DEFAULT '{}'::jsonb,

  pack_sha256          TEXT,
  pack_bytes           BIGINT,

  tags                 TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  notes                TEXT,

  created_by           UUID,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT drape_templates_version_uniq UNIQUE (garment_type, drape_style, version),
  CONSTRAINT drape_templates_status_chk CHECK (status IN ('active','deprecated','disabled'))
);



-- only one active template per garment/style
CREATE UNIQUE INDEX IF NOT EXISTS drape_templates_one_active
  ON drape_templates (garment_type, drape_style)
  WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS drape_templates_lookup
  ON drape_templates (garment_type, drape_style, status, version DESC);




-- training_examples  (referenced by training datasets and model checkpoints)
CREATE TABLE IF NOT EXISTS training_datasets (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                TEXT NOT NULL,
  kind                TEXT NOT NULL,   -- synthetic|licensed|user_opt_in|research_noncommercial
  usage_scope         TEXT NOT NULL,   -- commercial_ok|research_only
  license_name        TEXT,
  license_url         TEXT,

  storage_container   TEXT NOT NULL,
  storage_prefix      TEXT NOT NULL,   -- e.g. 'training/saree_synth/2026-02-22/<dataset_id>'

  recipe_json         JSONB NOT NULL DEFAULT '{}'::jsonb,
  stats_json          JSONB NOT NULL DEFAULT '{}'::jsonb,
  is_frozen           BOOLEAN NOT NULL DEFAULT FALSE,

  created_by          UUID,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT training_datasets_kind_chk CHECK (kind IN ('synthetic','licensed','user_opt_in','research_noncommercial')),
  CONSTRAINT training_datasets_scope_chk CHECK (usage_scope IN ('commercial_ok','research_only'))
);

CREATE INDEX IF NOT EXISTS training_datasets_lookup
  ON training_datasets (kind, usage_scope, created_at DESC);
  

CREATE INDEX IF NOT EXISTS training_datasets_lookup
  ON training_datasets (kind, usage_scope, created_at DESC);


--  training_examples (referenced by training datasets and model checkpoints)
CREATE TABLE IF NOT EXISTS training_examples (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dataset_id           UUID NOT NULL REFERENCES training_datasets(id) ON DELETE CASCADE,
  template_id          UUID REFERENCES drape_templates(id),

  split               TEXT NOT NULL DEFAULT 'train',   -- train|val|test
  task                TEXT NOT NULL DEFAULT 'saree_tryon',

  person_ref          JSONB NOT NULL DEFAULT '{}'::jsonb,
  garment_refs        JSONB NOT NULL DEFAULT '{}'::jsonb,
  conditioning_refs   JSONB NOT NULL DEFAULT '{}'::jsonb,

  target_ref          JSONB,
  mask_refs           JSONB NOT NULL DEFAULT '{}'::jsonb,
  labels_json         JSONB NOT NULL DEFAULT '{}'::jsonb,

  quality_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
  consent_json        JSONB NOT NULL DEFAULT '{}'::jsonb,

  dedup_hash          TEXT NOT NULL,
  sha256_json         JSONB NOT NULL DEFAULT '{}'::jsonb,

  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT training_examples_split_chk CHECK (split IN ('train','val','test'))
);

CREATE UNIQUE INDEX IF NOT EXISTS training_examples_dedup
  ON training_examples (dataset_id, dedup_hash);

CREATE INDEX IF NOT EXISTS training_examples_query
  ON training_examples (dataset_id, split, task);

CREATE INDEX IF NOT EXISTS training_examples_template
  ON training_examples (template_id);


-- model checkpoints + join table
CREATE TABLE IF NOT EXISTS model_checkpoints (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  model_family         TEXT NOT NULL,     -- saree_refiner|saree_vton|parser
  base_model           TEXT,

  status              TEXT NOT NULL DEFAULT 'running', -- running|succeeded|failed|deprecated
  code_git_sha         TEXT,
  config_json          JSONB NOT NULL DEFAULT '{}'::jsonb,
  hyperparams_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
  metrics_json         JSONB NOT NULL DEFAULT '{}'::jsonb,

  artifacts_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
  notes               TEXT,

  created_by           UUID,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT model_checkpoints_status_chk CHECK (status IN ('running','succeeded','failed','deprecated'))
);

CREATE INDEX IF NOT EXISTS model_checkpoints_lookup
  ON model_checkpoints (model_family, status, created_at DESC);

CREATE TABLE IF NOT EXISTS model_checkpoint_datasets (
  checkpoint_id        UUID NOT NULL REFERENCES model_checkpoints(id) ON DELETE CASCADE,
  dataset_id           UUID NOT NULL REFERENCES training_datasets(id) ON DELETE RESTRICT,
  PRIMARY KEY (checkpoint_id, dataset_id)
);


