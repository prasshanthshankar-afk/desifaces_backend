-- migrations/020_media_assets.sql
-- Durable user media library (uploads + reusable outputs)
-- Depends on: 000_bootstrap.sql (and/or pgcrypto extension)

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -------------------------------------------------------------------
-- media_assets: long-lived media library (not job-scoped)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_assets (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       integer NOT NULL,

  -- High-level classification: upload|face|audio|video|image|thumb|other
  kind          text NOT NULL,

  -- Where the asset lives (prefer az://container/blob; can also be https://)
  storage_ref   text NOT NULL,

  content_type  text,
  bytes         bigint,
  sha256        text,

  -- Optional media metadata
  width         integer,
  height        integer,
  duration_ms   integer,

  -- Free-form metadata: prompt, provider, locale, tags, safety, lineage, etc.
  meta_json     jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_media_assets_kind_nonempty CHECK (length(trim(kind)) > 0),
  CONSTRAINT ck_media_assets_storage_ref_nonempty CHECK (length(trim(storage_ref)) > 0),
  CONSTRAINT ck_media_assets_bytes_nonneg CHECK (bytes IS NULL OR bytes >= 0),
  CONSTRAINT ck_media_assets_dims_nonneg CHECK (
    (width  IS NULL OR width  >= 0) AND
    (height IS NULL OR height >= 0)
  ),
  CONSTRAINT ck_media_assets_duration_nonneg CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

CREATE INDEX IF NOT EXISTS idx_media_assets_user_created
  ON media_assets (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_media_assets_user_kind_created
  ON media_assets (user_id, kind, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_media_assets_sha256
  ON media_assets (sha256);

CREATE INDEX IF NOT EXISTS idx_media_assets_storage_ref
  ON media_assets (storage_ref);

-- Optional: de-dupe exact same content for a user when sha256 is known
-- (Allows multiple assets with NULL sha256)
CREATE UNIQUE INDEX IF NOT EXISTS uq_media_assets_user_sha256
  ON media_assets (user_id, sha256)
  WHERE sha256 IS NOT NULL;

-- -------------------------------------------------------------------
-- media_asset_versions: optional versioning for remix/compare lineage
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_asset_versions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id        uuid NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
  version         integer NOT NULL,
  storage_ref     text NOT NULL,
  content_type    text,
  bytes           bigint,
  sha256          text,
  meta_json       jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_media_asset_versions_version_pos CHECK (version > 0),
  CONSTRAINT ck_media_asset_versions_storage_ref_nonempty CHECK (length(trim(storage_ref)) > 0)
);

-- One version number per asset
CREATE UNIQUE INDEX IF NOT EXISTS uq_media_asset_versions_asset_version
  ON media_asset_versions (asset_id, version);

CREATE INDEX IF NOT EXISTS idx_media_asset_versions_asset_created
  ON media_asset_versions (asset_id, created_at DESC);

COMMIT;