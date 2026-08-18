-- V3-C4: Canonical media lifecycle over the existing public.media_assets identity.
--
-- Principles:
-- - preserve existing media_assets.id UUIDs as canonical MediaAsset.media_id;
-- - do not create a competing media table;
-- - storage_ref remains the durable storage identity; signed/SAS URLs are delivery views;
-- - add explicit account ownership, lifecycle role/state, project/job linkage, and lineage;
-- - migration is V3-safe/idempotent and does not delete or rewrite blobs.

BEGIN;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE public.media_assets ADD COLUMN IF NOT EXISTS account_id uuid;
ALTER TABLE public.media_assets ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE public.media_assets ADD COLUMN IF NOT EXISTS role text;
ALTER TABLE public.media_assets ADD COLUMN IF NOT EXISTS lifecycle_state text NOT NULL DEFAULT 'active';
ALTER TABLE public.media_assets ADD COLUMN IF NOT EXISTS thumbnail_media_id uuid;
ALTER TABLE public.media_assets ADD COLUMN IF NOT EXISTS parent_generation_job_id uuid;
ALTER TABLE public.media_assets ADD COLUMN IF NOT EXISTS retention_until timestamptz;
ALTER TABLE public.media_assets ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

-- Canonical account ownership. Prefer an explicit active membership, then the
-- linked credit account, then the deterministic individual account code.
UPDATE public.media_assets ma
SET account_id = COALESCE(
    (
      SELECT bam.billing_account_id
      FROM public.pricing_billing_account_members bam
      JOIN public.pricing_billing_accounts ba ON ba.id = bam.billing_account_id
      WHERE bam.user_id = ma.user_id
        AND bam.status = 'active'
        AND ba.status = 'active'
      ORDER BY bam.is_default DESC,
               CASE bam.role WHEN 'owner' THEN 0 WHEN 'finance_admin' THEN 1 ELSE 2 END,
               bam.created_at ASC
      LIMIT 1
    ),
    (
      SELECT pca.billing_account_id
      FROM public.pricing_credit_accounts pca
      WHERE pca.user_id = ma.user_id
        AND pca.billing_account_id IS NOT NULL
      LIMIT 1
    ),
    (
      SELECT ba.id
      FROM public.pricing_billing_accounts ba
      WHERE ba.account_code = 'user:' || ma.user_id::text
        AND ba.status = 'active'
      LIMIT 1
    )
)
WHERE ma.account_id IS NULL;

-- Backfill lifecycle roles conservatively. New V3 writes must always supply an
-- explicit role through the shared MediaStore; these rules exist only to make
-- inherited V2 rows usable without changing their IDs.
UPDATE public.media_assets
SET role = CASE
    WHEN lower(coalesce(kind, '')) IN ('upload','source','source_image','byo_audio','voice_reference','voice_ref') THEN 'source'
    WHEN lower(coalesce(kind, '')) IN ('thumb','thumbnail') OR lower(coalesce(kind, '')) LIKE '%thumbnail%' THEN 'thumbnail'
    WHEN lower(coalesce(kind, '')) LIKE '%preview%' THEN 'preview'
    WHEN coalesce(meta_json->>'final_only','') IN ('1','true','TRUE')
      OR coalesce(meta_json->>'is_final','') IN ('1','true','TRUE')
      OR lower(coalesce(kind, '')) IN ('face','face_image','image','audio','audio_master','song_audio','full_mix','video','final_video')
      THEN 'final'
    ELSE 'intermediate'
END
WHERE role IS NULL OR btrim(role) = '';

UPDATE public.media_assets
SET lifecycle_state = 'deleted'
WHERE deleted_at IS NOT NULL AND lifecycle_state <> 'deleted';

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_assets_v3_role') THEN
    ALTER TABLE public.media_assets
      ADD CONSTRAINT ck_media_assets_v3_role
      CHECK (role IS NULL OR role IN ('source','intermediate','preview','final','thumbnail'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_assets_v3_lifecycle_state') THEN
    ALTER TABLE public.media_assets
      ADD CONSTRAINT ck_media_assets_v3_lifecycle_state
      CHECK (lifecycle_state IN ('active','archived','deleted'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_media_assets_thumbnail_media') THEN
    ALTER TABLE public.media_assets
      ADD CONSTRAINT fk_media_assets_thumbnail_media
      FOREIGN KEY (thumbnail_media_id) REFERENCES public.media_assets(id) ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_media_assets_account_created
  ON public.media_assets(account_id, created_at DESC)
  WHERE account_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_media_assets_account_role_created
  ON public.media_assets(account_id, role, created_at DESC)
  WHERE account_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_media_assets_parent_generation_job
  ON public.media_assets(parent_generation_job_id)
  WHERE parent_generation_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_media_assets_project_created
  ON public.media_assets(project_id, created_at DESC)
  WHERE project_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.v3_media_asset_lineage (
  source_media_id uuid NOT NULL REFERENCES public.media_assets(id) ON DELETE RESTRICT,
  derived_media_id uuid NOT NULL REFERENCES public.media_assets(id) ON DELETE CASCADE,
  relation text NOT NULL DEFAULT 'derived_from',
  sequence_no integer NOT NULL DEFAULT 0,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_media_id, derived_media_id, relation),
  CONSTRAINT ck_v3_media_lineage_no_self CHECK (source_media_id <> derived_media_id),
  CONSTRAINT ck_v3_media_lineage_relation_nonempty CHECK (length(btrim(relation)) > 0),
  CONSTRAINT ck_v3_media_lineage_sequence_nonneg CHECK (sequence_no >= 0)
);

CREATE INDEX IF NOT EXISTS idx_v3_media_lineage_derived
  ON public.v3_media_asset_lineage(derived_media_id, sequence_no, created_at);

-- Stable canonical read model. storage_uri intentionally maps to storage_ref;
-- temporary signed URLs must never be stored as canonical identity by V3 code.
CREATE OR REPLACE VIEW public.v3_media_assets AS
SELECT
  ma.id AS media_id,
  ma.account_id,
  ma.user_id AS owner_user_id,
  ma.project_id,
  CASE
    WHEN lower(coalesce(ma.kind,'')) IN ('audio','audio_master','song_audio','full_mix','voice_reference','voice_ref','byo_audio') THEN 'audio'
    WHEN lower(coalesce(ma.kind,'')) IN ('video','final_video') OR lower(coalesce(ma.content_type,'')) LIKE 'video/%' THEN 'video'
    WHEN lower(coalesce(ma.kind,'')) IN ('face','face_image','image','upload','source_image','thumb','thumbnail') OR lower(coalesce(ma.content_type,'')) LIKE 'image/%' THEN 'image'
    WHEN lower(coalesce(ma.content_type,'')) LIKE 'audio/%' THEN 'audio'
    WHEN lower(coalesce(ma.content_type,'')) LIKE 'application/%' OR lower(coalesce(ma.content_type,'')) LIKE 'text/%' THEN 'document'
    ELSE 'other'
  END AS media_kind,
  ma.role,
  ma.lifecycle_state,
  ma.content_type AS mime_type,
  ma.storage_ref AS storage_uri,
  ma.thumbnail_media_id,
  ma.parent_generation_job_id,
  ma.sha256,
  ma.bytes,
  ma.width,
  ma.height,
  ma.duration_ms,
  ma.retention_until,
  ma.deleted_at,
  ma.meta_json AS metadata,
  ma.created_at,
  ma.updated_at
FROM public.media_assets ma;

COMMIT;
